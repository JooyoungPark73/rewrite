"""The git panel backend: status, diff, log, show, commit, push and pull against real
temporary repos and a local bare remote (app/gitops.py, app/api/git.py)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import gitops
from app.events import EventBus
from app.gitops import GitError, GitManager
from app.repo import Repo
from tests.conftest import git, requires_git

pytestmark = requires_git

INTRO = "sections/intro.tex"


def drain(client: TestClient) -> list[dict[str, Any]]:
    """Events published since the last drain (the subscriber is created on first use)."""
    state = client.app.state.pb  # type: ignore[attr-defined]
    queue = getattr(state, "_test_queue", None)
    if queue is None:
        queue = state._test_queue = state.bus.subscribe()
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def wait_git(client: TestClient, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Wait for the running push/pull to end; returns the events up to `git.finished`."""
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        events += drain(client)
        if any(e["type"] == "git.finished" for e in events):
            return events
        if time.monotonic() > deadline:
            raise AssertionError(f"git op did not finish: {events}")
        time.sleep(0.05)


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def edit(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


# --------------------------------------------------------------------------- status


def test_status_outside_git(client: TestClient) -> None:
    data = ok(client.get("/api/git/status"))
    assert data == {
        "branch": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "head_sha": None,
        "head_subject": None,
        "files": [],
        "clean": True,
        "is_repo": False,
        "remotes": [],
    }
    assert ok(client.get("/api/git/log")) == {"commits": []}
    assert ok(client.get("/api/git/diff")) == {"diff": "", "truncated": False}
    assert client.post("/api/git/commit", json={"message": "x"}).status_code == 400
    assert client.post("/api/git/push").status_code == 400
    assert client.get("/api/git/show", params={"ref": "HEAD", "path": INTRO}).status_code == 400


def test_status_lists_changes(client: TestClient, git_repo: Path) -> None:
    data = ok(client.get("/api/git/status"))
    assert data["is_repo"] and data["clean"] and data["files"] == []
    assert data["branch"] == "main" and data["upstream"] is None
    assert data["head_sha"] == git(git_repo, "rev-parse", "HEAD").strip()
    assert data["head_subject"] == "initial"

    edit(git_repo, INTRO, "Function-as-a-Service", "Functions-as-a-Service")
    (git_repo / "sections" / "new file.tex").write_text("new\n")
    (git_repo / "sections" / "conclusion.tex").unlink()
    (git_repo / "refs.bib").write_text("% changed\n")
    git(git_repo, "add", "refs.bib")
    (git_repo / ".rewrite" / "state.db").write_bytes(b"ignored")
    data = ok(client.get("/api/git/status"))
    files = {f["path"]: f for f in data["files"]}
    assert set(files) == {INTRO, "sections/new file.tex", "sections/conclusion.tex", "refs.bib"}
    assert files[INTRO].items() >= {"status": "M", "staged": False, "xy": ".M"}.items()
    assert files["sections/new file.tex"]["status"] == "??"
    assert files["sections/conclusion.tex"]["status"] == "D"
    assert files["refs.bib"].items() >= {"status": "M", "staged": True, "xy": "M."}.items()
    assert data["clean"] is False


def test_status_rename_and_upstream(client: TestClient, remote: Path) -> None:
    repo = remote.parent / "paper"
    git(repo, "mv", "sections/results.tex", "sections/findings.tex")
    data = ok(client.get("/api/git/status"))
    renamed = next(f for f in data["files"] if f["status"] == "R")
    assert renamed["path"] == "sections/findings.tex"
    assert renamed["orig_path"] == "sections/results.tex" and renamed["staged"]
    assert data["upstream"] == "origin/main" and data["remotes"] == ["origin"]
    git(repo, "commit", "-q", "-m", "rename")
    data = ok(client.get("/api/git/status"))
    assert (data["ahead"], data["behind"]) == (1, 0)


def test_paper_in_a_subdirectory_of_a_work_tree(tmp_path: Path, initialized_repo: Path) -> None:
    """A paper repo inside a larger work tree: paths stay relative to the paper root."""
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    paper = outer / "papers" / "p"
    paper.parent.mkdir()
    initialized_repo.rename(paper)
    (outer / "unrelated.txt").write_text("x\n")
    state = gitops.status(paper)
    assert state.is_repo
    paths = {f.path for f in state.files}
    assert INTRO in paths and "main.tex" in paths and "unrelated.txt" not in paths
    result = gitops.commit(paper, "paper only")
    assert INTRO in result.files and "unrelated.txt" not in result.files
    assert gitops.status(paper).clean
    assert "unrelated.txt" in git(outer, "status", "--porcelain")
    assert gitops.show(paper, "HEAD", INTRO).startswith("\\section{Introduction}")


# --------------------------------------------------------------------------- diff


def test_diff_working_tree_untracked_and_staged(client: TestClient, git_repo: Path) -> None:
    edit(git_repo, INTRO, "Function-as-a-Service", "Functions-as-a-Service")
    (git_repo / "sections" / "extra.tex").write_text("brand new line\n")
    whole = ok(client.get("/api/git/diff"))["diff"]
    assert "diff --git a/sections/intro.tex b/sections/intro.tex" in whole
    assert "+Functions-as-a-Service" in whole
    assert "+++ b/sections/extra.tex" in whole and "+brand new line" in whole
    assert "new file mode" in whole

    one = ok(client.get("/api/git/diff", params={"path": INTRO}))["diff"]
    assert "intro.tex" in one and "extra.tex" not in one
    new = ok(client.get("/api/git/diff", params={"path": "sections/extra.tex"}))["diff"]
    assert "+brand new line" in new and "intro.tex" not in new

    assert ok(client.get("/api/git/diff", params={"staged": "true"}))["diff"] == ""
    git(git_repo, "add", INTRO)
    staged = ok(client.get("/api/git/diff", params={"staged": "true"}))["diff"]
    assert "+Functions-as-a-Service" in staged and "extra.tex" not in staged
    # staged=false is the working tree against HEAD, so a staged change still shows
    assert "+Functions-as-a-Service" in ok(client.get("/api/git/diff"))["diff"]

    for bad in ("../outside.tex", "/etc/passwd", ".git/config"):
        assert client.get("/api/git/diff", params={"path": bad}).status_code == 400, bad


def test_diff_before_the_first_commit(client: TestClient, initialized_repo: Path) -> None:
    git(initialized_repo, "init", "-q", "-b", "main")
    git(initialized_repo, "add", "main.tex")
    staged = ok(client.get("/api/git/diff", params={"staged": "true"}))["diff"]
    assert "+++ b/main.tex" in staged
    status = ok(client.get("/api/git/status"))
    assert status["head_sha"] is None and status["branch"] == "main"
    assert "+++ b/sections/intro.tex" in ok(client.get("/api/git/diff"))["diff"]


# --------------------------------------------------------------------------- log, show


def test_log_with_tags(client: TestClient, git_repo: Path) -> None:
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    git(git_repo, "commit", "-qam", "second commit")
    git(git_repo, "tag", "-a", "rewrite/rev-1-x", "-m", "note")
    git(git_repo, "tag", "light")
    commits = ok(client.get("/api/git/log"))["commits"]
    assert [c["subject"] for c in commits] == ["second commit", "initial"]
    head = commits[0]
    assert head["sha"] == git(git_repo, "rev-parse", "HEAD").strip()
    assert sorted(head["tags"]) == ["light", "rewrite/rev-1-x"]
    assert head["author"] == "Test Author" and head["date"] > 1_600_000_000
    assert commits[1]["tags"] == []
    assert len(ok(client.get("/api/git/log", params={"limit": 1}))["commits"]) == 1


def test_show_file_at_ref(client: TestClient, git_repo: Path) -> None:
    original = (git_repo / INTRO).read_text()
    first = git(git_repo, "rev-parse", "HEAD").strip()
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    git(git_repo, "commit", "-qam", "second")
    for ref in (first, first[:10], "HEAD~1", "main~1"):
        assert ok(client.get("/api/git/show", params={"ref": ref, "path": INTRO})) == {
            "content": original
        }, ref
    absent = client.get("/api/git/show", params={"ref": "HEAD", "path": "sections/none.tex"})
    assert absent.status_code == 404
    folder = client.get("/api/git/show", params={"ref": "HEAD", "path": "sections"})
    assert folder.status_code == 404
    for bad in ("nope", "-n", "HEAD:main.tex", "HEAD main"):
        response = client.get("/api/git/show", params={"ref": bad, "path": INTRO})
        assert response.status_code == 400, bad
    escape = client.get("/api/git/show", params={"ref": "HEAD", "path": "../x"})
    assert escape.status_code == 400


# --------------------------------------------------------------------------- commit


def test_commit_explicit_paths_only(client: TestClient, git_repo: Path) -> None:
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    (git_repo / "refs.bib").write_text("% staged elsewhere\n")
    git(git_repo, "add", "refs.bib")
    (git_repo / "sections" / "new.tex").write_text("new\n")
    result = ok(client.post("/api/git/commit", json={"message": "Intro wording", "paths": [INTRO]}))
    assert result["subject"] == "Intro wording"
    assert result["sha"] == git(git_repo, "rev-parse", "HEAD").strip()
    shown = git(git_repo, "show", "--name-only", "--format=", "HEAD").split()
    assert shown == [INTRO]
    status = {f["path"]: f for f in ok(client.get("/api/git/status"))["files"]}
    assert status["refs.bib"]["staged"] and status["sections/new.tex"]["status"] == "??"
    assert INTRO not in status


def test_commit_defaults_to_every_changed_path(client: TestClient, git_repo: Path) -> None:
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    (git_repo / "sections" / "conclusion.tex").unlink()
    (git_repo / "sections" / "new.tex").write_text("new\n")
    (git_repo / ".rewrite" / "state.db").write_bytes(b"x")  # gitignored: never committed
    (git_repo / ".rewrite" / "build" / "main.pdf").write_bytes(b"%PDF")
    result = ok(client.post("/api/git/commit", json={"message": "  Everything\n\nbody  "}))
    assert result["subject"] == "Everything"
    assert sorted(result["files"]) == sorted([INTRO, "sections/conclusion.tex", "sections/new.tex"])
    assert ok(client.get("/api/git/status"))["clean"]
    tracked = git(git_repo, "ls-files").split()
    assert ".rewrite/state.db" not in tracked and "sections/new.tex" in tracked
    assert git(git_repo, "log", "-1", "--format=%B").strip() == "Everything\n\nbody"


def test_commit_errors(client: TestClient, git_repo: Path) -> None:
    assert client.post("/api/git/commit", json={"message": "   "}).status_code == 400
    clean = client.post("/api/git/commit", json={"message": "x"})
    assert clean.status_code == 400 and "nothing to commit" in clean.json()["detail"]
    unchanged = client.post("/api/git/commit", json={"message": "x", "paths": ["main.tex"]})
    assert unchanged.status_code == 400 and "nothing to commit" in unchanged.json()["detail"]
    missing = client.post("/api/git/commit", json={"message": "x", "paths": ["nope.tex"]})
    assert missing.status_code == 400
    ignored = client.post("/api/git/commit", json={"message": "x", "paths": [".rewrite/state.db"]})
    assert ignored.status_code == 400
    for body in ({}, {"message": 3}, {"message": "x", "paths": "a"}, {"message": "x", "y": 1}):
        assert client.post("/api/git/commit", json=body).status_code == 400, body
    foreign = client.post(
        "/api/git/commit", json={"message": "x"}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403


CONCLUSION = "sections/conclusion.tex"
CLOSING = "sections/closing.tex"


def head_changes(repo: Path) -> list[str]:
    """`<status letter> <path>[ <new path>]` per path the HEAD commit changed (renames found)."""
    out = git(repo, "show", "-M", "--name-status", "--format=", "HEAD").strip().splitlines()
    return [" ".join([line.split("\t")[0][:1], *line.split("\t")[1:]]) for line in out]


@pytest.mark.parametrize("subset", [False, True], ids=["all", "subset"])
def test_commit_after_a_staged_deletion(client: TestClient, git_repo: Path, subset: bool) -> None:
    """Audit 2026-10-18 item 9: `git rm` leaves a path in neither the index nor the working
    tree; committing it must not run `git add` on it (fatal: pathspec did not match)."""
    git(git_repo, "rm", "-q", CONCLUSION)
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")  # not selected in the subset case
    (git_repo / "refs.bib").write_text("% staged elsewhere\n")
    git(git_repo, "add", "refs.bib")
    body: dict[str, Any] = {"message": "Drop the conclusion"}
    if subset:
        body["paths"] = [CONCLUSION]
    result = ok(client.post("/api/git/commit", json=body))
    assert result["sha"] == git(git_repo, "rev-parse", "HEAD").strip()
    status = {f["path"]: f for f in ok(client.get("/api/git/status"))["files"]}
    if subset:
        assert head_changes(git_repo) == [f"D {CONCLUSION}"]
        assert status[INTRO]["status"] == "M" and status["refs.bib"]["staged"]
    else:
        assert sorted(head_changes(git_repo)) == sorted(
            [f"D {CONCLUSION}", f"M {INTRO}", "M refs.bib"]
        )
        assert status == {}
    assert CONCLUSION not in git(git_repo, "ls-files").split()


@pytest.mark.parametrize("subset", [False, True], ids=["all", "subset"])
def test_commit_after_a_staged_rename(client: TestClient, git_repo: Path, subset: bool) -> None:
    """Audit item 9: after `git mv` status lists both sides; the old path is gone from the
    index and the working tree. Selecting the new path (what the git panel sends) commits
    the whole rename, and nothing else."""
    git(git_repo, "mv", CONCLUSION, CLOSING)
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    (git_repo / "refs.bib").write_text("% staged elsewhere\n")
    git(git_repo, "add", "refs.bib")
    renamed = next(f for f in ok(client.get("/api/git/status"))["files"] if f["path"] == CLOSING)
    assert renamed["orig_path"] == CONCLUSION and renamed["status"] == "R"
    body: dict[str, Any] = {"message": "Rename the conclusion"}
    if subset:
        body["paths"] = [CLOSING]
    result = ok(client.post("/api/git/commit", json=body))
    assert set(result["files"]) >= {CONCLUSION, CLOSING}
    status = {f["path"]: f for f in ok(client.get("/api/git/status"))["files"]}
    if subset:
        assert head_changes(git_repo) == [f"R {CONCLUSION} {CLOSING}"]
        assert set(status) == {INTRO, "refs.bib"}
        assert status["refs.bib"]["staged"] and not status[INTRO]["staged"]
    else:
        assert sorted(head_changes(git_repo)) == sorted(
            [f"R {CONCLUSION} {CLOSING}", f"M {INTRO}", "M refs.bib"]
        )
        assert status == {}
    tracked = git(git_repo, "ls-files").split()
    assert CLOSING in tracked and CONCLUSION not in tracked


def test_one_git_operation_at_a_time(client: TestClient, git_repo: Path) -> None:
    state = client.app.state.pb  # type: ignore[attr-defined]
    edit(git_repo, INTRO, "Function-as-a-Service", "FaaS")
    state.git.begin("pull")
    try:
        for response in (
            client.post("/api/git/commit", json={"message": "x"}),
            client.post("/api/git/push"),
            client.post("/api/git/pull"),
            client.post("/api/snapshots", json={"note": "v1"}),
        ):
            assert response.status_code == 409, response.text
            assert "already running" in response.json()["detail"]
    finally:
        state.git.end()
    assert client.post("/api/git/commit", json={"message": "x"}).status_code == 200


# --------------------------------------------------------------------------- push / pull


def test_push_streams_output_and_sends_snapshot_tags(client: TestClient, remote: Path) -> None:
    repo = remote.parent / "paper"
    edit(repo, INTRO, "Function-as-a-Service", "FaaS")
    ok(client.post("/api/git/commit", json={"message": "local work"}))
    ok(client.post("/api/snapshots", json={"note": "v1"}))
    drain(client)
    started = ok(client.post("/api/git/push"))
    assert started["started"] is True and started["op"] == "push"
    assert started["command"][:3] == ["git", "push", "--follow-tags"]
    events = wait_git(client)
    outputs = [e for e in events if e["type"] == "git.output"]
    assert outputs and all(e["op"] == "push" and e["line"].strip() for e in outputs)
    assert any("main -> main" in e["line"] for e in outputs)
    finished = events[-1]
    assert finished == {
        "type": "git.finished",
        "op": "push",
        "ok": True,
        "error": None,
        "changed": [],
    }
    refs = git(repo, "ls-remote", str(remote))
    head = git(repo, "rev-parse", "HEAD").strip()
    assert f"{head}\trefs/heads/main" in refs
    assert "refs/tags/rewrite/rev-1-v1" in refs
    assert client.app.state.pb.git.current is None  # type: ignore[attr-defined]


def test_push_sets_the_upstream_on_first_push(
    client: TestClient, git_repo: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "fresh.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(git_repo, "remote", "add", "origin", str(bare))
    assert ok(client.get("/api/git/status"))["upstream"] is None
    drain(client)
    started = ok(client.post("/api/git/push"))
    assert started["command"] == ["git", "push", "--follow-tags", "-u", "origin", "HEAD"]
    assert wait_git(client)[-1]["ok"] is True
    assert ok(client.get("/api/git/status"))["upstream"] == "origin/main"


def test_push_rejected_reports_the_error(client: TestClient, remote: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "x.tex").write_text("x\n")
    git(other, "add", "x.tex")
    git(other, "commit", "-qm", "theirs")
    git(other, "push", "-q")
    repo = remote.parent / "paper"
    edit(repo, INTRO, "Function-as-a-Service", "FaaS")
    ok(client.post("/api/git/commit", json={"message": "mine"}))
    drain(client)
    ok(client.post("/api/git/push"))
    finished = wait_git(client)[-1]
    assert finished["ok"] is False
    assert "rejected" in finished["error"] or "error:" in finished["error"]


def test_push_and_pull_need_a_remote(client: TestClient, git_repo: Path) -> None:
    for op in ("push", "pull"):
        response = client.post(f"/api/git/{op}")
        assert response.status_code == 400 and "no remote" in response.json()["detail"]
    assert client.app.state.pb.git.current is None  # type: ignore[attr-defined]


def _spec_with_says(text: str, says: str) -> str:
    data = json.loads(text)
    data["message"]["says"] = says
    from app.spec import parse_spec_data, serialize

    spec, errors = parse_spec_data(data)
    assert spec is not None, errors
    return serialize(spec)


def test_pull_announces_changed_files_and_the_spec(
    client: TestClient, remote: Path, tmp_path: Path
) -> None:
    repo = remote.parent / "paper"
    state = client.app.state.pb  # type: ignore[attr-defined]
    reanchored: list[str] = []

    async def record(_repo: Repo, path: str) -> int:
        reanchored.append(path)
        return 0

    state.git.reanchor = record
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    edit(other, INTRO, "Function-as-a-Service", "Serverless")
    (other / "sections" / "appendix.tex").write_text("appendix\n")
    git(other, "rm", "-q", "sections/conclusion.tex")
    spec_path = other / "paper.json"
    spec_path.write_text(_spec_with_says(spec_path.read_text(), "Collaborator's message"))
    git(other, "add", "-A")
    git(other, "commit", "-qm", "collaborator")
    git(other, "push", "-q")

    ok(client.get("/api/spec"))
    drain(client)
    started = ok(client.post("/api/git/pull"))
    assert started["command"] == ["git", "-c", "fetch.pruneTags=false", "pull"]
    events = wait_git(client)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)
    assert by_type["git.output"]
    changed = {e["path"]: e for e in by_type["file.changed"]}
    assert set(changed) == {INTRO, "sections/appendix.tex", "sections/conclusion.tex"}
    assert changed[INTRO]["change"] == "modified" and changed[INTRO]["mtime"] > 0
    assert changed["sections/appendix.tex"]["change"] == "added"
    assert changed["sections/conclusion.tex"] == {
        "type": "file.changed",
        "path": "sections/conclusion.tex",
        "change": "deleted",
        "mtime": None,
    }
    assert by_type["spec.changed"] == [{"type": "spec.changed"}]
    finished = by_type["git.finished"][0]
    assert finished["ok"] is True and finished["op"] == "pull"
    assert set(finished["changed"]) == {*changed, "paper.json"}
    assert sorted(reanchored) == ["sections/appendix.tex", "sections/conclusion.tex", INTRO]
    assert "Serverless" in (repo / INTRO).read_text()
    assert ok(client.get("/api/spec"))["spec"]["message"]["says"] == "Collaborator's message"
    events_after = types_after_quiet(client)
    assert "spec.changed" not in events_after


def types_after_quiet(client: TestClient) -> list[str]:
    time.sleep(0.2)
    return [e["type"] for e in drain(client)]


def test_pull_with_nothing_new_announces_nothing(client: TestClient, remote: Path) -> None:
    drain(client)
    ok(client.post("/api/git/pull"))
    events = wait_git(client)
    assert [e["type"] for e in events if e["type"] != "git.output"] == ["git.finished"]
    assert events[-1]["ok"] is True and events[-1]["changed"] == []


def test_pull_keeps_local_snapshot_tags_when_tags_are_pruned(
    client: TestClient, remote: Path
) -> None:
    repo = remote.parent / "paper"
    git(repo, "config", "fetch.prune", "true")
    git(repo, "config", "fetch.pruneTags", "true")
    ok(client.post("/api/snapshots", json={"note": "local only"}))
    drain(client)
    ok(client.post("/api/git/pull"))
    assert wait_git(client)[-1]["ok"] is True
    assert "rewrite/rev-1-local-only" in git(repo, "tag", "-l")


def test_pull_failure_is_reported(client: TestClient, remote: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    edit(other, INTRO, "Function-as-a-Service", "Theirs")
    git(other, "commit", "-qam", "theirs")
    git(other, "push", "-q")
    repo = remote.parent / "paper"
    edit(repo, INTRO, "Function-as-a-Service", "Mine")  # uncommitted: pull must refuse
    drain(client)
    ok(client.post("/api/git/pull"))
    finished = wait_git(client)[-1]
    assert finished["ok"] is False and finished["error"]
    assert "Mine" in (repo / INTRO).read_text()


async def test_remote_timeout_kills_git(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote that never answers: the op times out and its process group is killed."""
    git(git_repo, "remote", "add", "origin", "ssh://example.invalid/repo.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", 'sh -c "sleep 30" --')
    bus = EventBus()
    queue = bus.subscribe()
    manager = GitManager(bus, spec_lock=asyncio.Lock(), timeout_s=0.5)
    started = time.monotonic()
    await manager.start(Repo.at(git_repo), "push")
    await asyncio.wait_for(manager.wait(), 10)
    assert time.monotonic() - started < 8
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    finished = events[-1]
    assert finished["type"] == "git.finished" and finished["ok"] is False
    assert "timed out" in finished["error"]
    assert manager.current is None


async def test_pull_holds_the_source_lock(remote: Path) -> None:
    """Audit 2026-10-18 item 5: a pull rewrites source files, so it takes the repo's source
    lock (after the spec lock), like PUT /api/file and marker insertion: it waits while a
    save holds it, and a save waits for it."""
    repo = Repo.at(remote.parent / "paper")
    lock = asyncio.Lock()
    manager = GitManager(EventBus(), spec_lock=asyncio.Lock(), source_lock=lambda root: lock)
    await lock.acquire()  # a save in progress
    await manager.start(repo, "pull")
    await asyncio.sleep(0.5)
    assert manager.current == "pull" and manager._task is not None
    assert not manager._task.done()  # waiting for the lock, git not even started
    lock.release()
    await asyncio.wait_for(manager.wait(), 30)
    assert manager.current is None and not lock.locked()


def test_error_line() -> None:
    output = (
        "To ../bare.git\n ! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs to '../bare.git'\nhint: Updates were rejected\n"
    )
    assert gitops.error_line(output, "x") == (
        "! [rejected]        main -> main (fetch first) / "
        "error: failed to push some refs to '../bare.git'"
    )
    assert gitops.error_line("fatal: not a git repository\n", "x") == (
        "fatal: not a git repository"
    )
    assert gitops.error_line("", "fallback") == "fallback"
    assert gitops.error_line("one\ntwo\nhint: three\n", "x") == "two"


def test_check_ref() -> None:
    for good in ("HEAD", "HEAD~2", "main", "rewrite/rev-1-x", "a1b2c3", "main@{1}"):
        assert gitops.check_ref(good) == good
    for bad in ("", "-x", "a b", "HEAD:path", "x\ny"):
        with pytest.raises(GitError):
            gitops.check_ref(bad)
