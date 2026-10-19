"""`.rewrite/state.db`: creation, migrations and the anchor_baseline table."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db
from app.repo import Repo


def test_connect_creates_and_migrates(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    assert not db.db_path(repo).exists()
    with db.connect(repo) as conn:
        assert db.schema_version(conn) == len(db.MIGRATIONS)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "anchor_baseline" in tables
    assert db.db_path(repo) == initialized_repo / ".rewrite" / "state.db"
    with db.connect(repo) as conn:  # reopening does not re-run migrations
        assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_baseline_crud(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        assert db.get_baseline(conn, "p-1") is None
        db.set_baseline(conn, "p-1", "aaa")
        db.set_baseline(conn, "p-1", "bbb")
        db.set_baseline(conn, "p-2", "ccc")
        assert db.get_baseline(conn, "p-1") == "bbb"
        db.delete_baseline(conn, "p-2")
        assert db.baseline_ids(conn) == {"p-1"}
    with db.connect(repo) as conn:
        assert db.get_baseline(conn, "p-1") == "bbb"


def test_transaction_rolls_back(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        with pytest.raises(RuntimeError), db.transaction(conn):
            db.set_baseline(conn, "p-1", "aaa")
            raise RuntimeError("boom")
        assert db.get_baseline(conn, "p-1") is None


def test_appended_migration_runs_once(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 4 adds tables by appending to MIGRATIONS; existing databases pick them up."""
    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        db.set_baseline(conn, "p-1", "aaa")
    shipped = len(db.MIGRATIONS)
    extra = ("CREATE TABLE later_table (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",)
    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS, extra))
    with db.connect(repo) as conn:
        assert db.schema_version(conn) == shipped + 1
        conn.execute("INSERT INTO later_table (name) VALUES ('clarity')")
        assert db.get_baseline(conn, "p-1") == "aaa"
    with db.connect(repo) as conn:
        assert conn.execute("SELECT count(*) FROM later_table").fetchone()[0] == 1


def test_failed_migration_leaves_version(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo.at(initialized_repo)
    with db.connect(repo):
        pass
    bad = ("CREATE TABLE ok_table (x)", "CREATE TABLE broken (")
    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS, bad))
    with pytest.raises(sqlite3.Error), db.connect(repo):
        pass
    monkeypatch.undo()
    with db.connect(repo) as conn:
        assert db.schema_version(conn) == len(db.MIGRATIONS)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ok_table" not in tables


def test_spec_ids_are_recorded_once(initialized_repo: Path) -> None:
    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        db.record_ids(conn, ["message", "p-1"])
        first = conn.execute("SELECT first_seen FROM spec_ids WHERE id = 'p-1'").fetchone()[0]
        db.record_ids(conn, ["p-1", "c1"])
        assert db.recorded_ids(conn) == {"message", "p-1", "c1"}
        again = conn.execute("SELECT first_seen FROM spec_ids WHERE id = 'p-1'").fetchone()[0]
        assert again == first


def test_runs_and_findings_roundtrip(initialized_repo: Path) -> None:
    from app import runstore
    from app.findings import Rejected

    repo = Repo.at(initialized_repo)
    with db.connect(repo) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runs", "findings", "rejected_findings"} <= tables
        runstore.insert_run(
            conn,
            {
                "id": "run1",
                "pass": "clarity",
                "level": "sentence",
                "scope_kind": "file",
                "scope_ref": "main.tex",
                "scope_json": '{"kind": "file", "file": "main.tex"}',
                "scope_name": "file main.tex",
                "scope_key": "clarity:file:main.tex",
                "runner": "claude",
                "model": "sonnet",
                "thinking": "low",
                "cache_key": "k1",
                "prompt": "the prompt",
                "command_json": '["claude", "-p"]',
                "status": "done",
                "created_at": 1.0,
                "cost_usd": 0.5,
            },
        )
        runstore.insert_findings(
            conn,
            [
                {
                    "id": "run1-0",
                    "run_id": "run1",
                    "idx": 0,
                    "file": "main.tex",
                    "line_start": 3,
                    "line_end": 4,
                    "quote": "q",
                    "level": "sentence",
                    "category": "hedge",
                    "severity": "minor",
                    "problem": "The sentence hedges.",
                    "why": "w",
                    "json_ref": None,
                    "anchor_state": "ok",
                    "anchor_line_start": 3,
                    "anchor_line_end": 4,
                }
            ],
        )
        runstore.insert_rejected(conn, "run1", [Rejected(index=1, reason="praise", raw={"a": 1})])
    with db.connect(repo) as conn:
        run = runstore.get_run(conn, "run1")
        assert run is not None
        assert run.pass_ == "clarity" and run.scope.file == "main.tex"
        assert (run.findings_count, run.rejected_count, run.cost_usd) == (1, 1, 0.5)
        assert run.model_dump(by_alias=True)["pass"] == "clarity"
        assert (
            runstore.find_cached(conn, "k1") == "run1" and runstore.find_cached(conn, "x") is None
        )
        (finding,) = runstore.list_findings(conn, file="main.tex")
        assert finding.state == "open" and finding.anchor_state == "ok"
        runstore.set_finding_state(conn, "run1-0", "dismissed")
        runstore.update_anchor(conn, "run1-0", "moved", 5, 6)
        assert runstore.open_findings_for_file(conn, "main.tex") == []
        moved = runstore.get_finding(conn, "run1-0")
        assert moved is not None and (moved.anchor_state, moved.anchor_line_start) == ("moved", 5)
        assert runstore.list_rejected(conn, "run1") == [
            Rejected(index=1, reason="praise", raw={"a": 1})
        ]
        text = runstore.get_run_text(conn, "run1")
        assert text is not None and text.prompt == "the prompt" and text.command == ["claude", "-p"]
        conn.execute("DELETE FROM runs WHERE id = 'run1'")  # cascades
        assert conn.execute("SELECT count(*) FROM findings").fetchone()[0] == 0


def test_phase6_schema(initialized_repo: Path) -> None:
    """Migration 5: run groups, triages, and the triage columns on findings."""
    with db.connect(Repo.at(initialized_repo)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"run_groups", "triages"} <= tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(findings)")}
        assert {"triage_verdict", "triage_reason", "triage_id"} <= columns
        groups = {r[1] for r in conn.execute("PRAGMA table_info(run_groups)")}
        assert groups == {
            "id",
            "kind",
            "pass",
            "scope_json",
            "run_ids_json",
            "status",
            "created_at",
            "finished_at",
        }
