"""Blind A/B comparison of two versions of one paragraph (PLAN.md "Workflows": "Blind A/B
compare"; phase-5 brief "Decisions fixed").

1. "before" is the paragraph as it is at a ref (a snapshot tag or any commit): the file at
   that ref (`git show`), the paragraph under `% @p <node_id>` by the phase-3 marker rule,
   else the given line range at that ref. "after" is the same paragraph in the working
   tree, found the same way.
2. The two versions go to a fresh model call in random order (`secrets`), rendered by the
   `ab-compare` pass with only the relevant spec entries (message, the paragraph, its
   section, the claims they link) and no hint that either version is an edit: both are
   "candidate versions". LaTeX comments are removed from the versions (they may carry notes
   such as "revised"); the texts shown to the user keep them.
3. The call goes through the phase-4 run machinery (same sandbox, flags, clean context,
   cancel, cost, stored prompt) as a Run with pass `ab-compare` and scope kind `selection`.
   It is never cached and never seeds the cache.
4. The answer must be exactly `{"verdict": "A"|"B"|"tie", "reason": str}` (structured
   output first, then the JSON in the text). Anything else fails the comparison; the raw
   answer stays on the run.
5. The mapping (which of A and B was "before") is stored but only returned once the
   comparison is done and the caller asks for it (`reveal`).

The spec is only read here (app.spec.load_spec); model output only becomes rows in state.db.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app import db, gitops, prompts, runstore, skeleton, snapshots
from app.config import ConfigError
from app.events import CompareFinished, EventBus
from app.files import (
    NotTextError,
    PathEscapeError,
    TooLargeError,
    is_protected_for_write,
    normalize_rel,
    read_text_file,
)
from app.findings import OutputParseError, extract_json, has_rewrite_text
from app.gitops import GitError
from app.prompts import PromptError, Scope, source_lines
from app.repo import Repo
from app.runner import RunResult
from app.runs import RunError, RunManager, RunOutcome, RunPlan
from app.runstore import Run
from app.spec import PaperSpec, find_tree_node, load_spec

log = logging.getLogger(__name__)

Verdict = Literal["A", "B", "tie"]
Side = Literal["before", "after"]
CompareStatus = Literal["queued", "running", "done", "failed"]
TERMINAL: tuple[str, ...] = ("done", "failed")

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["A", "B", "tie"],
            "description": "The version that better delivers the paragraph's local message.",
        },
        "reason": {
            "type": "string",
            "description": "The weaknesses that decided the verdict, in one or two sentences.",
        },
    },
}


class CompareError(Exception):
    """A comparison request the API refuses; `status` is the HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- API models


class Before(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["snapshot", "sha"]
    ref: str  # snapshot: its id or tag; sha: any commit-ish (sha, branch, tag, HEAD~1)


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    node_id: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    before: Before
    runner: str | None = None
    model: str | None = None
    thinking: str | None = None


class Mapping(BaseModel):
    A: Side
    B: Side


class Comparison(BaseModel):
    id: str
    file: str
    node_id: str | None
    line_start: int | None
    line_end: int | None
    before: Before
    run_id: str | None
    status: CompareStatus
    verdict: Verdict | None
    reason: str | None
    mapping: Mapping | None  # null unless revealed (status done and ?reveal=true)
    error: str | None
    created_at: float
    finished_at: float | None
    before_sha: str | None = None  # + the commit "before" was read from
    before_lines: list[int] | None = None  # + [first, last] line of the paragraph at the ref
    after_lines: list[int] | None = None  # + [first, last] line in the working tree
    revealed_at: float | None = None  # + when the mapping was first revealed


class CompareDetail(BaseModel):
    comparison: Comparison
    before_text: str
    after_text: str
    run: Run | None
    version_a: str = ""  # + the text shown to the model as version A (comments removed)
    version_b: str = ""  # + ... as version B
    raw_output: str | None = None  # + the model's raw answer (kept when it failed to parse)


@dataclass
class VerdictAnswer:
    verdict: Verdict
    reason: str


# --------------------------------------------------------------------------- locating text


@dataclass
class Located:
    text: str  # the raw source lines, joined with \n
    line_start: int
    line_end: int


def locate(
    lines: list[str], node_id: str | None, line_range: tuple[int, int] | None
) -> Located | None:
    """The paragraph under `% @p <node_id>` in `lines`, else the line range, else None."""
    if node_id:
        found = skeleton.marker_paragraph(lines, node_id)
        if found is not None:
            _, start, end = found
            return Located("\n".join(lines[start - 1 : end]), start, end)
    if line_range is not None:
        start, end = line_range
        if 1 <= start <= end and start <= len(lines):
            end = min(end, len(lines))
            return Located("\n".join(lines[start - 1 : end]), start, end)
    return None


def _describe_target(node_id: str | None, line_range: tuple[int, int] | None) -> str:
    parts = []
    if node_id:
        parts.append(f"no '% @p {node_id}' paragraph")
    if line_range:
        parts.append(f"no lines {line_range[0]}-{line_range[1]}")
    return " and ".join(parts)


# --------------------------------------------------------------------------- the answer


def parse_verdict(result: RunResult) -> RunOutcome:
    """Exactly `{"verdict": "A"|"B"|"tie", "reason": str}` → done with a VerdictAnswer;
    anything else → failed (the raw answer stays on the run)."""
    try:
        value = result.structured if result.structured is not None else extract_json(result.text)[0]
    except OutputParseError as exc:
        return RunOutcome(status="failed", error=f"could not parse the verdict: {exc}")
    if not isinstance(value, dict):
        kind = type(value).__name__
        return RunOutcome(status="failed", error=f"the answer is a {kind}, not a JSON object")
    keys = set(value)
    if keys != {"verdict", "reason"}:
        missing = sorted({"verdict", "reason"} - keys)
        extra = sorted(keys - {"verdict", "reason"})
        detail = "; ".join(
            part
            for part in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unexpected {', '.join(extra)}" if extra else "",
            )
            if part
        )
        return RunOutcome(
            status="failed", error=f"the answer must be {{verdict, reason}}: {detail}"
        )
    verdict, reason = value["verdict"], value["reason"]
    normalized = {"a": "A", "b": "B", "tie": "tie"}.get(str(verdict).strip().lower())
    if not isinstance(verdict, str) or normalized is None:
        return RunOutcome(status="failed", error=f"verdict must be A, B or tie; got {verdict!r}")
    if not isinstance(reason, str):
        return RunOutcome(status="failed", error="reason must be a string")
    if has_rewrite_text(reason):
        return RunOutcome(
            status="failed", error="rejected: the reason contains replacement text (rewrite_text)"
        )
    return RunOutcome(
        status="done",
        data=VerdictAnswer(verdict=normalized, reason=reason.strip()),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- rows


def new_id() -> str:
    return f"cmp{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def _lines(start: Any, end: Any) -> list[int] | None:
    return [int(start), int(end)] if start is not None and end is not None else None


def _comparison(row: sqlite3.Row, run: Run | None, reveal: bool) -> Comparison:
    status: str = row["status"]
    if status not in TERMINAL:  # follow the run while it is active
        status = "running" if run is not None and run.status != "queued" else "queued"
    mapping = None
    if reveal and status == "done":
        mapping = Mapping.model_validate(json.loads(row["mapping_json"]))
    return Comparison(
        id=row["id"],
        file=row["file"],
        node_id=row["node_id"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        before=Before(kind=row["before_kind"], ref=row["before_ref"]),
        run_id=row["run_id"],
        status=status,  # type: ignore[arg-type]
        verdict=row["verdict"],
        reason=row["reason"],
        mapping=mapping,
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        before_sha=row["before_sha"],
        before_lines=_lines(row["before_line_start"], row["before_line_end"]),
        after_lines=_lines(row["after_line_start"], row["after_line_end"]),
        revealed_at=row["revealed_at"],
    )


def _row(conn: sqlite3.Connection, comparison_id: str) -> sqlite3.Row | None:
    return (
        _cursor(conn).execute("SELECT * FROM comparisons WHERE id = ?", (comparison_id,)).fetchone()
    )


def _run_of(conn: sqlite3.Connection, row: sqlite3.Row) -> Run | None:
    return runstore.get_run(conn, row["run_id"]) if row["run_id"] else None


def get_comparison(
    conn: sqlite3.Connection, comparison_id: str, reveal: bool = False
) -> Comparison | None:
    row = _row(conn, comparison_id)
    return None if row is None else _comparison(row, _run_of(conn, row), reveal)


def list_comparisons(conn: sqlite3.Connection, limit: int = 50) -> list[Comparison]:
    """Newest first; the mapping is never included."""
    rows = _cursor(conn).execute(
        "SELECT * FROM comparisons ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    )
    return [_comparison(row, _run_of(conn, row), reveal=False) for row in rows.fetchall()]


def get_detail(conn: sqlite3.Connection, comparison_id: str, reveal: bool) -> CompareDetail | None:
    """The comparison with both texts and its run. With `reveal` and status done the mapping
    is included, and the first reveal is recorded (`revealed_at`)."""
    row = _row(conn, comparison_id)
    if row is None:
        return None
    run = _run_of(conn, row)
    comparison = _comparison(row, run, reveal)
    if comparison.mapping is not None and row["revealed_at"] is None:
        now = time.time()
        conn.execute("UPDATE comparisons SET revealed_at = ? WHERE id = ?", (now, row["id"]))
        comparison.revealed_at = now
    text = runstore.get_run_text(conn, row["run_id"]) if row["run_id"] else None
    return CompareDetail(
        comparison=comparison,
        before_text=row["before_text"],
        after_text=row["after_text"],
        run=run,
        version_a=row["version_a"],
        version_b=row["version_b"],
        raw_output=text.raw_output if text is not None else None,
    )


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Comparisons a previous server process left unfinished become failed."""
    cur = conn.execute(
        "UPDATE comparisons SET status = 'failed', error = 'interrupted: the server stopped "
        "during the comparison', finished_at = ? WHERE status IN ('queued', 'running')",
        (time.time(),),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- planning


@dataclass
class Prepared:
    comparison_id: str
    request: CompareRequest
    file: str
    before_kind: str
    before_ref: str
    before_sha: str
    before: Located
    after: Located
    version_a: str
    version_b: str
    mapping: Mapping
    plan: RunPlan


def _spec(repo: Repo) -> PaperSpec:
    spec, _, errors = load_spec(repo)
    if spec is None:
        first = f": {errors[0].message}" if errors else ""
        raise CompareError(409, f"the spec file is missing or invalid; fix it first{first}")
    return spec


def _outline_node(repo: Repo, spec: PaperSpec, file: str, line: int) -> str | None:
    """Without a node id: the spec paragraph whose marker sits on the paragraph at file:line
    in the working tree, so the model still sees the local message."""
    try:
        scan = skeleton.scan_document(repo.root, repo.main)
    except ConfigError:
        return None
    paragraph = scan.paragraph_at(file, line)
    if paragraph is None or paragraph.marker_id is None:
        return None
    found = find_tree_node(spec, paragraph.marker_id)
    return paragraph.marker_id if found is not None and found[0].level == "paragraph" else None


def _before_commit(repo: Repo, before: Before) -> str:
    try:
        if before.kind == "snapshot":
            return snapshots.resolve(repo, before.ref)[1]
        return gitops.resolve_commit(repo.root, before.ref)
    except GitError as exc:
        raise CompareError(400, exc.detail) from exc


def prepare(
    repo: Repo,
    runs: RunManager,
    request: CompareRequest,
    *,
    flip: bool | None = None,
) -> Prepared:
    """Everything up to the model call (blocking): both texts, the random order, the prompt
    and the run plan. CompareError 400 when the paragraph cannot be found on either side.
    `flip` forces the order (tests); by default it is random."""
    try:
        file = normalize_rel(request.file.strip())
        if not request.file.strip() or is_protected_for_write(file):
            raise CompareError(400, f"{request.file!r} is not a source file of the paper")
    except PathEscapeError as exc:
        raise CompareError(400, str(exc)) from exc
    line_range: tuple[int, int] | None = None
    if request.line_start is not None or request.line_end is not None:
        if request.line_start is None or request.line_end is None:
            raise CompareError(400, "give both line_start and line_end, or neither")
        if request.line_start < 1 or request.line_end < request.line_start:
            raise CompareError(400, f"not a line range: {request.line_start}-{request.line_end}")
        line_range = (request.line_start, request.line_end)
    node_id = (request.node_id or "").strip() or None
    if node_id is None and line_range is None:
        raise CompareError(400, "give node_id (a paragraph) or line_start and line_end")
    if gitops.locate(repo.root) is None:
        raise CompareError(400, f"{repo.root} is not a git repository: there is no 'before'")

    spec = _spec(repo)
    if node_id is not None:
        found = find_tree_node(spec, node_id)
        if found is None:
            raise CompareError(400, f"unknown spec node {node_id!r}")
        if found[0].level != "paragraph":
            raise CompareError(400, f"{node_id!r} is a {found[0].level}, not a paragraph")

    before_sha = _before_commit(repo, request.before)
    try:
        old_text = gitops.show(repo.root, before_sha, file)
    except GitError as exc:
        raise CompareError(
            400, f"{file} cannot be read at {request.before.ref}: {exc.detail}"
        ) from exc
    before = locate(source_lines(old_text), node_id, line_range)
    if before is None:
        raise CompareError(
            400,
            f"the paragraph cannot be found at {request.before.ref}: "
            f"{_describe_target(node_id, line_range)} in {file}",
        )
    try:
        new_text, _ = read_text_file(repo.root, file)
    except FileNotFoundError as exc:
        raise CompareError(400, f"{file} does not exist in the working tree") from exc
    except (PathEscapeError, NotTextError, TooLargeError, IsADirectoryError) as exc:
        raise CompareError(400, f"cannot read {file}: {exc}") from exc
    after = locate(source_lines(new_text), node_id, line_range)
    if after is None:
        raise CompareError(
            400,
            f"the paragraph cannot be found in the working tree: "
            f"{_describe_target(node_id, line_range)} in {file}",
        )

    shown_before = prompts.compare_text(before.text)
    shown_after = prompts.compare_text(after.text)
    if " ".join(shown_before.split()) == " ".join(shown_after.split()):
        raise CompareError(
            400, "the two versions are the same text (comments and line breaks aside)"
        )
    before_first = secrets.randbelow(2) == 0 if flip is None else not flip
    mapping = Mapping(A="before", B="after") if before_first else Mapping(A="after", B="before")
    version_a, version_b = (
        (shown_before, shown_after) if before_first else (shown_after, shown_before)
    )

    outline_node = node_id or _outline_node(repo, spec, file, after.line_start)
    target = (
        f"paragraph {outline_node}"
        if outline_node
        else f"lines {after.line_start}-{after.line_end}"
    )
    scope_name = f"{target} of {file}"
    scope = Scope(kind="selection", file=file, line_start=after.line_start, line_end=after.line_end)
    comparison_id = new_id()
    try:
        config = repo.load_config()
        _, spec_text, _ = load_spec(repo)
        assembled = prompts.assemble_compare_prompt(
            repo,
            spec=spec,
            spec_text=spec_text,
            node_id=outline_node,
            scope=scope,
            scope_name=scope_name,
            version_a=version_a,
            version_b=version_b,
            answer_schema=json.dumps(VERDICT_SCHEMA, indent=2),
        )
        plan = runs.plan_assembled(
            repo,
            config,
            assembled,
            request.runner,
            request.model,
            request.thinking,
            schema=VERDICT_SCHEMA,
            scope_key_override=f"{prompts.COMPARE_PASS}:{comparison_id}",
            cacheable=False,
        )
    except (PromptError, ConfigError) as exc:
        raise CompareError(400, str(exc)) from exc
    except RunError as exc:
        raise CompareError(exc.status, exc.detail) from exc
    try:
        runs.check_available(plan)
    except RunError as exc:
        raise CompareError(exc.status, exc.detail) from exc
    plan.interpret = parse_verdict
    return Prepared(
        comparison_id=comparison_id,
        request=request,
        file=file,
        before_kind=request.before.kind,
        before_ref=request.before.ref.strip(),
        before_sha=before_sha,
        before=before,
        after=after,
        version_a=version_a,
        version_b=version_b,
        mapping=mapping,
        plan=plan,
    )


# --------------------------------------------------------------------------- manager


class CompareManager:
    """Starts comparisons on the shared RunManager and records their verdicts."""

    def __init__(self, bus: EventBus, runs: RunManager) -> None:
        self.bus = bus
        self.runs = runs

    async def on_repo_opened(self, repo: Repo) -> None:
        def work() -> None:
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    count = mark_interrupted(conn)
                if count:
                    log.info("marked %d interrupted comparisons as failed", count)

        try:
            await asyncio.to_thread(work)
        except Exception:  # start-up must not fail on state.db
            log.exception("comparison start-up work failed")

    async def start(
        self, repo: Repo, request: CompareRequest, *, flip: bool | None = None
    ) -> Comparison:
        prepared = await asyncio.to_thread(prepare, repo, self.runs, request, flip=flip)
        comparison_id = prepared.comparison_id

        def on_created(conn: sqlite3.Connection, run: Run) -> None:
            conn.execute(
                "INSERT INTO comparisons (id, file, node_id, line_start, line_end, before_kind, "
                "before_ref, before_sha, run_id, status, mapping_json, before_text, after_text, "
                "version_a, version_b, before_line_start, before_line_end, after_line_start, "
                "after_line_end, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    comparison_id,
                    prepared.file,
                    (request.node_id or "").strip() or None,
                    request.line_start,
                    request.line_end,
                    prepared.before_kind,
                    prepared.before_ref,
                    prepared.before_sha,
                    run.id,
                    "queued",
                    prepared.mapping.model_dump_json(),
                    prepared.before.text,
                    prepared.after.text,
                    prepared.version_a,
                    prepared.version_b,
                    prepared.before.line_start,
                    prepared.before.line_end,
                    prepared.after.line_start,
                    prepared.after.line_end,
                    time.time(),
                ),
            )

        prepared.plan.on_finished = lambda run, data: self._finished(repo, comparison_id, run, data)
        try:
            await self.runs.launch(repo, prepared.plan, use_cache=False, on_created=on_created)
        except RunError as exc:
            raise CompareError(exc.status, exc.detail) from exc

        def read() -> Comparison:
            with db.connect(repo) as conn:
                comparison = get_comparison(conn, comparison_id)
            assert comparison is not None
            return comparison

        return await asyncio.to_thread(read)

    def _finished(self, repo: Repo, comparison_id: str, run: Run, data: Any) -> None:
        """The run ended (done, failed or cancelled): record the verdict or the error and
        publish `compare.finished` (without the mapping)."""
        now = time.time()
        if run.status == "done" and isinstance(data, VerdictAnswer):
            values: dict[str, Any] = {
                "status": "done",
                "verdict": data.verdict,
                "reason": data.reason,
                "error": None,
            }
        else:
            values = {"status": "failed", "error": run.error or f"the run ended {run.status}"}
        values["finished_at"] = now
        assignments = ", ".join(f"{column} = ?" for column in values)
        with db.connect(repo) as conn:
            conn.execute(
                f"UPDATE comparisons SET {assignments} WHERE id = ?",
                [*values.values(), comparison_id],
            )
            comparison = get_comparison(conn, comparison_id)
        if comparison is not None:
            self.bus.publish(CompareFinished(comparison=comparison.model_dump(mode="json")))
