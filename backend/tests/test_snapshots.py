"""Revision snapshots: annotated `rewrite/rev-<n>-<slug>` tags, the dirty-tree commit, and
rows rebuilt from the tags (app/snapshots.py, app/api/snapshots.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import db, snapshots
from app.repo import Repo
from tests.conftest import git, make_client, requires_git

pytestmark = requires_git

INTRO = "sections/intro.tex"


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def test_numbering_and_tags(client: TestClient, git_repo: Path) -> None:
    head = git(git_repo, "rev-parse", "HEAD").strip()
    first = ok(client.post("/api/snapshots", json={"note": "First full draft"}))
    snap = first["snapshot"]
    assert first["commit"] is None  # the tree was clean
    assert snap["tag"] == "rewrite/rev-1-first-full-draft"
    assert snap["sha"] == head and snap["note"] == "First full draft"
    assert snap["id"] and snap["created_at"] > 0
    assert git(git_repo, "cat-file", "-t", snap["tag"]).strip() == "tag"  # annotated
    assert git(git_repo, "tag", "-l", "--format=%(contents)", snap["tag"]).strip() == (
        "First full draft"
    )
    second = ok(client.post("/api/snapshots", json={"note": "Après la relecture: v2!"}))
    assert second["snapshot"]["tag"] == "rewrite/rev-2-apres-la-relecture-v2"
    git(git_repo, "tag", "-a", "rewrite/rev-7-by-hand", "-m", "made in a terminal")
    third = ok(client.post("/api/snapshots", json={"note": "#3 starts with a hash"}))
    assert third["snapshot"]["tag"] == "rewrite/rev-8-3-starts-with-a-hash"
    assert third["snapshot"]["note"] == "#3 starts with a hash"
    listed = ok(client.get("/api/snapshots"))["snapshots"]
    assert [s["tag"] for s in listed] == [
        "rewrite/rev-8-3-starts-with-a-hash",
        "rewrite/rev-7-by-hand",
        "rewrite/rev-2-apres-la-relecture-v2",
        "rewrite/rev-1-first-full-draft",
    ]
    by_tag = {s["tag"]: s for s in listed}
    assert by_tag["rewrite/rev-7-by-hand"]["note"] == "made in a terminal"
    assert by_tag["rewrite/rev-1-first-full-draft"]["id"] == snap["id"]
    assert ok(client.get("/api/snapshots"))["snapshots"] == listed  # stable ids
    commits = ok(client.get("/api/git/log"))["commits"]
    assert set(commits[0]["tags"]) >= {"rewrite/rev-1-first-full-draft"}


def test_dirty_tree_is_committed_first(client: TestClient, git_repo: Path) -> None:
    path = git_repo / INTRO
    path.write_text(path.read_text().replace("Function-as-a-Service", "FaaS", 1))
    (git_repo / "sections" / "draft.tex").write_text("new\n")
    (git_repo / ".rewrite" / "state.db").write_bytes(b"x")
    data = ok(client.post("/api/snapshots", json={"note": "Major revision\n\nafter review"}))
    head = git(git_repo, "rev-parse", "HEAD").strip()
    assert data["commit"] == head
    assert data["snapshot"]["sha"] == head
    assert data["snapshot"]["tag"] == "rewrite/rev-1-major-revision-after-review"
    assert git(git_repo, "log", "-1", "--format=%B").strip() == (
        "Snapshot: Major revision\n\nafter review"
    )
    committed = set(git(git_repo, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {INTRO, "sections/draft.tex"}
    assert ok(client.get("/api/git/status"))["clean"]
    with db.connect(Repo.at(git_repo)) as conn:
        rows = conn.execute("SELECT tag, sha, note FROM snapshots").fetchall()
    assert rows == [(data["snapshot"]["tag"], head, "Major revision\n\nafter review")]


def test_snapshot_after_a_staged_deletion_and_rename(client: TestClient, git_repo: Path) -> None:
    """Audit 2026-10-18 item 9: a dirty-tree snapshot commits through gitops.commit, which
    must cope with paths already removed from the index (`git rm`, the old side of
    `git mv`)."""
    git(git_repo, "rm", "-q", "sections/results.tex")
    git(git_repo, "mv", "sections/conclusion.tex", "sections/closing.tex")
    data = ok(client.post("/api/snapshots", json={"note": "Restructure"}))
    head = git(git_repo, "rev-parse", "HEAD").strip()
    assert data["commit"] == head and data["snapshot"]["sha"] == head
    changes = git(git_repo, "show", "-M", "--name-status", "--format=", "HEAD").split("\n")
    changes = sorted(
        line.split("\t")[0][:1] + " " + " ".join(line.split("\t")[1:]) for line in changes if line
    )
    assert changes == [
        "D sections/results.tex",
        "R sections/conclusion.tex sections/closing.tex",
    ]
    assert ok(client.get("/api/git/status"))["clean"]


def test_snapshots_are_rebuilt_from_tags_in_a_clone(
    client: TestClient, remote: Path, tmp_path: Path
) -> None:
    created = ok(client.post("/api/snapshots", json={"note": "v1"}))["snapshot"]
    repo = remote.parent / "paper"
    git(repo, "push", "-q", "--follow-tags")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    assert (clone / ".rewrite" / "config.toml").is_file()  # versioned with the paper
    assert not (clone / ".rewrite" / "state.db").exists()
    with make_client(clone) as other:
        listed = ok(other.get("/api/snapshots"))["snapshots"]
        assert len(listed) == 1
        snap = listed[0]
        assert (snap["tag"], snap["sha"], snap["note"]) == (
            created["tag"],
            created["sha"],
            "v1",
        )
        assert ok(other.get("/api/snapshots"))["snapshots"][0]["id"] == snap["id"]
        nxt = ok(other.post("/api/snapshots", json={"note": "v2"}))["snapshot"]
        assert nxt["tag"] == "rewrite/rev-2-v2"


def test_snapshot_errors(client: TestClient, initialized_repo: Path) -> None:
    assert ok(client.get("/api/snapshots")) == {"snapshots": []}  # not a git repo
    assert client.post("/api/snapshots", json={"note": "v1"}).status_code == 400
    git(initialized_repo, "init", "-q", "-b", "main")
    assert ok(client.get("/api/snapshots")) == {"snapshots": []}
    for body in ({}, {"note": 1}, {"note": "x", "extra": 1}):
        assert client.post("/api/snapshots", json=body).status_code == 400, body
    empty = client.post("/api/snapshots", json={"note": "   "})
    assert empty.status_code == 400 and "note" in empty.json()["detail"]
    # a repo without commits: the dirty tree is committed first, then tagged
    data = ok(client.post("/api/snapshots", json={"note": "start"}))
    assert data["commit"] and data["snapshot"]["tag"] == "rewrite/rev-1-start"


def test_deleted_tag_numbers_are_not_reused(client: TestClient, git_repo: Path) -> None:
    ok(client.post("/api/snapshots", json={"note": "one"}))
    ok(client.post("/api/snapshots", json={"note": "two"}))
    git(git_repo, "tag", "-d", "rewrite/rev-2-two")
    assert [s["tag"] for s in ok(client.get("/api/snapshots"))["snapshots"]] == [
        "rewrite/rev-1-one"
    ]
    third = ok(client.post("/api/snapshots", json={"note": "three"}))["snapshot"]
    assert third["tag"] == "rewrite/rev-3-three"


def test_resolve_by_id_or_tag(client: TestClient, git_repo: Path) -> None:
    snap = ok(client.post("/api/snapshots", json={"note": "v1"}))["snapshot"]
    repo = Repo.at(git_repo)
    assert snapshots.resolve(repo, snap["id"]) == (snap["tag"], snap["sha"])
    assert snapshots.resolve(repo, snap["tag"]) == (snap["tag"], snap["sha"])
    assert snapshots.rev_number("rewrite/rev-12-x") == 12
    assert snapshots.rev_number("rewrite/rev-3") == 3
    assert snapshots.rev_number("rewrite/other") is None
