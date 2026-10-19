"""Triage calls (PLAN.md "LLM interaction extras", phase-6 brief "Decisions fixed": triage).

A triage sends one run's open findings, numbered, together with that run's scope (resolved
against the current source) and the full spec outline to a fresh call, rendered by the
`triage` pass (`kind: triage`). It is the one place where earlier findings enter a prompt
(PLAN.md "Rule enforcement": clean context). The call is an ordinary run on the phase-4
machinery (sandbox, flags, cancel, cost, stored prompt) with pass `triage` and the judged
run's scope; it is never cached and never seeds the cache.

The model answers `{"verdicts": [{"index", "verdict": "keep"|"wrong"|"trivial", "reason"}]}`
where `index` is the finding's number in the list (1-based). Entries with an index outside
the list, a repeated index (the first one counts), an unknown verdict, extra keys, or
replacement wording in the reason (the no-rewrites rule) are ignored and counted. Each
verdict is stored on its finding row (`triage_verdict`, `triage_reason`, `triage_id`);
a finding that was sent but got no verdict has its earlier verdict cleared. A verdict never
changes a finding's `state`: `dismiss_flagged` (the UI's "Dismiss all flagged") does.

The spec is only read (app.prompts); model output only becomes rows in state.db.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app import db, prompts, runstore
from app.config import ConfigError
from app.events import EventBus, FindingsUpdated, TriageFinished
from app.findings import (
    TRIAGE_VERDICTS,
    Finding,
    OutputParseError,
    collapse,
    extract_json,
    has_rewrite_text,
)
from app.prompts import PromptError
from app.repo import Repo
from app.runner import RunResult
from app.runs import RunError, RunManager, RunOutcome, RunPlan
from app.runstore import Run

log = logging.getLogger(__name__)

TriageStatus = Literal["queued", "running", "done", "failed"]
TERMINAL: tuple[str, ...] = ("done", "failed")
FLAGGED: tuple[str, ...] = ("wrong", "trivial")
VERDICT_KEYS = frozenset({"index", "verdict", "reason"})

VERDICTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "verdict", "reason"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The finding's number in the list.",
                    },
                    "verdict": {"type": "string", "enum": list(TRIAGE_VERDICTS)},
                    "reason": {
                        "type": "string",
                        "description": "One sentence: what decided the verdict.",
                    },
                },
            },
        }
    },
}


class TriageError(Exception):
    """A triage request the API refuses; `status` is the HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- API models


class TriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner: str | None = None
    model: str | None = None
    thinking: str | None = None


class TriageCounts(BaseModel):
    keep: int = 0
    wrong: int = 0
    trivial: int = 0


class Triage(BaseModel):
    """A triage as the API returns it (phase-6 brief "API contract"; extras marked +)."""

    id: str
    run_id: str  # the judged run
    triage_run_id: str | None  # the run that made the call
    status: TriageStatus
    error: str | None
    counts: TriageCounts
    created_at: float
    finished_at: float | None
    sent: int = 0  # + how many findings the call judged (the run's open findings)
    ignored: int = 0  # + verdict entries that were ignored (bad index, verdict or keys)


class TriageDetail(BaseModel):
    triage: Triage | None  # the latest triage of the run
    findings: list[Finding]  # the run's findings with their verdicts


# --------------------------------------------------------------------------- the answer


@dataclass
class VerdictEntry:
    index: int
    verdict: str
    reason: str


@dataclass
class TriageAnswer:
    verdicts: list[VerdictEntry]
    ignored: int  # entries already dropped while parsing


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_verdicts(result: RunResult) -> RunOutcome:
    """`{"verdicts": [...]}` (or a bare list of verdicts) → done with a TriageAnswer; an
    answer that is not such a list → failed (the raw answer stays on the run). Single bad
    entries are dropped and counted, not fatal."""
    try:
        value = result.structured if result.structured is not None else extract_json(result.text)[0]
    except OutputParseError as exc:
        return RunOutcome(status="failed", error=f"could not parse the verdicts: {exc}")
    if isinstance(value, dict):
        if not isinstance(value.get("verdicts"), list):
            return RunOutcome(status="failed", error="the answer has no `verdicts` list")
        items = value["verdicts"]
    elif isinstance(value, list):
        items = value
    else:
        kind = type(value).__name__
        return RunOutcome(status="failed", error=f"the answer is a {kind}, not a JSON object")
    verdicts: list[VerdictEntry] = []
    ignored = 0
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) - VERDICT_KEYS
            or not _is_int(item.get("index"))
            or not isinstance(item.get("verdict"), str)
            or not isinstance(item.get("reason", ""), str)
        ):
            ignored += 1
            continue
        verdict = item["verdict"].strip().lower()
        reason = str(item.get("reason", "")).strip()
        if verdict not in TRIAGE_VERDICTS or has_rewrite_text(reason):
            ignored += 1
            continue
        verdicts.append(VerdictEntry(index=item["index"], verdict=verdict, reason=reason))
    return RunOutcome(status="done", data=TriageAnswer(verdicts=verdicts, ignored=ignored))


def render_findings(findings: Sequence[Finding]) -> str:
    """The `findings` template variable: one numbered entry per finding with where it is
    (the current anchor lines, matching the numbered source), its quote, problem and why."""
    blocks = []
    for number, finding in enumerate(findings, start=1):
        start, end = finding.anchor_line_start, finding.anchor_line_end
        lines = f"line {start}" if start == end else f"lines {start}-{end}"
        about = f"{finding.level}, {finding.category}, {finding.severity}"
        if finding.json_ref:
            about += f"; spec entry {finding.json_ref}"
        blocks.append(
            "\n".join(
                [
                    f"{number}. {finding.file}, {lines} ({about})",
                    f"   quote: {collapse(finding.quote)}",
                    f"   problem: {collapse(finding.problem)}",
                    f"   why: {collapse(finding.why)}",
                ]
            )
        )
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- rows


def new_id() -> str:
    return f"tri{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def _triage(row: sqlite3.Row, run: Run | None) -> Triage:
    status: str = row["status"]
    if status not in TERMINAL:  # follow the triage run while it is active
        status = "running" if run is not None and run.status != "queued" else "queued"
    return Triage(
        id=row["id"],
        run_id=row["run_id"],
        triage_run_id=row["triage_run_id"],
        status=status,  # type: ignore[arg-type]
        error=row["error"],
        counts=TriageCounts(
            keep=row["keep_count"], wrong=row["wrong_count"], trivial=row["trivial_count"]
        ),
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        sent=len(json.loads(row["finding_ids_json"])),
        ignored=row["ignored_count"],
    )


def _from_row(conn: sqlite3.Connection, row: sqlite3.Row | None) -> Triage | None:
    if row is None:
        return None
    run = runstore.get_run(conn, row["triage_run_id"]) if row["triage_run_id"] else None
    return _triage(row, run)


def _row(conn: sqlite3.Connection, triage_id: str) -> sqlite3.Row | None:
    return _cursor(conn).execute("SELECT * FROM triages WHERE id = ?", (triage_id,)).fetchone()


def get_triage(conn: sqlite3.Connection, triage_id: str) -> Triage | None:
    return _from_row(conn, _row(conn, triage_id))


def latest_triage(conn: sqlite3.Connection, run_id: str) -> Triage | None:
    row = (
        _cursor(conn)
        .execute(
            "SELECT * FROM triages WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (run_id,),
        )
        .fetchone()
    )
    return _from_row(conn, row)


def active_triage(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM triages WHERE run_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Triages a previous server process left unfinished become failed."""
    cur = conn.execute(
        "UPDATE triages SET status = 'failed', error = 'interrupted: the server stopped during "
        "the triage', finished_at = ? WHERE status IN ('queued', 'running')",
        (time.time(),),
    )
    return cur.rowcount


def store_verdicts(
    conn: sqlite3.Connection, triage_id: str, finding_ids: Sequence[str], answer: TriageAnswer
) -> TriageCounts:
    """Write the verdicts onto the findings that were sent (index i = finding_ids[i - 1])
    and the counts onto the triage row; returns the counts."""
    judged: dict[int, VerdictEntry] = {}
    ignored = answer.ignored
    for entry in answer.verdicts:
        if not 1 <= entry.index <= len(finding_ids) or entry.index in judged:
            ignored += 1
            continue
        judged[entry.index] = entry
    counts = TriageCounts()
    now = time.time()
    with db.transaction(conn):
        for number, finding_id in enumerate(finding_ids, start=1):
            entry = judged.get(number)
            if entry is not None:
                setattr(counts, entry.verdict, getattr(counts, entry.verdict) + 1)
            conn.execute(
                "UPDATE findings SET triage_verdict = ?, triage_reason = ?, triage_id = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    entry.verdict if entry else None,
                    (entry.reason or None) if entry else None,
                    triage_id if entry else None,
                    now,
                    finding_id,
                ),
            )
        conn.execute(
            "UPDATE triages SET status = 'done', error = NULL, keep_count = ?, wrong_count = ?, "
            "trivial_count = ?, ignored_count = ?, finished_at = ? WHERE id = ?",
            (counts.keep, counts.wrong, counts.trivial, ignored, now, triage_id),
        )
    return counts


def dismiss_flagged(conn: sqlite3.Connection, run_id: str) -> int:
    """Set `dismissed` on the run's open findings whose verdict is wrong or trivial."""
    cur = conn.execute(
        "UPDATE findings SET state = 'dismissed', updated_at = ? WHERE run_id = ? AND "
        "state = 'open' AND triage_verdict IN ('wrong', 'trivial')",
        (time.time(), run_id),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- manager


@dataclass
class Prepared:
    triage_id: str
    run: Run
    finding_ids: list[str]
    plan: RunPlan


class TriageManager:
    """Starts triage calls on the shared RunManager and stores their verdicts."""

    def __init__(self, bus: EventBus, runs: RunManager) -> None:
        self.bus = bus
        self.runs = runs

    async def on_repo_opened(self, repo: Repo) -> None:
        def work() -> None:
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    count = mark_interrupted(conn)
                if count:
                    log.info("marked %d interrupted triages as failed", count)

        try:
            await asyncio.to_thread(work)
        except Exception:  # start-up must not fail on state.db
            log.exception("triage start-up work failed")

    def prepare(self, repo: Repo, run_id: str, request: TriageRequest) -> Prepared:
        """The prompt and the plan (blocking): 404 unknown run, 409 a triage of the run is
        active, 400 no open findings / no usable triage pass / unresolvable scope / bad
        runner settings."""
        if not db.db_path(repo).exists():
            raise TriageError(404, f"unknown run {run_id!r}")
        with db.connect(repo) as conn:
            run = runstore.get_run(conn, run_id)
            if run is None:
                raise TriageError(404, f"unknown run {run_id!r}")
            active = active_triage(conn, run_id)
            if active is not None:
                raise TriageError(409, f"a triage of run {run_id} is already active ({active})")
            findings = runstore.list_findings(conn, run_id=run_id, state="open")
        if not findings:
            raise TriageError(400, f"run {run_id} has no open findings to triage")
        try:
            config = repo.load_config()
            assembled = prompts.assemble_triage_prompt(
                repo,
                run.scope,
                render_findings(findings),
                json.dumps(VERDICTS_SCHEMA, indent=2),
            )
            plan = self.runs.plan_assembled(
                repo,
                config,
                assembled,
                request.runner,
                request.model,
                request.thinking,
                schema=VERDICTS_SCHEMA,
                scope_key_override=f"{prompts.TRIAGE_PASS}:{run_id}",
                cacheable=False,
            )
            self.runs.check_available(plan)
        except (PromptError, ConfigError) as exc:
            raise TriageError(400, str(exc)) from exc
        except RunError as exc:
            raise TriageError(exc.status, exc.detail) from exc
        plan.interpret = parse_verdicts
        return Prepared(
            triage_id=new_id(), run=run, finding_ids=[f.id for f in findings], plan=plan
        )

    async def start(self, repo: Repo, run_id: str, request: TriageRequest) -> Triage:
        prepared = await asyncio.to_thread(self.prepare, repo, run_id, request)
        triage_id = prepared.triage_id

        def on_created(conn: sqlite3.Connection, run: Run) -> None:
            conn.execute(
                "INSERT INTO triages (id, run_id, triage_run_id, status, finding_ids_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    triage_id,
                    run_id,
                    run.id,
                    "queued",
                    json.dumps(prepared.finding_ids),
                    time.time(),
                ),
            )

        prepared.plan.on_finished = lambda run, data: self._finished(repo, triage_id, run, data)
        try:
            await self.runs.launch(repo, prepared.plan, use_cache=False, on_created=on_created)
        except RunError as exc:
            raise TriageError(exc.status, exc.detail) from exc

        def read() -> Triage:
            with db.connect(repo) as conn:
                triage = get_triage(conn, triage_id)
            assert triage is not None
            return triage

        return await asyncio.to_thread(read)

    def _finished(self, repo: Repo, triage_id: str, run: Run, data: Any) -> None:
        """The triage run ended: store the verdicts (done) or the error (failed), then
        publish `triage.finished` and, for verdicts, `findings.updated` for the judged run."""
        with db.connect(repo) as conn:
            row = _row(conn, triage_id)
            if row is None:
                return
            stored = False
            if run.status == "done" and isinstance(data, TriageAnswer):
                store_verdicts(conn, triage_id, json.loads(row["finding_ids_json"]), data)
                stored = True
            else:
                conn.execute(
                    "UPDATE triages SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (run.error or f"the run ended {run.status}", time.time(), triage_id),
                )
            triage = get_triage(conn, triage_id)
        if triage is not None:
            self.bus.publish(TriageFinished(triage=triage.model_dump(mode="json")))
        if stored:
            self.bus.publish(FindingsUpdated(run_id=row["run_id"]))

    async def detail(self, repo: Repo, run_id: str) -> TriageDetail:
        def work() -> TriageDetail | None:
            if not db.db_path(repo).exists():
                return None
            with db.connect(repo) as conn:
                if runstore.get_run(conn, run_id) is None:
                    return None
                return TriageDetail(
                    triage=latest_triage(conn, run_id),
                    findings=runstore.list_findings(conn, run_id=run_id),
                )

        detail = await asyncio.to_thread(work)
        if detail is None:
            raise TriageError(404, f"unknown run {run_id!r}")
        return detail

    async def dismiss_flagged(self, repo: Repo, run_id: str) -> int:
        def work() -> int | None:
            if not db.db_path(repo).exists():
                return None
            with db.connect(repo) as conn:
                if runstore.get_run(conn, run_id) is None:
                    return None
                return dismiss_flagged(conn, run_id)

        updated = await asyncio.to_thread(work)
        if updated is None:
            raise TriageError(404, f"unknown run {run_id!r}")
        if updated:
            self.bus.publish(FindingsUpdated(run_id=run_id))
        return updated
