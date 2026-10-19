"""DockerLatexmk, image checks and `docker pull` streaming.

Most tests use a fake `docker` script. The real compiles at the end run only with
REWRITE_TEST_DOCKER=1 and the TeX Live image already pulled (they never pull it).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.compiler.docker import (
    DockerLatexmk,
    docker_available,
    docker_image,
    image_present,
    pull,
)
from app.events import DockerPull, DockerPullFinished
from app.synctex import docker_runner, forward, reverse
from tests.conftest import fake_exe, make_client, requires_docker

YEAR = 2024
IMAGE = "texlive/texlive:TL2024-historic"
UID_GID = f"{os.getuid()}:{os.getgid()}"


def test_image_name() -> None:
    assert docker_image(2024) == IMAGE
    assert DockerLatexmk(2019).image == "texlive/texlive:TL2019-historic"


def test_command(tmp_path: Path) -> None:
    cmd = DockerLatexmk(YEAR).command(tmp_path, "main.tex", name="rewrite-x")
    assert cmd == [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        "rewrite-x",
        "-v",
        f"{tmp_path}:{tmp_path}",
        "-w",
        str(tmp_path),
        "-u",
        UID_GID,
        "-e",
        "HOME=/tmp",
        "-e",
        "max_print_line=10000",
        "-e",
        "error_line=254",
        "-e",
        "half_error_line=238",
        IMAGE,
        "latexmk",
        "-pdf",
        "-synctex=1",
        "-interaction=nonstopmode",
        "-output-directory=.rewrite/build",
        "main.tex",
    ]


def test_command_with_container_root(tmp_path: Path) -> None:
    cmd = DockerLatexmk(YEAR, container_root="/workdir/").command(tmp_path, "main.tex")
    assert cmd[cmd.index("-v") + 1] == f"{tmp_path}:/workdir"
    assert cmd[cmd.index("-w") + 1] == "/workdir"
    assert "--name" not in cmd


def test_missing_docker(initialized_repo: Path) -> None:
    result = DockerLatexmk(YEAR, docker="no-such-docker").compile(initialized_repo, "main.tex")
    assert not result.ok and result.pdf is None
    assert result.errors[0].message == "docker was not found on PATH"


def test_missing_main(initialized_repo: Path, tmp_path: Path) -> None:
    docker = fake_exe(tmp_path / "bin", "docker", "exit 0\n")
    result = DockerLatexmk(YEAR, docker=str(docker)).compile(initialized_repo, "nope.tex")
    assert result.errors[0].message == "main file not found: nope.tex"


def test_missing_image_is_explained(initialized_repo: Path, tmp_path: Path) -> None:
    docker = fake_exe(
        tmp_path / "bin",
        "docker",
        f"echo 'docker: Error response from daemon: No such image: {IMAGE}' >&2\nexit 125\n",
    )
    # a stale PDF from an earlier compile must not be reported as this compile's output
    build = initialized_repo / ".rewrite" / "build"
    (build / "main.pdf").write_bytes(b"%PDF-old")
    result = DockerLatexmk(YEAR, docker=str(docker)).compile(initialized_repo, "main.tex")
    assert not result.ok and result.pdf is None
    assert len(result.errors) == 1
    assert f"Docker image {IMAGE} is not present; pull it first" in result.errors[0].message
    assert "No such image" in result.log


def test_log_is_parsed_with_container_paths(initialized_repo: Path, tmp_path: Path) -> None:
    """A fake container that leaves a log, a PDF and SyncTeX data like latexmk would."""
    script = """\
mkdir -p .rewrite/build
printf '%s\\n' \
  'This is pdfTeX, Version 3.141592653-2.6-1.40.26 (TeX Live 2024) (preloaded format=pdflatex)' \
  '(/workdir/./main.tex (/workdir/./sections/intro.tex' \
  '! Undefined control sequence.' \
  'l.3 \\foo' \
  '' \
  ') )' > .rewrite/build/main.log
printf '%%PDF-1.5\\n' > .rewrite/build/main.pdf
printf 'x' | gzip > .rewrite/build/main.synctex.gz
exit 12
"""
    docker = fake_exe(tmp_path / "bin", "docker", script)
    result = DockerLatexmk(YEAR, container_root="/workdir", docker=str(docker)).compile(
        initialized_repo, "main.tex"
    )
    assert not result.ok and result.exit_code == 12
    assert result.pdf == ".rewrite/build/main.pdf"
    assert result.synctex == ".rewrite/build/main.synctex.gz"
    errors = [(e.file, e.line, e.message) for e in result.errors]
    assert ("sections/intro.tex", 3, "Undefined control sequence: \\foo") in errors


def test_timeout_kills_the_container(initialized_repo: Path, tmp_path: Path) -> None:
    killed = tmp_path / "killed.txt"
    script = f"""\
if [ "$1" = kill ]; then echo "$2" >> {killed}; exit 0; fi
exec sleep 30
"""
    docker = fake_exe(tmp_path / "bin", "docker", script)
    compiler = DockerLatexmk(YEAR, timeout_s=0.5, docker=str(docker))
    result = compiler.compile(initialized_repo, "main.tex")
    assert not result.ok and result.exit_code is None
    assert any("timed out" in e.message for e in result.errors)
    assert killed.read_text().startswith("rewrite-")


def test_image_checks(tmp_path: Path) -> None:
    present = fake_exe(tmp_path / "a", "docker", "exit 0\n")
    absent = fake_exe(tmp_path / "b", "docker", "exit 1\n")
    assert image_present(YEAR, docker=str(present)) is True
    assert image_present(YEAR, docker=str(absent)) is False
    assert image_present(YEAR, docker="no-such-docker") is False
    assert docker_available(str(present)) is True
    assert docker_available(str(absent)) is False  # CLI present, daemon not answering
    assert docker_available("no-such-docker") is False


def test_image_check_arguments(tmp_path: Path) -> None:
    argv = tmp_path / "argv.txt"
    docker = fake_exe(tmp_path / "bin", "docker", f'echo "$@" >> {argv}\n')
    image_present(YEAR, docker=str(docker))
    docker_available(str(docker))
    assert argv.read_text().splitlines() == [
        f"image inspect --format {{{{.Id}}}} {IMAGE}",
        "version --format {{.Server.Version}}",
    ]


# ------------------------------------------------------------------ pull


async def test_pull_streams_lines(tmp_path: Path) -> None:
    script = f"""\
[ "$1 $2" = "pull {IMAGE}" ] || exit 3
echo 'TL2024-historic: Pulling from texlive/texlive'
echo 'abc123: Pulling fs layer'
echo ''
echo 'Status: Downloaded newer image for {IMAGE}'
"""
    docker = fake_exe(tmp_path / "bin", "docker", script)
    events: list[object] = []
    assert await pull(YEAR, events.append, docker=str(docker)) is True
    assert events == [
        DockerPull(line="TL2024-historic: Pulling from texlive/texlive", image=IMAGE),
        DockerPull(line="abc123: Pulling fs layer", image=IMAGE),
        DockerPull(line=f"Status: Downloaded newer image for {IMAGE}", image=IMAGE),
        DockerPullFinished(ok=True, error=None, image=IMAGE),
    ]


async def test_pull_failure(tmp_path: Path) -> None:
    docker = fake_exe(
        tmp_path / "bin",
        "docker",
        "echo 'Error response from daemon: manifest unknown' >&2\nexit 1\n",
    )
    events: list[object] = []
    assert await pull(2017, events.append, docker=str(docker)) is False
    image = "texlive/texlive:TL2017-historic"
    assert events[-1] == DockerPullFinished(
        ok=False, error="Error response from daemon: manifest unknown", image=image
    )
    events.clear()
    assert await pull(YEAR, events.append, docker="no-such-docker") is False
    assert events == [
        DockerPullFinished(ok=False, error="docker was not found on PATH", image=IMAGE)
    ]


async def test_pull_cancel(tmp_path: Path) -> None:
    docker = fake_exe(tmp_path / "bin", "docker", "echo started\nexec sleep 30\n")
    events: list[object] = []
    task = asyncio.create_task(pull(YEAR, events.append, docker=str(docker)))
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[0] == DockerPull(line="started", image=IMAGE)
    assert events[-1] == DockerPullFinished(ok=False, error="pull cancelled", image=IMAGE)


# ------------------------------------------------------------------ POST /api/compile/docker/pull


class FakePull:
    """Stands in for app.compiler.docker.pull; records overlap like FakeCompiler."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.years: list[int] = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, year: int, publish) -> bool:  # type: ignore[no-untyped-def]
        self.years.append(year)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        publish(DockerPull(line=f"pulling {year}", image=docker_image(year)))
        await asyncio.sleep(self.delay)
        self.active -= 1
        publish(DockerPullFinished(ok=True, error=None, image=docker_image(year)))
        return True


def _fake_docker_state(client: TestClient, fake: FakePull) -> None:
    state = client.app.state.pb  # type: ignore[attr-defined]
    state.pull_fn = fake
    state.docker_cli = shutil.which("sh") or "/bin/sh"  # any executable passes the PATH check


def test_pull_endpoint_streams_events(client: TestClient) -> None:
    fake = FakePull()
    _fake_docker_state(client, fake)
    with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
        response = client.post("/api/compile/docker/pull")
        assert response.status_code == 200
        assert response.json() == {"started": True, "image": IMAGE, "already_running": False}
        assert ws.receive_json() == {"type": "docker.pull", "line": "pulling 2024", "image": IMAGE}
        assert ws.receive_json() == {
            "type": "docker.pull.finished",
            "ok": True,
            "error": None,
            "image": IMAGE,
        }
    assert fake.years == [2024]


async def test_pulls_are_serialized_and_coalesced(initialized_repo: Path) -> None:
    import httpx

    from app.main import create_app

    app = create_app(initialized_repo, watch=False, frontend_dist=None)
    fake = FakePull(delay=0.2)
    state = app.state.pb
    state.pull_fn = fake
    state.docker_cli = shutil.which("sh") or "/bin/sh"
    config = initialized_repo / ".rewrite" / "config.toml"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        first = (await c.post("/api/compile/docker/pull")).json()
        again = (await c.post("/api/compile/docker/pull")).json()
        config.write_text(config.read_text().replace("texlive_year = 2024", "texlive_year = 2023"))
        other = (await c.post("/api/compile/docker/pull")).json()
        for _ in range(100):
            if not state.pulls_running:
                break
            await asyncio.sleep(0.05)
    assert first == {"started": True, "image": IMAGE, "already_running": False}
    assert again == {"started": True, "image": IMAGE, "already_running": True}
    assert other["image"] == "texlive/texlive:TL2023-historic" and not other["already_running"]
    assert fake.years == [2024, 2023]
    assert fake.max_active == 1


def test_pull_endpoint_errors(client: TestClient, bare_client: TestClient) -> None:
    assert bare_client.post("/api/compile/docker/pull").status_code == 409
    state = client.app.state.pb  # type: ignore[attr-defined]
    state.docker_cli = "no-such-docker"
    response = client.post("/api/compile/docker/pull")
    assert response.status_code == 503 and "docker" in response.json()["detail"]
    _fake_docker_state(client, FakePull())
    foreign = client.post("/api/compile/docker/pull", headers={"origin": "http://evil.example"})
    assert foreign.status_code == 403
    local = client.post("/api/compile/docker/pull", headers={"origin": "http://localhost:5173"})
    assert local.status_code == 200


# ------------------------------------------------------------------ real Docker compiles


def _require_image() -> None:
    if not image_present(YEAR):
        pytest.skip(f"{IMAGE} is not pulled; run `docker pull {IMAGE}` first")


@requires_docker
def test_docker_compile_sample(initialized_repo: Path) -> None:
    _require_image()
    result = DockerLatexmk(YEAR).compile(initialized_repo, "main.tex")
    assert result.ok, (result.errors, result.log[-2000:])
    assert result.exit_code == 0
    assert result.pdf == ".rewrite/build/main.pdf"
    assert result.synctex == ".rewrite/build/main.synctex.gz"
    assert (initialized_repo / result.pdf).read_bytes()[:5] == b"%PDF-"
    assert "TeX Live 2024" in result.log  # it really ran in the pinned image
    assert [e for e in result.errors if e.severity != "info"] == []

    build = initialized_repo / ".rewrite" / "build"
    for path in build.iterdir():
        assert path.stat().st_uid == os.getuid(), f"{path.name} is not owned by the user"
        assert path.stat().st_gid == os.getgid()

    # Forward and reverse sync, with synctex inside the same image (the no-host-synctex path)
    run = docker_runner(initialized_repo.resolve(), IMAGE)
    boxes = forward(initialized_repo, build, "main.tex", "sections/intro.tex", 4, run=run)
    assert boxes and boxes[0].page == 1
    box = boxes[0]
    found = reverse(
        initialized_repo, build, "main.tex", 1, box.x + box.w / 2, box.y + box.h / 2, run=run
    )
    assert found is not None and found.file == "sections/intro.tex" and found.line == 4

    # and with the host's synctex, when there is one
    if shutil.which("synctex"):
        assert forward(initialized_repo, build, "main.tex", "sections/intro.tex", 4) == boxes


@requires_docker
def test_docker_error_has_file_and_line(initialized_repo: Path) -> None:
    _require_image()
    method = initialized_repo / "sections" / "method.tex"
    lines = method.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("Figure~\\ref"))
    lines.insert(index, "\\undefinedmacro")
    method.write_text("\n".join(lines) + "\n")
    result = DockerLatexmk(YEAR).compile(initialized_repo, "main.tex")
    assert not result.ok
    errors = [(e.file, e.line, e.message) for e in result.errors if e.severity == "error"]
    assert ("sections/method.tex", index + 1, "Undefined control sequence: \\undefinedmacro") in (
        errors
    )


@requires_docker
def test_docker_via_api_without_host_synctex(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual check 3, headless: switch to docker, compile, sync both ways."""
    _require_image()
    real_which = shutil.which
    monkeypatch.setattr(
        "app.synctex.shutil.which",
        lambda name, *a, **k: None if name == "synctex" else real_which(name, *a, **k),
    )
    with make_client(initialized_repo) as c:
        put = c.put("/api/config", json={"compiler": {"kind": "docker", "texlive_year": YEAR}})
        assert put.status_code == 200
        assert put.json()["docker"] == {"available": True, "image_present": True}
        result = c.post("/api/compile").json()
        assert result["ok"] is True, result["errors"]
        assert "TeX Live 2024" in result["log"]
        boxes = c.get(
            "/api/synctex/forward", params={"file": "sections/intro.tex", "line": 22}
        ).json()["boxes"]
        assert boxes
        box = boxes[0]
        found = c.get(
            "/api/synctex/reverse",
            params={"page": box["page"], "x": box["x"] + 5, "y": box["y"] + box["h"] / 2},
        ).json()
        assert found["file"] == "sections/intro.tex" and abs(found["line"] - 22) <= 1
