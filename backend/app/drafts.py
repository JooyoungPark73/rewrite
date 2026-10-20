"""Drafting from the spec (phase 7): one paragraph node → a LaTeX draft the author reviews.

Separate from review (PLAN.md, "Decided 2026-10-20"): its own prompt stack
(`prompts/drafts/`), its own answer `{draft, notes}`, never cached, never a finding. The model
call writes nothing; `apply` writes the `.tex` only when the author asks, under the marker and
after a `% drafted by` comment, so a replaced paragraph comes up stale and is re-read like any
edit. This module never writes the spec file (tests/test_invariants.py); re-linking an
unanchored node after an insert goes through the spec API's `adopt`, called by the frontend.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app import db, prompts, runstore, skeleton
from app.config import ConfigError
from app.events import DraftFinished, EventBus
from app.files import normalize_rel, read_text_file, write_text_file
from app.prompts import PromptError
from app.repo import Repo
from app.runner import RunResult
from app.runs import RunError, RunManager, RunOutcome, RunPlan
from app.runstore import Run
from app.spec import find_tree_node, load_spec, paragraph_marker_id

log = logging.getLogger(__name__)

# How the model must answer (no JSON: string escapes would mangle `\ref`, `\textsc`, ...).
DRAFT_FORMAT = (
    "Return the paragraph as LaTeX inside one fenced code block (```latex ... ```), and after "
    "the block an optional line starting with `Notes:` for the authors (what the spec left "
    "open, every \\todo you wrote, a claim kept narrower than asked). Nothing else: no "
    "preamble, no second block."
)

DraftStatus = Literal["queued", "running", "done", "failed"]
ApplyMode = Literal["replace", "insert"]


class DraftError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    runner: str | None = None
    model: str | None = None
    thinking: str | None = None


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ApplyMode
    # insert only: where the new paragraph goes (the line it is inserted before, 1-based).
    file: str | None = None
    line: int | None = None
    # The text to write: the author's edited version of the draft (default: the draft itself).
    text: str | None = None


class Draft(BaseModel):
    id: str
    node_id: str
    run_id: str | None
    status: DraftStatus
    text: str | None
    notes: str | None
    error: str | None
    created_at: float
    finished_at: float | None
    applied_at: float | None
    applied_mode: str | None
    applied_file: str | None
    applied_line_start: int | None
    applied_line_end: int | None


class DraftDetail(BaseModel):
    draft: Draft
    run: Run | None


class Applied(BaseModel):
    draft: Draft
    file: str
    line_start: int
    line_end: int


@dataclass
class DraftAnswer:
    text: str
    notes: str


# --------------------------------------------------------------------------- parsing

_FORBIDDEN_RE = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\s*\{|^\s*%\s*@p\s|\\begin\{document\}|\\end\{document\}",
    re.M,
)
_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z]*[ \t]*\r?\n(.*?)\r?\n?```", re.S)
_NOTES_RE = re.compile(r"^\s*notes?\s*:\s*(.*)$", re.I | re.S)


def parse_draft(result: RunResult) -> RunOutcome:
    """The answer is one fenced block holding the LaTeX paragraph, optionally followed by a
    `Notes:` line. Exactly one block with a non-empty paragraph that is one paragraph (no
    heading, no `% @p` line, no document environment) → done with a DraftAnswer; anything
    else → failed (the raw answer stays on the run)."""
    text = result.text or ""
    blocks = _FENCE_RE.findall(text)
    if not blocks:
        return RunOutcome(
            status="failed",
            error="could not find the draft: the answer has no fenced ```latex block",
        )
    if len(blocks) > 1:
        return RunOutcome(
            status="failed", error=f"the answer has {len(blocks)} fenced blocks; one was asked for"
        )
    draft = blocks[0].strip("\n")
    if not draft.strip():
        return RunOutcome(status="failed", error="the fenced block is empty")
    if _FORBIDDEN_RE.search(draft):
        return RunOutcome(
            status="failed",
            error=(
                "rejected: the draft contains a heading, a `% @p` line or a document "
                "environment (one paragraph was asked for)"
            ),
        )
    tail = text[text.rfind("```") + 3 :]
    notes = ""
    m = _NOTES_RE.match(tail.strip())
    if m:
        notes = " ".join(m.group(1).split())
    return RunOutcome(status="done", data=DraftAnswer(text=draft, notes=notes))


# --------------------------------------------------------------------------- rows


def new_id() -> str:
    return "drf" + secrets.token_hex(8)


def _draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        node_id=row["node_id"],
        run_id=row["run_id"],
        status=row["status"],
        text=row["text"],
        notes=row["notes"],
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        applied_at=row["applied_at"],
        applied_mode=row["applied_mode"],
        applied_file=row["applied_file"],
        applied_line_start=row["applied_line_start"],
        applied_line_end=row["applied_line_end"],
    )


def _row(conn: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def get_draft(conn: sqlite3.Connection, draft_id: str) -> Draft | None:
    row = _row(conn, draft_id)
    return None if row is None else _draft(row)


def list_drafts(
    conn: sqlite3.Connection, limit: int = 50, node_id: str | None = None
) -> list[Draft]:
    conn.row_factory = sqlite3.Row
    if node_id is not None:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE node_id = ? ORDER BY created_at DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_draft(r) for r in rows]


def get_detail(conn: sqlite3.Connection, draft_id: str) -> DraftDetail | None:
    row = _row(conn, draft_id)
    if row is None:
        return None
    run = runstore.get_run(conn, row["run_id"]) if row["run_id"] else None
    return DraftDetail(draft=_draft(row), run=run)


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Drafts left queued/running by a previous process are failed (the run is gone)."""
    cur = conn.execute(
        "UPDATE drafts SET status = 'failed', error = 'interrupted: the server stopped before the "
        "draft finished', finished_at = ? WHERE status IN ('queued', 'running')",
        (time.time(),),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- preparing


@dataclass
class Prepared:
    draft_id: str
    node_id: str
    plan: RunPlan


def prepare(repo: Repo, runs: RunManager, request: DraftRequest) -> Prepared:
    node_id = request.node_id.strip()
    if not node_id:
        raise DraftError(400, "node_id is empty")
    spec, _, errors = load_spec(repo)
    if spec is None:
        raise DraftError(400, "the spec file is missing or invalid; fix it in the Spec view")
    found = find_tree_node(spec, node_id)
    if found is None:
        raise DraftError(404, f"unknown node {node_id!r}")
    if found[0].level != "paragraph":
        raise DraftError(400, f"{node_id} is a {found[0].level}; drafts target paragraphs")
    draft_id = new_id()
    try:
        config = repo.load_config()
        assembled = prompts.assemble_draft_prompt(repo, node_id, DRAFT_FORMAT)
        plan = runs.plan_assembled(
            repo,
            config,
            assembled,
            request.runner,
            request.model,
            request.thinking,
            scope_key_override=f"draft:{node_id}",
            cacheable=False,
            free_text=True,
        )
    except (PromptError, ConfigError) as exc:
        raise DraftError(400, str(exc)) from exc
    except RunError as exc:
        raise DraftError(exc.status, exc.detail) from exc
    try:
        runs.check_available(plan)
    except RunError as exc:
        raise DraftError(exc.status, exc.detail) from exc
    plan.interpret = parse_draft
    return Prepared(draft_id=draft_id, node_id=node_id, plan=plan)


# --------------------------------------------------------------------------- applying


def _drafted_comment(run: Run | None, draft_id: str) -> str:
    who = run.runner if run else "a model"
    model = f" ({run.model})" if run and run.model else ""
    return f"% drafted by {who}{model}, draft {draft_id}: review before keeping"


def apply(repo: Repo, draft_id: str, request: ApplyRequest) -> Applied:
    """Write the draft (or the author's edit of it, `request.text`) into the `.tex`.

    replace: the node is anchored and its marker is found; the paragraph under the marker is
    replaced by the comment line and the text. insert: at `file`:`line` (before that line),
    the `% @p <id>` marker line, the comment and the text, then a blank line; the node must
    not be anchored to a marker that exists (that would duplicate it). The caller re-anchors an
    unanchored node through the spec API's adopt afterwards."""
    with db.connect(repo) as conn:
        detail = get_detail(conn, draft_id)
    if detail is None:
        raise DraftError(404, f"unknown draft {draft_id!r}")
    draft = detail.draft
    if draft.status != "done" or not draft.text:
        raise DraftError(
            409, f"draft {draft_id} is {draft.status}; only a finished draft can be applied"
        )
    if draft.applied_at is not None:
        raise DraftError(409, f"draft {draft_id} was already applied ({draft.applied_mode})")
    text = (request.text if request.text is not None else draft.text).strip("\n")
    if not text.strip():
        raise DraftError(400, "the text to insert is empty")
    spec, _, _ = load_spec(repo)
    if spec is None:
        raise DraftError(400, "the spec file is missing or invalid")
    found = find_tree_node(spec, draft.node_id)
    if found is None:
        raise DraftError(404, f"node {draft.node_id} no longer exists")
    node = found[0]
    try:
        scan = skeleton.scan_document(repo.root, repo.main)
    except ConfigError as exc:
        raise DraftError(400, str(exc)) from exc
    marker_id = paragraph_marker_id(node) if node.anchor else None
    marker = (
        skeleton.find_marker(scan, marker_id, node.anchor.file)
        if marker_id and node.anchor
        else None
    )
    comment = _drafted_comment(detail.run, draft_id)
    new_lines = text.split("\n")

    if request.mode == "replace":
        if marker is None:
            raise DraftError(
                400, f"{draft.node_id} has no marker in the source; use insert with a file and line"
            )
        lines = scan.lines.get(marker.file, [])
        span = skeleton.marker_paragraph(lines, marker_id or "")
        if span is None:
            raise DraftError(
                400, f"the paragraph under the marker of {draft.node_id} could not be located"
            )
        _, first, last = span
        before = lines[: first - 1]
        after = lines[last:]
        updated = [*before, comment, *new_lines, *after]
        file = marker.file
        line_start, line_end = first + 1, first + len(new_lines)  # the comment sits at `first`
    else:
        if marker is not None:
            raise DraftError(
                400, f"{draft.node_id} already has a marker in {marker.file}; use replace"
            )
        if not request.file or request.line is None or request.line < 1:
            raise DraftError(400, "insert needs a file and a 1-based line")
        file = normalize_rel(request.file)
        try:
            content, _ = read_text_file(repo.root, file)
        except FileNotFoundError as exc:
            raise DraftError(404, f"file not found: {file}") from exc
        lines = content.split("\n")
        at = min(request.line - 1, len(lines))
        block = [skeleton.marker_line(draft.node_id), comment, *new_lines, ""]
        if at > 0 and lines[at - 1].strip():
            block = ["", *block]
        updated = [*lines[:at], *block, *lines[at:]]
        offset = at + (1 if block[0] == "" else 0)
        line_start, line_end = offset + 3, offset + 2 + len(new_lines)
    joined = "\n".join(updated)
    if not joined.endswith("\n"):
        joined += "\n"
    write_text_file(repo.root, file, joined)
    now = time.time()
    with db.connect(repo) as conn:
        conn.execute(
            "UPDATE drafts SET applied_at = ?, applied_mode = ?, applied_file = ?, "
            "applied_line_start = ?, applied_line_end = ? WHERE id = ?",
            (now, request.mode, file, line_start, line_end, draft_id),
        )
        updated_draft = get_draft(conn, draft_id)
    assert updated_draft is not None
    return Applied(draft=updated_draft, file=file, line_start=line_start, line_end=line_end)


# --------------------------------------------------------------------------- manager


class DraftManager:
    """Starts drafts on the shared RunManager and records their answers."""

    def __init__(self, bus: EventBus, runs: RunManager) -> None:
        self.bus = bus
        self.runs = runs

    async def on_repo_opened(self, repo: Repo) -> None:
        def work() -> None:
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    count = mark_interrupted(conn)
                if count:
                    log.info("marked %d interrupted drafts as failed", count)

        try:
            await asyncio.to_thread(work)
        except Exception:  # start-up must not fail on state.db
            log.exception("draft start-up work failed")

    async def start(self, repo: Repo, request: DraftRequest) -> Draft:
        prepared = await asyncio.to_thread(prepare, repo, self.runs, request)
        draft_id = prepared.draft_id

        def on_created(conn: sqlite3.Connection, run: Run) -> None:
            conn.execute(
                "INSERT INTO drafts (id, node_id, run_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, prepared.node_id, run.id, "queued", time.time()),
            )

        prepared.plan.on_finished = lambda run, data: self._finished(repo, draft_id, run, data)
        try:
            await self.runs.launch(repo, prepared.plan, use_cache=False, on_created=on_created)
        except RunError as exc:
            raise DraftError(exc.status, exc.detail) from exc

        def read() -> Draft:
            with db.connect(repo) as conn:
                draft = get_draft(conn, draft_id)
            assert draft is not None
            return draft

        return await asyncio.to_thread(read)

    def _finished(self, repo: Repo, draft_id: str, run: Run, data: Any) -> None:
        now = time.time()
        if run.status == "done" and isinstance(data, DraftAnswer):
            values: dict[str, Any] = {
                "status": "done",
                "text": data.text,
                "notes": data.notes,
                "error": None,
            }
        else:
            values = {"status": "failed", "error": run.error or f"the run ended {run.status}"}
        values["finished_at"] = now
        assignments = ", ".join(f"{column} = ?" for column in values)
        with db.connect(repo) as conn:
            conn.execute(
                f"UPDATE drafts SET {assignments} WHERE id = ?", [*values.values(), draft_id]
            )
            draft = get_draft(conn, draft_id)
        if draft is not None:
            self.bus.publish(DraftFinished(draft=draft.model_dump(mode="json")))
