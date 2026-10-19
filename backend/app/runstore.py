"""Rows of the phase-4 tables in state.db (`runs`, `findings`, `rejected_findings`; the
schema is migration 2 in app/db.py) and the API models built from them.

A run row keeps everything needed to replay or diff it: the exact prompt, the argv, the raw
answer, the raw event stream, stderr, the content hashes and the cache key.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.findings import Finding, Rejected
from app.prompts import Scope

RunStatus = Literal["queued", "running", "done", "failed", "cancelled", "cached"]
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")


class Run(BaseModel):
    """A run as the API returns it (phase-4 brief "API contract"; extras marked +)."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    pass_: str = Field(alias="pass")
    level: str
    scope: Scope
    scope_name: str
    runner: str  # claude | codex | paste
    model: str | None  # as requested; null = the CLI's default
    thinking: str | None
    status: RunStatus
    cached_from: str | None
    git_sha: str | None
    started_at: float | None
    finished_at: float | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None
    findings_count: int
    rejected_count: int
    created_at: float  # +
    model_used: str | None = None  # + the model the CLI reported (claude does, codex does not)
    allow_rewrites: bool = False  # + the pass's flag at run time
    # + instruction files outside the sandbox the CLI could read with this run's argv
    # (audit 2026-10-18 item 7); null when none could.
    context_warning: str | None = None


class RunText(BaseModel):
    prompt: str
    command: list[str]
    raw_output: str | None
    raw_events: str | None
    stderr: str | None


def new_run_id() -> str:
    """Sortable by creation time: milliseconds in hex plus random bits."""
    return f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def finding_id(run_id: str, index: int) -> str:
    return f"{run_id}-{index}"


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


_RUN_SELECT = """
    SELECT r.*,
           (SELECT count(*) FROM findings f WHERE f.run_id = r.id) AS findings_count,
           (SELECT count(*) FROM rejected_findings x WHERE x.run_id = r.id) AS rejected_count
    FROM runs r
"""


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        pass_=row["pass"],
        level=row["level"],
        scope=Scope.model_validate_json(row["scope_json"]),
        scope_name=row["scope_name"],
        runner=row["runner"],
        model=row["model"],
        thinking=row["thinking"],
        status=row["status"],
        cached_from=row["cached_from"],
        git_sha=row["git_sha"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        error=row["error"],
        findings_count=row["findings_count"],
        rejected_count=row["rejected_count"],
        created_at=row["created_at"],
        model_used=row["model_used"],
        allow_rewrites=bool(row["allow_rewrites"]),
        context_warning=row["context_warning"],
    )


RUN_COLUMNS = (
    "id",
    "pass",
    "level",
    "scope_kind",
    "scope_ref",
    "scope_json",
    "scope_name",
    "scope_key",
    "runner",
    "model",
    "model_used",
    "thinking",
    "allow_rewrites",
    "git_sha",
    "prompt_hash",
    "scope_hash",
    "spec_hash",
    "cache_key",
    "cached_from",
    "prompt",
    "command_json",
    "raw_output",
    "raw_events",
    "stderr",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "error",
    "context_warning",
)


def insert_run(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    unknown = set(values) - set(RUN_COLUMNS)
    if unknown:
        raise KeyError(f"unknown run columns: {sorted(unknown)}")
    columns = list(values)
    conn.execute(
        f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [values[c] for c in columns],
    )


def update_run(conn: sqlite3.Connection, run_id: str, **values: Any) -> None:
    if not values:
        return
    unknown = set(values) - set(RUN_COLUMNS)
    if unknown:
        raise KeyError(f"unknown run columns: {sorted(unknown)}")
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", [*values.values(), run_id])


def get_run(conn: sqlite3.Connection, run_id: str) -> Run | None:
    row = _cursor(conn).execute(_RUN_SELECT + " WHERE r.id = ?", (run_id,)).fetchone()
    return None if row is None else _run(row)


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[Run]:
    rows = _cursor(conn).execute(
        _RUN_SELECT + " ORDER BY r.created_at DESC, r.id DESC LIMIT ?", (limit,)
    )
    return [_run(row) for row in rows]


def get_run_text(conn: sqlite3.Connection, run_id: str) -> RunText | None:
    row = (
        _cursor(conn)
        .execute(
            "SELECT prompt, command_json, raw_output, raw_events, stderr FROM runs WHERE id = ?",
            (run_id,),
        )
        .fetchone()
    )
    if row is None:
        return None
    return RunText(
        prompt=row["prompt"],
        command=json.loads(row["command_json"] or "[]"),
        raw_output=row["raw_output"],
        raw_events=row["raw_events"],
        stderr=row["stderr"],
    )


def find_cached(conn: sqlite3.Connection, cache_key: str) -> str | None:
    """The newest finished (`done`) run with this cache key."""
    row = conn.execute(
        "SELECT id FROM runs WHERE cache_key = ? AND status = 'done' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (cache_key,),
    ).fetchone()
    return None if row is None else str(row[0])


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Runs left queued or running by a previous server process become failed."""
    now = time.time()
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', error = 'interrupted: the server stopped during the "
        "run', finished_at = ? WHERE status IN ('queued', 'running')",
        (now,),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- findings

_FINDING_COLUMNS = (
    "id, run_id, file, line_start, line_end, quote, level, category, severity, problem, why, "
    "json_ref, state, anchor_state, anchor_line_start, anchor_line_end, created_at, "
    "triage_verdict, triage_reason"
)


def _finding(row: sqlite3.Row) -> Finding:
    return Finding(**{key: row[key] for key in row.keys() if key in Finding.model_fields})


def finding_origin(conn: sqlite3.Connection, finding_id_: str) -> tuple[int, int] | None:
    """Where the finding was first anchored (None for rows from before migration 3)."""
    row = conn.execute(
        "SELECT origin_line_start, origin_line_end FROM findings WHERE id = ?", (finding_id_,)
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


def insert_findings(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    now = time.time()
    conn.executemany(
        "INSERT INTO findings (id, run_id, idx, file, line_start, line_end, quote, level, "
        "category, severity, problem, why, json_ref, state, anchor_state, anchor_line_start, "
        "anchor_line_end, origin_line_start, origin_line_end, created_at, updated_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["id"],
                row["run_id"],
                row["idx"],
                row["file"],
                row["line_start"],
                row["line_end"],
                row["quote"],
                row["level"],
                row["category"],
                row["severity"],
                row["problem"],
                row["why"],
                row["json_ref"],
                row.get("state", "open"),
                row["anchor_state"],
                row["anchor_line_start"],
                row["anchor_line_end"],
                row.get("origin_line_start", row["anchor_line_start"]),
                row.get("origin_line_end", row["anchor_line_end"]),
                row.get("created_at", now),
                now,
            )
            for row in rows
        ],
    )


def insert_rejected(conn: sqlite3.Connection, run_id: str, rejected: Sequence[Rejected]) -> None:
    conn.executemany(
        "INSERT INTO rejected_findings (run_id, idx, reason, raw_json) VALUES (?, ?, ?, ?)",
        [(run_id, r.index, r.reason, json.dumps(r.raw, ensure_ascii=False)) for r in rejected],
    )


def list_findings(
    conn: sqlite3.Connection,
    *,
    file: str | None = None,
    state: str | None = None,
    run_id: str | None = None,
) -> list[Finding]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (("f.file", file), ("f.state", state), ("f.run_id", run_id)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    sql = (
        f"SELECT {', '.join('f.' + c.strip() for c in _FINDING_COLUMNS.split(','))} "
        "FROM findings f JOIN runs r ON r.id = f.run_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC, f.run_id DESC, f.idx ASC"
    return [_finding(row) for row in _cursor(conn).execute(sql, params)]


def get_finding(conn: sqlite3.Connection, finding_id_: str) -> Finding | None:
    row = (
        _cursor(conn)
        .execute(f"SELECT {_FINDING_COLUMNS} FROM findings WHERE id = ?", (finding_id_,))
        .fetchone()
    )
    return None if row is None else _finding(row)


def set_finding_state(conn: sqlite3.Connection, finding_id_: str, state: str) -> None:
    conn.execute(
        "UPDATE findings SET state = ?, updated_at = ? WHERE id = ?",
        (state, time.time(), finding_id_),
    )


def open_findings_for_file(conn: sqlite3.Connection, file: str) -> list[Finding]:
    return list_findings(conn, file=file, state="open")


def update_anchor(
    conn: sqlite3.Connection, finding_id_: str, state: str, line_start: int, line_end: int
) -> None:
    conn.execute(
        "UPDATE findings SET anchor_state = ?, anchor_line_start = ?, anchor_line_end = ?, "
        "updated_at = ? WHERE id = ?",
        (state, line_start, line_end, time.time(), finding_id_),
    )


def list_rejected(conn: sqlite3.Connection, run_id: str) -> list[Rejected]:
    rows = conn.execute(
        "SELECT idx, reason, raw_json FROM rejected_findings WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [Rejected(index=idx, reason=reason, raw=json.loads(raw)) for idx, reason, raw in rows]
