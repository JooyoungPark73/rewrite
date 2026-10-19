"""Shared fixtures: a throwaway copy of examples/sample-paper and API clients."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repo import init_repo

APP_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = APP_ROOT / "examples" / "sample-paper"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

requires_latexmk = pytest.mark.skipif(
    shutil.which("latexmk") is None, reason="latexmk is not installed"
)
requires_synctex = pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("synctex") is None,
    reason="latexmk or synctex is not installed",
)
# Real Docker compiles pull nothing and need the TeX Live image already present; opt in with
# REWRITE_TEST_DOCKER=1 (see tests/test_docker.py).
requires_docker = pytest.mark.skipif(
    os.environ.get("REWRITE_TEST_DOCKER", "").strip() not in ("1", "true", "yes")
    or shutil.which("docker") is None,
    reason="set REWRITE_TEST_DOCKER=1 (and install docker) to run Docker compiles",
)


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git call in the tests (and in the app under test) ignores the user's global and
    system git config, and commits as a fixed test identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test Author")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git in a test repo and return stdout."""
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def git_repo(initialized_repo: Path) -> Path:
    """The initialised sample paper as a git repo (branch main) with one commit."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    git(initialized_repo, "init", "-q", "-b", "main")
    git(initialized_repo, "add", "-A")
    git(initialized_repo, "commit", "-q", "-m", "initial")
    return initialized_repo


@pytest.fixture
def remote(git_repo: Path, tmp_path: Path) -> Path:
    """A local bare repo as `origin`, with main pushed and tracked."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(git_repo, "remote", "add", "origin", str(bare))
    git(git_repo, "push", "-q", "-u", "origin", "main")
    return bare


def fake_exe(directory: Path, name: str, body: str) -> Path:
    """An executable /bin/sh script standing in for a CLI (docker, synctex, latexmk)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def paper_repo(tmp_path: Path) -> Path:
    """A tmp paper repo: main.tex, sections/*.tex and refs.bib (not initialised)."""
    dest = tmp_path / "paper"
    shutil.copytree(EXAMPLE, dest)
    return dest


@pytest.fixture
def initialized_repo(paper_repo: Path) -> Path:
    init_repo(paper_repo)
    return paper_repo


def make_client(repo: Path | None) -> TestClient:
    app = create_app(repo, watch=False, frontend_dist=None)
    # The app only trusts 127.0.0.1 / localhost Host headers.
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client(initialized_repo: Path) -> Iterator[TestClient]:
    with make_client(initialized_repo) as c:
        yield c


@pytest.fixture
def bare_client() -> Iterator[TestClient]:
    """A server with no repo open."""
    with make_client(None) as c:
        yield c


@pytest.fixture(scope="module")
def compiled_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The sample paper, initialised and compiled natively once per test module.

    Read-only for tests: SyncTeX records absolute paths, so the build cannot be copied.
    """
    if shutil.which("latexmk") is None:
        pytest.skip("latexmk is not installed")
    from app.compiler.native import NativeLatexmk

    dest = tmp_path_factory.mktemp("compiled") / "paper"
    shutil.copytree(EXAMPLE, dest)
    init_repo(dest)
    result = NativeLatexmk().compile(dest, "main.tex")
    assert result.ok, result.errors
    return dest.resolve()


def adopt_intro(client: TestClient) -> tuple[str, list[str]]:
    """Adopt `\\section{Introduction}` and its four paragraphs (in document order) through
    the spec API. Returns (section id, paragraph ids). Afterwards sections/intro.tex has the
    markers on lines 4, 12, 18 and 25, each paragraph's text right below its marker (text on
    5-10, 13-16, 19-23 and 26-29)."""
    skeleton = client.get("/api/spec/skeleton").json()
    intro = next(s for s in skeleton["sections"] if s["heading"] == "Introduction")
    response = client.post(
        "/api/spec/adopt", json={"kind": "section", "file": intro["file"], "line": intro["line"]}
    )
    assert response.status_code == 200, response.text
    section_id = response.json()["new_id"]
    paragraph_ids = []
    while True:
        paragraphs = [
            p
            for p in client.get("/api/spec/skeleton").json()["paragraphs"]
            if p["file"] == "sections/intro.tex" and p["marker_id"] is None
        ]
        if not paragraphs:
            break
        response = client.post(
            "/api/spec/adopt",
            json={
                "kind": "paragraph",
                "file": "sections/intro.tex",
                "line_start": paragraphs[0]["line_start"],
                "parent_id": section_id,
            },
        )
        assert response.status_code == 200, response.text
        paragraph_ids.append(response.json()["new_id"])
    return section_id, paragraph_ids
