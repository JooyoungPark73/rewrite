"""GET /api/config and PUT /api/config, and the compiler choice following the config."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.compiler import CompileResult
from app.config import CompilerConfig, load_config


@pytest.fixture
def docker_probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Pin the Docker probes so the tests do not depend on this machine's docker."""
    status = {"available": True, "present": False}
    monkeypatch.setattr("app.api.config.docker_available", lambda: status["available"])
    monkeypatch.setattr("app.api.config.image_present", lambda year: status["present"])
    return status


def test_get_config(client: TestClient, docker_probes: dict[str, bool]) -> None:
    body = client.get("/api/config").json()
    styles = body.pop("styles")  # the writing styles copied by init; tested in test_styles.py
    assert {s["name"] for s in styles} >= {"systems", "machine-learning"}
    assert body == {
        "main": "main.tex",
        "paper": {"style": ""},
        "compiler": {
            "kind": "native",
            "texlive_year": 2024,
            "image": "texlive/texlive:TL2024-historic",
        },
        "docker": {"available": True, "image_present": False},
    }
    docker_probes["available"] = False
    docker_probes["present"] = True  # never reported without a working docker
    assert client.get("/api/config").json()["docker"] == {
        "available": False,
        "image_present": False,
    }


def test_get_config_real_probes(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert isinstance(body["docker"]["available"], bool)
    assert isinstance(body["docker"]["image_present"], bool)


def test_put_roundtrip_preserves_other_sections(
    client: TestClient, initialized_repo: Path, docker_probes: dict[str, bool]
) -> None:
    path = initialized_repo / ".rewrite" / "config.toml"
    # a user customisation in [runner.*] and a key this backend does not know
    text = path.read_text()
    text = text.replace('model_flag = "-m"', 'model_flag = "--model-x"')
    text += '\n[custom]\nkeep = "me"\n'
    path.write_text(text)
    before = tomllib.loads(path.read_text())

    response = client.put(
        "/api/config", json={"compiler": {"kind": "docker", "texlive_year": 2023}}
    )
    assert response.status_code == 200
    assert response.json()["compiler"] == {
        "kind": "docker",
        "texlive_year": 2023,
        "image": "texlive/texlive:TL2023-historic",
    }
    assert client.get("/api/config").json() == response.json()

    after = tomllib.loads(path.read_text())
    assert after["compiler"] == {"kind": "docker", "texlive_year": 2023}
    for section in ("paper", "runner", "models", "custom"):
        assert after[section] == before[section], section
    assert after["runner"]["codex"]["model_flag"] == "--model-x"
    assert load_config(path).runner.claude.command == before["runner"]["claude"]["command"]

    # partial update
    client.put("/api/config", json={"compiler": {"kind": "native"}})
    assert tomllib.loads(path.read_text())["compiler"] == {"kind": "native", "texlive_year": 2023}


def test_put_without_changes_keeps_the_file(
    client: TestClient, initialized_repo: Path, docker_probes: dict[str, bool]
) -> None:
    path = initialized_repo / ".rewrite" / "config.toml"
    before = path.read_text()
    assert "# native | docker" in before  # init writes a commented file
    for body in ({"compiler": {"kind": "native", "texlive_year": 2024}}, {"compiler": {}}, {}):
        assert client.put("/api/config", json=body).status_code == 200
    assert path.read_text() == before


@pytest.mark.parametrize(
    "compiler",
    [
        {"kind": "lualatex-in-the-cloud"},
        {"kind": ""},
        {"texlive_year": 2017},
        {"texlive_year": 2031},
        {"kind": "docker", "texlive_year": 1999},
    ],
)
def test_put_rejects_bad_values(
    client: TestClient,
    initialized_repo: Path,
    docker_probes: dict[str, bool],
    compiler: dict[str, object],
) -> None:
    path = initialized_repo / ".rewrite" / "config.toml"
    before = path.read_text()
    response = client.put("/api/config", json={"compiler": compiler})
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    assert path.read_text() == before


def test_put_year_bounds_are_inclusive(client: TestClient, docker_probes: dict[str, bool]) -> None:
    for year in (2018, 2030):
        response = client.put("/api/config", json={"compiler": {"texlive_year": year}})
        assert response.status_code == 200 and response.json()["compiler"]["texlive_year"] == year


def test_put_ignores_an_image_name(
    client: TestClient, initialized_repo: Path, docker_probes: dict[str, bool]
) -> None:
    body = {"compiler": {"kind": "docker", "image": "evil/image:latest", "texlive_year": 2024}}
    response = client.put("/api/config", json=body)
    assert response.json()["compiler"]["image"] == "texlive/texlive:TL2024-historic"
    assert "evil" not in (initialized_repo / ".rewrite" / "config.toml").read_text()


def test_config_errors(bare_client: TestClient, client: TestClient, initialized_repo: Path) -> None:
    assert bare_client.get("/api/config").status_code == 409
    assert bare_client.put("/api/config", json={"compiler": {}}).status_code == 409
    (initialized_repo / ".rewrite" / "config.toml").write_text("not = [valid")
    assert client.get("/api/config").status_code == 400
    assert client.put("/api/config", json={"compiler": {"kind": "docker"}}).status_code == 400


def test_compile_uses_the_compiler_from_config_each_time(
    client: TestClient, docker_probes: dict[str, bool]
) -> None:
    seen: list[CompilerConfig] = []

    class Recorder:
        def compile(self, repo: Path, main: str) -> CompileResult:
            return CompileResult(ok=True, pdf=None, log="", errors=[], duration_ms=0)

    def factory(config: CompilerConfig) -> Recorder:
        seen.append(config)
        return Recorder()

    client.app.state.pb.compiler_factory = factory  # type: ignore[attr-defined]
    client.post("/api/compile")
    client.put("/api/config", json={"compiler": {"kind": "docker", "texlive_year": 2022}})
    client.post("/api/compile")
    assert [(c.kind, c.texlive_year) for c in seen] == [("native", 2024), ("docker", 2022)]


def test_get_compiler_follows_config() -> None:
    from app.compiler import get_compiler
    from app.compiler.docker import DockerLatexmk
    from app.compiler.native import NativeLatexmk

    assert isinstance(get_compiler(CompilerConfig()), NativeLatexmk)
    docker = get_compiler(CompilerConfig(kind="docker", texlive_year=2021, container_root="/w"))
    assert isinstance(docker, DockerLatexmk)
    assert docker.image == "texlive/texlive:TL2021-historic" and docker.container_root == "/w"


def test_health_reports_docker_image(
    client: TestClient, bare_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.repo.image_present", lambda year: year == 2024)
    body = client.get("/api/health").json()
    if body["tools"]["docker"]:
        assert body["docker_image_present"] is True
    else:
        assert body["docker_image_present"] is None
    assert bare_client.get("/api/health").json()["docker_image_present"] is None
