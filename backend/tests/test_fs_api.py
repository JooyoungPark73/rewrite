"""GET /api/fs/dirs: the folder listing behind the Open dialog's browser."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _listing(client: TestClient, path: str | None = None) -> dict:
    res = client.get("/api/fs/dirs", params={} if path is None else {"path": path})
    assert res.status_code == 200, res.text
    return res.json()


def test_default_is_home(bare_client: TestClient) -> None:
    body = _listing(bare_client)
    assert body["path"] == str(Path.home().resolve())
    assert body["home"] == str(Path.home())
    assert body["parent"] == str(Path.home().resolve().parent)
    assert all(not d["name"].startswith(".") for d in body["dirs"])


def test_lists_folders_with_flags(
    bare_client: TestClient, tmp_path: Path, initialized_repo: Path
) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "tex-only").mkdir()
    (tmp_path / "tex-only" / "main.tex").write_text("x")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a-file.txt").write_text("not a folder")
    os.symlink(initialized_repo, tmp_path / "linked-paper")

    body = _listing(bare_client, str(tmp_path))
    assert body["path"] == str(tmp_path.resolve())
    names = [d["name"] for d in body["dirs"]]
    # Sorted, no hidden folders, no files; the symlinked folder is listed.
    assert names == sorted(names, key=str.lower)
    assert ".hidden" not in names and "a-file.txt" not in names
    by_name = {d["name"]: d for d in body["dirs"]}
    assert by_name["plain"] == {
        "name": "plain",
        "path": str(tmp_path / "plain"),
        "initialized": False,
        "has_tex": False,
        "is_git": False,
    }
    assert by_name["tex-only"]["has_tex"] is True
    assert by_name["tex-only"]["initialized"] is False
    assert by_name["linked-paper"]["initialized"] is True
    assert by_name["linked-paper"]["has_tex"] is True
    # The initialised repo itself: its own listing shows `sections`, not `.rewrite`.
    inner = _listing(bare_client, str(initialized_repo))
    assert "sections" in [d["name"] for d in inner["dirs"]]
    assert ".rewrite" not in [d["name"] for d in inner["dirs"]]


def test_tilde_and_trailing_slash(bare_client: TestClient) -> None:
    assert _listing(bare_client, "~")["path"] == str(Path.home().resolve())
    assert _listing(bare_client, "~/")["path"] == str(Path.home().resolve())


def test_root_has_no_parent(bare_client: TestClient) -> None:
    body = _listing(bare_client, "/")
    assert body["path"] == "/"
    assert body["parent"] is None


def test_errors(bare_client: TestClient, tmp_path: Path) -> None:
    assert bare_client.get("/api/fs/dirs", params={"path": "relative/dir"}).status_code == 400
    assert (
        bare_client.get("/api/fs/dirs", params={"path": str(tmp_path / "missing")}).status_code
        == 404
    )
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert bare_client.get("/api/fs/dirs", params={"path": str(f)}).status_code == 404
    # Every error carries a string `detail`.
    for bad in ("relative/dir", str(tmp_path / "missing"), str(f)):
        detail = bare_client.get("/api/fs/dirs", params={"path": bad}).json()["detail"]
        assert isinstance(detail, str) and detail


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a non-root POSIX user"
)
def test_unreadable_directory_is_403(bare_client: TestClient, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        res = bare_client.get("/api/fs/dirs", params={"path": str(locked)})
        assert res.status_code == 403
    finally:
        locked.chmod(0o755)


def test_listing_works_with_a_repo_open(client: TestClient, initialized_repo: Path) -> None:
    body = _listing(client, str(initialized_repo.parent))
    assert any(d["path"] == str(initialized_repo) and d["initialized"] for d in body["dirs"])
