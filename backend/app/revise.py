"""Revise (phase 8): comments on the text, and rewrites proposed on them.

Two halves, both built on findings:

- An **author comment** is a finding whose author is the human: `add_comment` stores a run
  (runner "author", pass "comment", done at once) with one finding anchored by the quoted
  text, so comments get the sidebar card, the highlight, re-anchoring and the states that
  model findings have.
- A **revision** proposes a rewrite for one finding (a comment or a model finding): the
  span it quotes, or the whole paragraph around it. The model answers in a fenced LaTeX block
  (prompts/drafts/rewrite.md, the drafting stack); nothing is written by the call. `apply`
  writes the author's approved text wrapped as `\\aigen{...}` (per paragraph), keeps the
  original on a `% aigen-was:` comment line above it, makes sure main.tex defines the macro,
  and marks the finding addressed. Co-authors accept by removing the wrapper (in Overleaf or
  here). This module never writes the spec file (tests/test_invariants.py).
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

from pydantic import BaseModel, ConfigDict, Field

from app import db, findings, prompts, runstore, skeleton
from app.config import ConfigError
from app.events import EventBus, FindingsUpdated, RevisionFinished, RunFinished
from app.files import (
    normalize_rel,
    read_text_file,
    resolve_tex_tree,
    strip_tex_comment,
    write_text_file,
)
from app.prompts import PromptError
from app.repo import Repo
from app.runner import RunResult
from app.runs import RunError, RunManager, RunOutcome, RunPlan
from app.runstore import Run
from app.spec import iter_tree, load_spec

log = logging.getLogger(__name__)

AUTHOR = "author"  # the runner name of a comment's run
COMMENT_PASS = "comment"

AIGEN_MACRO = r"\providecommand{\aigen}[1]{{\textcolor{red}{[~AIGEN:~#1~]}}}"
AIGEN_PREAMBLE = (
    "% rewrite: text a model proposed on a comment, awaiting co-author approval. Accept a\n"
    '% proposal by removing its \\aigen{...} wrapper and the "% aigen-was:" line above it.\n'
    "% For a clean build with every proposal shown as plain text, uncomment the last line.\n"
    "{xcolor}" + AIGEN_MACRO + "\n% \\renewcommand{\\aigen}[1]{#1}\n"
)
WAS_PREFIX = "% aigen-was: "

# How the model must answer (the drafting stack's fenced block; see app/drafts.py).
REWRITE_FORMAT = (
    "Return the replacement inside one fenced code block (```latex ... ```), and after the "
    "block an optional line starting with `Notes:` for the authors. Nothing else. An empty "
    "block means the span should be dropped."
)

SpanKind = Literal["quote", "paragraph"]
RevisionStatus = Literal["queued", "running", "done", "failed"]


class ReviseError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- comments


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    # The selected text (whitespace-collapsed match anchors the comment; may span lines).
    quote: str
    # The comment itself ("too verbose", "these two sentences disconnect; add a bridge").
    problem: str
    why: str = ""
    # The spec node the span belongs to (the nearest `% @p` above), or null.
    json_ref: str | None = None
    severity: findings.Severity = "major"


def _level_of(repo: Repo, json_ref: str | None) -> str:
    if json_ref:
        spec, _, _ = load_spec(repo)
        if spec is not None:
            for node, _, _ in iter_tree(spec):
                if node.id == json_ref:
                    return node.level
    return "sentence"


def add_comment(repo: Repo, request: CommentRequest) -> findings.Finding:
    """Store an author comment as a finding of a done run (runner "author")."""
    file = normalize_rel(request.file.strip())
    if not request.problem.strip():
        raise ReviseError(400, "the comment is empty")
    if request.line_end < request.line_start:
        raise ReviseError(400, f"not a line range: {request.line_start}-{request.line_end}")
    try:
        text, _ = read_text_file(repo.root, file)
    except FileNotFoundError as exc:
        raise ReviseError(404, f"file not found: {file}") from exc
    lines = text.split("\n")
    anchor = findings.anchor_new(request.quote, lines, request.line_start, request.line_end)
    level = _level_of(repo, request.json_ref)
    run_id = runstore.new_run_id()
    now = time.time()
    scope = prompts.Scope(
        kind="selection", file=file, line_start=request.line_start, line_end=request.line_end
    )
    row: dict[str, Any] = {
        "id": run_id,
        "pass": COMMENT_PASS,
        "level": level,
        "scope_kind": "selection",
        "scope_ref": file,
        "scope_json": scope.model_dump_json(),
        "scope_name": f"comment on {file}:{request.line_start}"
        + (f"-{request.line_end}" if request.line_end != request.line_start else ""),
        "scope_key": f"{COMMENT_PASS}:{run_id}",
        "runner": AUTHOR,
        "model": None,
        "thinking": None,
        "allow_rewrites": 0,
        "git_sha": None,
        "prompt_hash": "",
        "scope_hash": "",
        "spec_hash": "",
        "cache_key": None,
        "prompt": "",
        "command_json": "[]",
        "status": "done",
        "created_at": now,
        "started_at": now,
    }
    finding_row = {
        "id": runstore.finding_id(run_id, 0),
        "run_id": run_id,
        "idx": 0,
        "file": file,
        "line_start": request.line_start,
        "line_end": request.line_end,
        "quote": " ".join(request.quote.split()),
        "level": level,
        "category": COMMENT_PASS,
        "severity": request.severity,
        "problem": request.problem.strip(),
        "why": request.why.strip(),
        "json_ref": request.json_ref,
        "anchor_state": anchor.state,
        "anchor_line_start": anchor.line_start,
        "anchor_line_end": anchor.line_end,
        "origin_line_start": anchor.line_start,
        "origin_line_end": anchor.line_end,
    }
    with db.connect(repo) as conn, db.transaction(conn):
        runstore.insert_run(conn, row)
        runstore.update_run(conn, run_id, finished_at=now, duration_ms=0)
        runstore.insert_findings(conn, [finding_row])
    with db.connect(repo) as conn:
        finding = runstore.get_finding(conn, finding_row["id"])
    assert finding is not None
    return finding


# --------------------------------------------------------------------------- revisions


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    # What the rewrite replaces: the finding's quoted span, or the whole paragraph around it.
    span: SpanKind = "quote"
    runner: str | None = None
    model: str | None = None
    thinking: str | None = None


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The author's approved text (default: the proposal). Empty = drop the span.
    text: str | None = None


class Revision(BaseModel):
    id: str
    finding_id: str
    run_id: str | None
    status: RevisionStatus
    span: SpanKind
    file: str
    line_start: int
    line_end: int
    original: str
    text: str | None
    notes: str | None
    error: str | None
    created_at: float
    finished_at: float | None
    applied_at: float | None
    applied_file: str | None
    applied_line_start: int | None
    applied_line_end: int | None


class RevisionDetail(BaseModel):
    revision: Revision
    run: Run | None
    finding: findings.Finding | None


class Applied(BaseModel):
    revision: Revision
    file: str
    line_start: int
    line_end: int
    # `\\aigen{...}` wrappers now in the paper (all files), after this write.
    outstanding: int


class Outstanding(BaseModel):
    count: int
    by_file: dict[str, int]


@dataclass
class RewriteAnswer:
    text: str
    notes: str


_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z]*[ \t]*\r?\n(.*?)\r?\n?```", re.S)
_NOTES_RE = re.compile(r"^\s*notes?\s*:\s*(.*)$", re.I | re.S)
_FORBIDDEN_RE = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\s*\{|^\s*%\s*@p\s|\\begin\{document\}|\\end\{document\}",
    re.M,
)


def parse_rewrite(result: RunResult) -> RunOutcome:
    """One fenced block (possibly empty: drop the span) plus an optional `Notes:` line."""
    text = result.text or ""
    blocks = _FENCE_RE.findall(text)
    if not blocks:
        # An empty block has no inner newline for the regex; accept ```latex\n``` too.
        if re.search(r"```[ \t]*[A-Za-z]*[ \t]*\r?\n```", text):
            blocks = [""]
        else:
            return RunOutcome(
                status="failed",
                error="could not find the rewrite: the answer has no fenced ```latex block",
            )
    if len(blocks) > 1:
        return RunOutcome(
            status="failed", error=f"the answer has {len(blocks)} fenced blocks; one was asked for"
        )
    draft = blocks[0].strip("\n")
    if _FORBIDDEN_RE.search(draft):
        return RunOutcome(
            status="failed",
            error=(
                "rejected: the rewrite contains a heading, a `% @p` line or a document "
                "environment (a span of one paragraph was asked for)"
            ),
        )
    tail = text[text.rfind("```") + 3 :]
    notes = ""
    m = _NOTES_RE.match(tail.strip())
    if m:
        notes = " ".join(m.group(1).split())
    return RunOutcome(status="done", data=RewriteAnswer(text=draft, notes=notes))


def new_id() -> str:
    return "rev" + secrets.token_hex(8)


def _revision(row: sqlite3.Row) -> Revision:
    return Revision(**{k: row[k] for k in Revision.model_fields})


def get_revision(conn: sqlite3.Connection, revision_id: str) -> Revision | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    return None if row is None else _revision(row)


def list_revisions(
    conn: sqlite3.Connection, limit: int = 50, finding_id: str | None = None
) -> list[Revision]:
    conn.row_factory = sqlite3.Row
    if finding_id is not None:
        rows = conn.execute(
            "SELECT * FROM revisions WHERE finding_id = ? ORDER BY created_at DESC LIMIT ?",
            (finding_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM revisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_revision(r) for r in rows]


def get_detail(conn: sqlite3.Connection, revision_id: str) -> RevisionDetail | None:
    revision = get_revision(conn, revision_id)
    if revision is None:
        return None
    run = runstore.get_run(conn, revision.run_id) if revision.run_id else None
    finding = runstore.get_finding(conn, revision.finding_id)
    return RevisionDetail(revision=revision, run=run, finding=finding)


def mark_interrupted(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE revisions SET status = 'failed', error = 'interrupted: the server stopped before "
        "the rewrite finished', finished_at = ? WHERE status IN ('queued', 'running')",
        (time.time(),),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- the span


@dataclass
class Span:
    file: str
    line_start: int  # 1-based, inclusive
    line_end: int
    # For a quote span: character offsets of the quote inside the joined lines; None for a
    # whole-paragraph span.
    char_start: int | None
    char_end: int | None
    original: str


def _joined(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end])


def locate_span(repo: Repo, finding: findings.Finding, span: SpanKind) -> Span:
    """Where the rewrite goes, as the file reads now: the quoted text inside the finding's
    anchor lines, or the paragraph those lines sit in (by the `% @p` rule when the paragraph
    has a marker, else the skeleton's paragraph)."""
    file = finding.file
    try:
        text, _ = read_text_file(repo.root, file)
    except FileNotFoundError as exc:
        raise ReviseError(404, f"file not found: {file}") from exc
    lines = text.split("\n")
    if finding.anchor_state == "stale":
        raise ReviseError(
            400,
            "the finding's quote is no longer in the file (stale); re-read the text and add a "
            "fresh comment on the current wording",
        )
    a, b = finding.anchor_line_start, finding.anchor_line_end
    if a < 1 or b > len(lines) or b < a:
        raise ReviseError(400, f"the finding's lines {a}-{b} are outside {file}")
    if span == "paragraph":
        try:
            scan = skeleton.scan_document(repo.root, repo.main)
        except ConfigError as exc:
            raise ReviseError(400, str(exc)) from exc
        paragraph = scan.paragraph_at(file, a)
        if paragraph is None:
            raise ReviseError(400, f"no paragraph found around {file}:{a}")
        start, end = paragraph.line_start, paragraph.line_end
        return Span(file, start, end, None, None, _joined(lines, start, end))
    joined = _joined(lines, a, b)
    hit = _find_collapsed(joined, finding.quote)
    if hit is None:
        # The anchor says ok but the text moved within the lines: fall back to the lines.
        return Span(file, a, b, None, None, joined)
    return Span(file, a, b, hit[0], hit[1], joined[hit[0] : hit[1]])


def _find_collapsed(haystack: str, needle: str) -> tuple[int, int] | None:
    """(start, end) offsets in `haystack` of `needle` matched with whitespace collapsed."""
    want = " ".join(needle.split())
    if not want:
        return None
    # Map every non-space run of the haystack to its offsets.
    positions: list[int] = []
    collapsed_chars: list[str] = []
    prev_space = True
    for i, ch in enumerate(haystack):
        if ch.isspace():
            if not prev_space:
                collapsed_chars.append(" ")
                positions.append(i)
            prev_space = True
        else:
            collapsed_chars.append(ch)
            positions.append(i)
            prev_space = False
    collapsed = "".join(collapsed_chars).strip()
    offset = len("".join(collapsed_chars)) - len("".join(collapsed_chars).lstrip())
    at = collapsed.find(want)
    if at < 0:
        return None
    start = positions[at + offset]
    end = positions[at + offset + len(want) - 1] + 1
    return start, end


# --------------------------------------------------------------------------- preparing


@dataclass
class Prepared:
    revision_id: str
    finding: findings.Finding
    span: Span
    span_kind: SpanKind
    plan: RunPlan


SPAN_WORDS = {"quote": "the quoted span", "paragraph": "the whole paragraph"}


def prepare(repo: Repo, runs: RunManager, request: RevisionRequest) -> Prepared:
    with db.connect(repo) as conn:
        finding = runstore.get_finding(conn, request.finding_id.strip())
    if finding is None:
        raise ReviseError(404, f"unknown finding {request.finding_id!r}")
    span = locate_span(repo, finding, request.span)
    revision_id = new_id()
    try:
        config = repo.load_config()
        assembled = prompts.assemble_rewrite_prompt(
            repo,
            comment=finding.problem,
            comment_why=finding.why,
            original=span.original,
            span_kind=SPAN_WORDS[request.span],
            file=span.file,
            line=span.line_start,
            node_id=finding.json_ref,
            answer_format=REWRITE_FORMAT,
        )
        plan = runs.plan_assembled(
            repo,
            config,
            assembled,
            request.runner,
            request.model,
            request.thinking,
            scope_key_override=f"rewrite:{finding.id}",
            cacheable=False,
            free_text=True,
        )
    except (PromptError, ConfigError) as exc:
        raise ReviseError(400, str(exc)) from exc
    except RunError as exc:
        raise ReviseError(exc.status, exc.detail) from exc
    try:
        runs.check_available(plan)
    except RunError as exc:
        raise ReviseError(exc.status, exc.detail) from exc
    plan.interpret = parse_rewrite
    return Prepared(revision_id, finding, span, request.span, plan)


# --------------------------------------------------------------------------- applying


def _wrap(text: str) -> str:
    """`\\aigen{...}` around each paragraph of `text` (paragraphs separated by blank lines)."""
    paragraphs = [p.strip("\n") for p in re.split(r"\n\s*\n", text.strip("\n")) if p.strip()]
    return "\n\n".join(f"\\aigen{{{p}}}" for p in paragraphs)


def _active(text: str) -> str:
    """`text` with LaTeX comments removed, line by line."""
    return "\n".join(strip_tex_comment(raw) for raw in text.split("\n"))


def ensure_preamble(repo: Repo) -> bool:
    """Define `\\aigen` in the main file when no file of the include tree defines it.
    Returns True when main.tex was changed."""
    try:
        tree = resolve_tex_tree(repo.root, repo.main)
    except ConfigError as exc:
        raise ReviseError(400, str(exc)) from exc
    defined = re.compile(r"\\(?:newcommand|providecommand|renewcommand|def)\s*\*?\s*\{?\\aigen\b")
    uses_color = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b(?:xcolor|color)\b[^}]*\}")
    texts: dict[str, str] = {}
    for entry in tree.files:
        if entry.kind != "tex":
            continue
        try:
            texts[entry.path], _ = read_text_file(repo.root, entry.path)
        except Exception:
            continue
    if any(defined.search(_active(t)) for t in texts.values()):
        return False
    main = tree.main
    text = texts.get(main)
    if text is None:
        raise ReviseError(400, f"cannot read {main}")
    color_line = (
        "" if any(uses_color.search(t) for t in texts.values()) else "\\usepackage{xcolor}\n"
    )
    block = AIGEN_PREAMBLE.replace("{xcolor}", color_line)
    lines = text.split("\n")
    at = next(
        (i for i, raw in enumerate(lines) if "\\begin{document}" in strip_tex_comment(raw)), None
    )
    if at is None:
        raise ReviseError(400, f"{main} has no \\begin{{document}}; add the \\aigen macro by hand")
    insert = block.rstrip("\n").split("\n")
    if at > 0 and lines[at - 1].strip():
        insert = ["", *insert]
    lines[at:at] = [*insert, ""]
    write_text_file(repo.root, main, "\n".join(lines))
    return True


def count_outstanding(repo: Repo) -> Outstanding:
    """`\\aigen{` wrappers in the paper's .tex files (comments ignored)."""
    try:
        tree = resolve_tex_tree(repo.root, repo.main)
    except ConfigError:
        return Outstanding(count=0, by_file={})
    by_file: dict[str, int] = {}
    for entry in tree.files:
        if entry.kind != "tex":
            continue
        try:
            text, _ = read_text_file(repo.root, entry.path)
        except Exception:
            continue
        n = sum(strip_tex_comment(raw).count("\\aigen{") for raw in text.split("\n"))
        if n:
            by_file[entry.path] = n
    return Outstanding(count=sum(by_file.values()), by_file=by_file)


def apply(repo: Repo, revision_id: str, request: ApplyRequest) -> Applied:
    """Write the approved text: `% aigen-was: <original>` above the span, the span replaced
    by `\\aigen{...}` per paragraph (or removed when the text is empty), the macro ensured in
    main.tex, the finding marked addressed."""
    with db.connect(repo) as conn:
        detail = get_detail(conn, revision_id)
    if detail is None:
        raise ReviseError(404, f"unknown revision {revision_id!r}")
    revision = detail.revision
    if revision.status != "done" or revision.text is None:
        raise ReviseError(
            409, f"revision {revision_id} is {revision.status}; only a finished one can be applied"
        )
    if revision.applied_at is not None:
        raise ReviseError(409, f"revision {revision_id} was already applied")
    if detail.finding is None:
        raise ReviseError(404, f"finding {revision.finding_id} no longer exists")
    new_text = (request.text if request.text is not None else revision.text).strip("\n")
    # Re-locate the span as the file reads now (it may have moved since the proposal).
    span = locate_span(repo, detail.finding, revision.span)
    if " ".join(span.original.split()) != " ".join(revision.original.split()):
        raise ReviseError(
            409,
            "the text changed since this rewrite was proposed; propose it again on the current "
            "wording",
        )
    text, _ = read_text_file(repo.root, span.file)
    lines = text.split("\n")
    was_line = WAS_PREFIX + " ".join(span.original.split())
    joined = _joined(lines, span.line_start, span.line_end)
    if span.char_start is not None and span.char_end is not None:
        replacement = _wrap(new_text) if new_text.strip() else ""
        new_joined = joined[: span.char_start] + replacement + joined[span.char_end :]
    else:
        new_joined = _wrap(new_text) if new_text.strip() else ""
    new_block = [was_line, *(new_joined.split("\n") if new_joined else [])]
    lines[span.line_start - 1 : span.line_end] = new_block
    updated = "\n".join(lines)
    if not updated.endswith("\n"):
        updated += "\n"
    write_text_file(repo.root, span.file, updated)
    if new_text.strip():
        ensure_preamble(repo)
    line_start = span.line_start + 1
    line_end = span.line_start + max(1, len(new_block) - 1)
    now = time.time()
    with db.connect(repo) as conn, db.transaction(conn):
        conn.execute(
            "UPDATE revisions SET applied_at = ?, applied_file = ?, applied_line_start = ?, "
            "applied_line_end = ? WHERE id = ?",
            (now, span.file, line_start, line_end, revision_id),
        )
        conn.execute(
            "UPDATE findings SET state = 'addressed', updated_at = ? "
            "WHERE id = ? AND state = 'open'",
            (now, revision.finding_id),
        )
        applied = get_revision(conn, revision_id)
    assert applied is not None
    return Applied(
        revision=applied,
        file=span.file,
        line_start=line_start,
        line_end=line_end,
        outstanding=count_outstanding(repo).count,
    )


# --------------------------------------------------------------------------- manager


class ReviseManager:
    def __init__(self, bus: EventBus, runs: RunManager) -> None:
        self.bus = bus
        self.runs = runs

    async def on_repo_opened(self, repo: Repo) -> None:
        def work() -> None:
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    count = mark_interrupted(conn)
                if count:
                    log.info("marked %d interrupted revisions as failed", count)

        try:
            await asyncio.to_thread(work)
        except Exception:
            log.exception("revision start-up work failed")

    async def comment(self, repo: Repo, request: CommentRequest) -> findings.Finding:
        finding = await asyncio.to_thread(add_comment, repo, request)
        with db.connect(repo) as conn:
            run = runstore.get_run(conn, finding.run_id)
        if run is not None:
            self.bus.publish(RunFinished(run=run.model_dump(mode="json", by_alias=True)))
        self.bus.publish(FindingsUpdated(run_id=finding.run_id, file=finding.file))
        return finding

    async def start(self, repo: Repo, request: RevisionRequest) -> Revision:
        prepared = await asyncio.to_thread(prepare, repo, self.runs, request)
        rid = prepared.revision_id

        def on_created(conn: sqlite3.Connection, run: Run) -> None:
            conn.execute(
                "INSERT INTO revisions (id, finding_id, run_id, status, span, file, line_start, "
                "line_end, original, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    prepared.finding.id,
                    run.id,
                    "queued",
                    prepared.span_kind,
                    prepared.span.file,
                    prepared.span.line_start,
                    prepared.span.line_end,
                    prepared.span.original,
                    time.time(),
                ),
            )

        prepared.plan.on_finished = lambda run, data: self._finished(repo, rid, run, data)
        try:
            await self.runs.launch(repo, prepared.plan, use_cache=False, on_created=on_created)
        except RunError as exc:
            raise ReviseError(exc.status, exc.detail) from exc

        def read() -> Revision:
            with db.connect(repo) as conn:
                revision = get_revision(conn, rid)
            assert revision is not None
            return revision

        return await asyncio.to_thread(read)

    def _finished(self, repo: Repo, revision_id: str, run: Run, data: Any) -> None:
        now = time.time()
        if run.status == "done" and isinstance(data, RewriteAnswer):
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
                f"UPDATE revisions SET {assignments} WHERE id = ?", [*values.values(), revision_id]
            )
            revision = get_revision(conn, revision_id)
        if revision is not None:
            self.bus.publish(RevisionFinished(revision=revision.model_dump(mode="json")))
