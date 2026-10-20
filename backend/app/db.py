"""Per-paper SQLite state at `.rewrite/state.db` (gitignored by `rewrite init`).

The database holds app-owned, machine-local state that must not go into git: phase 3 keeps
stale-paragraph baselines and the ids the spec has used here; phase 4 adds runs, findings
and rejected findings (rows handled by app/runstore.py); phase 5 adds revision snapshots
(app/snapshots.py) and blind A/B comparisons (app/compare.py); phase 6 adds run groups
(app/groups.py) and triage calls with per-finding verdicts (app/triage.py).

Schema changes go through MIGRATIONS, an append-only list. Entry N (0-based) upgrades a
database whose `PRAGMA user_version` is N to N + 1. Never edit a migration that has shipped;
append a new one instead.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from app.repo import Repo

DB_NAME = "state.db"

# Each migration is a tuple of SQL statements applied in one transaction.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1 (phase 3)
    (
        # The paragraph text hash a paragraph node was last reconciled against. A paragraph
        # is "stale" when its current text hash differs from this baseline.
        """
        CREATE TABLE anchor_baseline (
            node_id    TEXT PRIMARY KEY,
            text_hash  TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        # Every node id the spec editor has ever written, so the id generator never hands
        # out a deleted node's id again (findings and baselines refer to nodes by id).
        """
        CREATE TABLE spec_ids (
            id         TEXT PRIMARY KEY,
            first_seen REAL NOT NULL
        )
        """,
    ),
    # 2 (phase 4): review runs, their findings and the findings the validator rejected.
    # Row access lives in app/runstore.py.
    (
        """
        CREATE TABLE runs (
            id           TEXT PRIMARY KEY,
            pass         TEXT NOT NULL,
            level        TEXT NOT NULL,
            scope_kind   TEXT NOT NULL,
            scope_ref    TEXT,
            scope_json   TEXT NOT NULL,
            scope_name   TEXT NOT NULL,
            scope_key    TEXT NOT NULL,
            runner       TEXT NOT NULL,
            model        TEXT,
            model_used   TEXT,
            thinking     TEXT,
            allow_rewrites INTEGER NOT NULL DEFAULT 0,
            git_sha      TEXT,
            prompt_hash  TEXT,
            scope_hash   TEXT,
            spec_hash    TEXT,
            cache_key    TEXT,
            cached_from  TEXT REFERENCES runs(id) ON DELETE SET NULL,
            prompt       TEXT NOT NULL,
            command_json TEXT NOT NULL DEFAULT '[]',
            raw_output   TEXT,
            raw_events   TEXT,
            stderr       TEXT,
            status       TEXT NOT NULL,
            created_at   REAL NOT NULL,
            started_at   REAL,
            finished_at  REAL,
            duration_ms  INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd     REAL,
            error        TEXT
        )
        """,
        "CREATE INDEX runs_created ON runs (created_at)",
        "CREATE INDEX runs_cache_key ON runs (cache_key, status)",
        """
        CREATE TABLE findings (
            id                TEXT PRIMARY KEY,
            run_id            TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            idx               INTEGER NOT NULL,
            file              TEXT NOT NULL,
            line_start        INTEGER NOT NULL,
            line_end          INTEGER NOT NULL,
            quote             TEXT NOT NULL,
            level             TEXT NOT NULL,
            category          TEXT NOT NULL,
            severity          TEXT NOT NULL,
            problem           TEXT NOT NULL,
            why               TEXT NOT NULL,
            json_ref          TEXT,
            state             TEXT NOT NULL DEFAULT 'open',
            anchor_state      TEXT NOT NULL,
            anchor_line_start INTEGER NOT NULL,
            anchor_line_end   INTEGER NOT NULL,
            created_at        REAL NOT NULL,
            updated_at        REAL NOT NULL
        )
        """,
        "CREATE INDEX findings_run ON findings (run_id, idx)",
        "CREATE INDEX findings_file ON findings (file, state)",
        """
        CREATE TABLE rejected_findings (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id   TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            idx      INTEGER NOT NULL,
            reason   TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
        """,
        "CREATE INDEX rejected_run ON rejected_findings (run_id, idx)",
    ),
    # 3 (phase 4): where a finding was first anchored, so re-anchoring can tell "back where
    # it was" (ok) from "elsewhere" (moved). NULL for rows written before this migration.
    (
        "ALTER TABLE findings ADD COLUMN origin_line_start INTEGER",
        "ALTER TABLE findings ADD COLUMN origin_line_end INTEGER",
    ),
    # 4 (phase 5): revision snapshots and blind A/B comparisons. Snapshot rows mirror the
    # `rewrite/*` git tags (app/snapshots.py rebuilds missing rows from the tags, so a
    # clone shows them too); comparison rows are handled by app/compare.py.
    (
        """
        CREATE TABLE snapshots (
            id         TEXT PRIMARY KEY,
            tag        TEXT NOT NULL UNIQUE,
            sha        TEXT NOT NULL,
            note       TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE comparisons (
            id                TEXT PRIMARY KEY,
            file              TEXT NOT NULL,
            node_id           TEXT,
            line_start        INTEGER,
            line_end          INTEGER,
            before_kind       TEXT NOT NULL,
            before_ref        TEXT NOT NULL,
            before_sha        TEXT,
            run_id            TEXT REFERENCES runs(id) ON DELETE SET NULL,
            status            TEXT NOT NULL,
            verdict           TEXT,
            reason            TEXT,
            mapping_json      TEXT NOT NULL,
            error             TEXT,
            before_text       TEXT NOT NULL,
            after_text        TEXT NOT NULL,
            version_a         TEXT NOT NULL,
            version_b         TEXT NOT NULL,
            before_line_start INTEGER,
            before_line_end   INTEGER,
            after_line_start  INTEGER,
            after_line_end    INTEGER,
            created_at        REAL NOT NULL,
            finished_at       REAL,
            revealed_at       REAL
        )
        """,
        "CREATE INDEX comparisons_created ON comparisons (created_at)",
        "CREATE INDEX comparisons_run ON comparisons (run_id)",
    ),
    # 5 (phase 6): run groups ("run both" and per-section fan-out, app/groups.py) and triage
    # calls (app/triage.py) with the verdict each finding got from the latest triage.
    (
        """
        CREATE TABLE run_groups (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            pass         TEXT NOT NULL,
            scope_json   TEXT NOT NULL,
            run_ids_json TEXT NOT NULL,
            status       TEXT NOT NULL,
            created_at   REAL NOT NULL,
            finished_at  REAL
        )
        """,
        "CREATE INDEX run_groups_created ON run_groups (created_at)",
        """
        CREATE TABLE triages (
            id               TEXT PRIMARY KEY,
            run_id           TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            triage_run_id    TEXT REFERENCES runs(id) ON DELETE SET NULL,
            status           TEXT NOT NULL,
            error            TEXT,
            finding_ids_json TEXT NOT NULL,
            keep_count       INTEGER NOT NULL DEFAULT 0,
            wrong_count      INTEGER NOT NULL DEFAULT 0,
            trivial_count    INTEGER NOT NULL DEFAULT 0,
            ignored_count    INTEGER NOT NULL DEFAULT 0,
            created_at       REAL NOT NULL,
            finished_at      REAL
        )
        """,
        "CREATE INDEX triages_run ON triages (run_id, created_at)",
        "ALTER TABLE findings ADD COLUMN triage_verdict TEXT",
        "ALTER TABLE findings ADD COLUMN triage_reason TEXT",
        "ALTER TABLE findings ADD COLUMN triage_id TEXT",
    ),
    # 6 (audit 2026-10-18 item 7): instruction files outside the sandbox the run's CLI
    # could read (app/runner/context.py), as a warning; NULL when none.
    ("ALTER TABLE runs ADD COLUMN context_warning TEXT",),
    # 6: re-read marks (phase 3 extension, 2026-10-19): nodes to re-read after a spec change.
    (
        """
        CREATE TABLE reread (
            node_id TEXT PRIMARY KEY,
            reason  TEXT NOT NULL,
            since   REAL NOT NULL
        )
        """,
    ),
    # 7: drafts from the spec (phase 7, 2026-10-20).
    (
        """
        CREATE TABLE drafts (
            id                 TEXT PRIMARY KEY,
            node_id            TEXT NOT NULL,
            run_id             TEXT,
            status             TEXT NOT NULL,
            text               TEXT,
            notes              TEXT,
            error              TEXT,
            created_at         REAL NOT NULL,
            finished_at        REAL,
            applied_at         REAL,
            applied_mode       TEXT,
            applied_file       TEXT,
            applied_line_start INTEGER,
            applied_line_end   INTEGER
        )
        """,
        "CREATE INDEX drafts_node ON drafts (node_id, created_at)",
    ),
    # 8: rewrite proposals on comments / findings (phase 8, Revise, 2026-10-20).
    (
        """
        CREATE TABLE revisions (
            id                 TEXT PRIMARY KEY,
            finding_id         TEXT NOT NULL,
            run_id             TEXT,
            status             TEXT NOT NULL,
            span               TEXT NOT NULL,
            file               TEXT NOT NULL,
            line_start         INTEGER NOT NULL,
            line_end           INTEGER NOT NULL,
            original           TEXT NOT NULL,
            text               TEXT,
            notes              TEXT,
            error              TEXT,
            created_at         REAL NOT NULL,
            finished_at        REAL,
            applied_at         REAL,
            applied_file       TEXT,
            applied_line_start INTEGER,
            applied_line_end   INTEGER
        )
        """,
        "CREATE INDEX revisions_finding ON revisions (finding_id, created_at)",
    ),
)


def db_path(repo: Repo) -> Path:
    return repo.rewrite_dir / DB_NAME


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the migrations this database has not seen yet.

    Runs under BEGIN IMMEDIATE so two processes opening a fresh database cannot both
    apply the same migration. Expects an autocommit connection (isolation_level=None).
    """
    if schema_version(conn) >= len(MIGRATIONS):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index in range(schema_version(conn), len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def connect(repo: Repo) -> Iterator[sqlite3.Connection]:
    """Open (creating and migrating on first use) the repo's state database.

    The connection is in autocommit mode: each statement commits on its own unless the
    caller opens a transaction with `with transaction(conn):`.
    """
    path = db_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --------------------------------------------------------------------------- anchor_baseline


def get_baseline(conn: sqlite3.Connection, node_id: str) -> str | None:
    row = conn.execute(
        "SELECT text_hash FROM anchor_baseline WHERE node_id = ?", (node_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def set_baseline(conn: sqlite3.Connection, node_id: str, text_hash: str) -> None:
    conn.execute(
        "INSERT INTO anchor_baseline (node_id, text_hash, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET text_hash = excluded.text_hash, "
        "updated_at = excluded.updated_at",
        (node_id, text_hash, time.time()),
    )


def delete_baseline(conn: sqlite3.Connection, node_id: str) -> None:
    conn.execute("DELETE FROM anchor_baseline WHERE node_id = ?", (node_id,))


def baseline_ids(conn: sqlite3.Connection) -> set[str]:
    """Every node id that ever had a baseline. Rows outlive their nodes on purpose: the id
    generator treats them as taken so a deleted paragraph's id is never handed out again."""
    return {str(row[0]) for row in conn.execute("SELECT node_id FROM anchor_baseline")}


# --------------------------------------------------------------------------- spec_ids


def record_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> None:
    now = time.time()
    conn.executemany(
        "INSERT OR IGNORE INTO spec_ids (id, first_seen) VALUES (?, ?)",
        [(node_id, now) for node_id in ids],
    )


def recorded_ids(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT id FROM spec_ids")}


# --------------------------------------------------------------------------- reread


def get_rereads(conn: sqlite3.Connection) -> dict[str, str]:
    """Every node marked for re-reading, id -> reason."""
    return {str(r[0]): str(r[1]) for r in conn.execute("SELECT node_id, reason FROM reread")}


def set_reread(conn: sqlite3.Connection, node_id: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO reread (node_id, reason, since) VALUES (?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET reason = excluded.reason, since = excluded.since",
        (node_id, reason, time.time()),
    )


def clear_reread(conn: sqlite3.Connection, node_id: str) -> None:
    conn.execute("DELETE FROM reread WHERE node_id = ?", (node_id,))


def prune_rereads(conn: sqlite3.Connection, keep: set[str]) -> None:
    """Drop marks of nodes that no longer exist."""
    for node_id in [str(r[0]) for r in conn.execute("SELECT node_id FROM reread")]:
        if node_id not in keep:
            conn.execute("DELETE FROM reread WHERE node_id = ?", (node_id,))
