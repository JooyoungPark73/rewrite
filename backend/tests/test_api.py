"""HTTP and WebSocket endpoints (00-overview.md "Shared API shape")."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.compiler import CompileResult
from app.main import create_app
from tests.conftest import requires_latexmk


class FakeCompiler:
    """Stands in for latexmk; records how many compiles overlap."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lock = threading.Lock()

    def compile(self, repo: Path, main: str) -> CompileResult:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return CompileResult(ok=True, pdf=None, log="fake", errors=[], duration_ms=1)


# ------------------------------------------------------------------ health and repo


def test_health(client: TestClient, initialized_repo: Path) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert isinstance(body["version"], str)
    assert set(body["tools"]) == {"latexmk", "docker", "claude", "codex", "git"}
    assert all(isinstance(v, bool) for v in body["tools"].values())
    assert body["repo"] == str(initialized_repo.resolve())


def test_get_repo(client: TestClient, initialized_repo: Path) -> None:
    assert client.get("/api/repo").json() == {
        "path": str(initialized_repo.resolve()),
        "main": "main.tex",
        "initialized": True,
    }


def test_no_repo_open(bare_client: TestClient) -> None:
    assert bare_client.get("/api/health").json()["repo"] is None
    for method, url in [
        ("GET", "/api/repo"),
        ("GET", "/api/files"),
        ("GET", "/api/file?path=main.tex"),
        ("POST", "/api/compile"),
        ("GET", "/api/pdf"),
    ]:
        response = bare_client.request(method, url)
        assert response.status_code == 409, url
        assert "detail" in response.json()


def test_open_and_init(bare_client: TestClient, paper_repo: Path, tmp_path: Path) -> None:
    # not initialised yet
    response = bare_client.post("/api/repo/open", json={"path": str(paper_repo)})
    assert response.status_code == 400 and ".rewrite" in response.json()["detail"]
    assert bare_client.post("/api/repo/open", json={"path": "relative/dir"}).status_code == 400
    missing = str(tmp_path / "nope")
    assert bare_client.post("/api/repo/open", json={"path": missing}).status_code == 400
    assert bare_client.post("/api/repo/init", json={"path": missing}).status_code == 400

    info = bare_client.post("/api/repo/init", json={"path": str(paper_repo)}).json()
    assert info == {"path": str(paper_repo.resolve()), "main": "main.tex", "initialized": True}
    assert (paper_repo / ".rewrite" / "config.toml").is_file()
    assert bare_client.get("/api/health").json()["repo"] == str(paper_repo.resolve())

    reopened = bare_client.post("/api/repo/open", json={"path": str(paper_repo)})
    assert reopened.status_code == 200 and reopened.json() == info


def test_invalid_config_is_a_400(client: TestClient, initialized_repo: Path) -> None:
    (initialized_repo / ".rewrite" / "config.toml").write_text("not = [valid")
    response = client.get("/api/repo")
    assert response.status_code == 400 and "config.toml" in response.json()["detail"]


def test_untrusted_host_header_rejected(initialized_repo: Path) -> None:
    app = create_app(initialized_repo, watch=False, frontend_dist=None)
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/api/health").status_code == 400
    with TestClient(app, base_url="http://localhost:5173") as c:
        assert c.get("/api/health").status_code == 200


# ------------------------------------------------------------------ files


def test_files(client: TestClient) -> None:
    body = client.get("/api/files").json()
    assert body["main"] == "main.tex"
    assert [f["path"] for f in body["files"]] == [
        "main.tex",
        "sections/intro.tex",
        "sections/method.tex",
        "sections/results.tex",
        "sections/conclusion.tex",
        "refs.bib",
    ]
    assert body["files"][0]["included_by"] is None
    assert body["files"][1]["included_by"] == "main.tex"
    assert body["missing"] == []


def test_file_roundtrip(client: TestClient, initialized_repo: Path) -> None:
    got = client.get("/api/file", params={"path": "sections/intro.tex"}).json()
    assert set(got) == {"path", "content", "mtime", "sha256"}  # sha256: + revision (item 5)
    assert got["path"] == "sections/intro.tex"
    assert got["content"].startswith("\\section{Introduction}")

    new = got["content"] + "\nAn added sentence.\n"
    saved = client.put("/api/file", json={"path": "sections/intro.tex", "content": new}).json()
    assert set(saved) == {"path", "mtime", "sha256"} and saved["path"] == "sections/intro.tex"
    assert (initialized_repo / "sections" / "intro.tex").read_text() == new
    again = client.get("/api/file", params={"path": "./sections/intro.tex"}).json()
    assert again["content"] == new and again["mtime"] == saved["mtime"]
    assert again["sha256"] == saved["sha256"] != got["sha256"]


@pytest.mark.parametrize("bad", ["../outside.tex", "/etc/passwd", "sections/../../x.tex"])
def test_file_path_escape_is_400(client: TestClient, bad: str) -> None:
    assert client.get("/api/file", params={"path": bad}).status_code == 400
    assert client.put("/api/file", json={"path": bad, "content": "x"}).status_code == 400


def test_file_errors(client: TestClient, initialized_repo: Path) -> None:
    assert client.get("/api/file", params={"path": "nope.tex"}).status_code == 404
    assert client.get("/api/file", params={"path": "sections"}).status_code == 404
    (initialized_repo / "blob.bin").write_bytes(b"\xff\xfe\x00")
    assert client.get("/api/file", params={"path": "blob.bin"}).status_code == 415
    response = client.put("/api/file", json={"path": ".git/config", "content": "x"})
    assert response.status_code == 403
    assert client.get("/api/file").status_code == 422  # missing ?path=


def test_json_body_requires_json_content_type(client: TestClient) -> None:
    # cross-site "simple" requests (text/plain) cannot reach the JSON endpoints
    response = client.put(
        "/api/file",
        content=b'{"path": "main.tex", "content": "pwned"}',
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ compile, pdf, events


def test_compile_emits_events(client: TestClient) -> None:
    fake = FakeCompiler()
    client.app.state.pb.compiler_factory = lambda cfg: fake  # type: ignore[attr-defined]
    with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
        result = client.post("/api/compile").json()
        started = ws.receive_json()
        finished = ws.receive_json()
    assert result == {
        "ok": True,
        "pdf": None,
        "log": "fake",
        "errors": [],
        "duration_ms": 1,
        "synctex": None,
        "exit_code": None,
    }
    assert started == {"type": "compile.started", "main": "main.tex"}
    assert finished == {"type": "compile.finished", "result": result}


def test_ws_rejects_foreign_origin(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "ws://127.0.0.1/ws/events", headers={"origin": "http://evil.example"}
        ):
            pass
    assert excinfo.value.code == 1008
    with client.websocket_connect(
        "ws://127.0.0.1/ws/events", headers={"origin": "http://localhost:5173"}
    ):
        pass


async def test_concurrent_compiles_are_serialized(initialized_repo: Path) -> None:
    app = create_app(initialized_repo, watch=False, frontend_dist=None)
    fake = FakeCompiler(delay=0.2)
    app.state.pb.compiler_factory = lambda cfg: fake
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        responses = await asyncio.gather(*(c.post("/api/compile") for _ in range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert fake.calls == 3 and fake.max_active == 1


def test_unimplemented_compiler_is_reported(client: TestClient) -> None:
    class Unimplemented:
        def compile(self, repo: Path, main: str) -> CompileResult:
            raise NotImplementedError("later")

    client.app.state.pb.compiler_factory = lambda cfg: Unimplemented()  # type: ignore[attr-defined]
    result = client.post("/api/compile").json()
    assert result["ok"] is False
    assert "not implemented yet (later)" in result["errors"][0]["message"]


def test_pdf_404_before_build(client: TestClient) -> None:
    response = client.get("/api/pdf")
    assert response.status_code == 404 and "detail" in response.json()


@requires_latexmk
def test_compile_and_fetch_pdf(client: TestClient, initialized_repo: Path) -> None:
    result = client.post("/api/compile").json()
    assert result["ok"] is True, result["errors"]
    assert result["pdf"] == ".rewrite/build/main.pdf"
    response = client.get("/api/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.content[:5] == b"%PDF-"


# ------------------------------------------------------------------ root / static frontend


def test_root_without_frontend(bare_client: TestClient) -> None:
    body = bare_client.get("/").json()
    assert body["health"] == "/api/health"


def test_root_serves_built_frontend(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>pb</title>")
    app = create_app(None, watch=False, frontend_dist=dist)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert "<title>pb</title>" in c.get("/").text
        assert c.get("/api/health").json()["ok"] is True
