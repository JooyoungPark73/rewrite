from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    Config,
    ConfigError,
    default_config_text,
    load_config,
    parse_config,
    save_config,
)


def test_default_text_matches_model_defaults() -> None:
    assert parse_config(default_config_text()) == Config()


def test_default_text_values() -> None:
    cfg = parse_config(default_config_text("paper/my main.tex"))
    assert cfg.paper.main == "paper/my main.tex"
    assert cfg.compiler.kind == "native" and cfg.compiler.texlive_year == 2024
    assert cfg.runner.claude.command[:2] == ["claude", "-p"]
    assert "--tools" in cfg.runner.claude.command
    assert cfg.runner.codex.model_flag == "-m"
    assert cfg.models.message.thinking == "high"
    assert cfg.models.sentence.thinking == "low"
    assert "verified 2026-10-18 against claude" in default_config_text().lower()
    assert cfg.models.message.model == "opus" and cfg.models.paragraph.model == "sonnet"
    assert cfg.runner.claude.schema_args == ["--json-schema", "{schema}"]
    assert cfg.runner.codex.thinking_args == ["-c", "model_reasoning_effort={thinking}"]


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "config.toml") == Config()


def test_save_load_roundtrip(tmp_path: Path) -> None:
    cfg = Config()
    cfg.compiler.kind = "docker"
    cfg.paper.main = "paper.tex"
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    assert load_config(path) == cfg


def test_invalid_config_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        parse_config("[compiler]\nkind = 'lualatex-in-the-cloud'\n")
    with pytest.raises(ConfigError):
        parse_config("this is not toml")


def test_container_root_is_optional_and_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(path, Config())
    assert "container_root" not in path.read_text()  # TOML has no null: unset keys are omitted
    cfg = Config()
    cfg.compiler.container_root = "/workdir"
    save_config(path, cfg)
    assert load_config(path).compiler.container_root == "/workdir"


PHASE1_CONFIG = """\
[paper]
main = "main.tex"

[runner]
default = "codex"
concurrency = 2

[runner.claude]
command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--tools", "", \
"--no-session-persistence"]
model_flag = "--model"
thinking = { low = "low", medium = "medium", high = "max" }

[runner.codex]
command = ["codex", "exec", "--json"]
model_flag = "-m"
thinking = { low = "low", medium = "medium", high = "high" }

[models]
message = { model = "", thinking = "high" }
sentence = { model = "cheap", thinking = "low" }
"""


def test_phase1_config_gets_phase4_keys_in_memory(tmp_path: Path) -> None:
    """Old `[runner.*]` tables keep their values; missing keys take the per-CLI defaults.
    The file itself is not rewritten."""
    path = tmp_path / "config.toml"
    path.write_text(PHASE1_CONFIG)
    cfg = load_config(path)
    assert cfg.runner.claude.command[0] == "claude" and cfg.runner.claude.thinking["high"] == "max"
    assert cfg.runner.claude.schema_args == ["--json-schema", "{schema}"]
    assert cfg.runner.claude.isolation_args == [
        "--setting-sources",
        "",
        "--strict-mcp-config",
    ]
    assert cfg.runner.claude.suppress_global is True
    assert cfg.runner.codex.command == [
        "codex",
        "exec",
        "--json",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "project_root_markers=[]",
    ]
    assert cfg.runner.codex.no_tools_args == [
        "--disable",
        "shell_tool",
        "--disable",
        "unified_exec",
    ]
    assert cfg.runner.codex.thinking_args == ["-c", "model_reasoning_effort={thinking}"]
    assert cfg.runner.codex.suppress_global is False
    assert cfg.runner.concurrency == 2 and cfg.runner.timeout_s == 1200
    assert (
        cfg.models.message.model == "" and cfg.models.message.runner is None
    )  # → [runner] default
    assert cfg.models.sentence.model == "cheap"
    assert cfg.models.paragraph.model == "sonnet"  # missing table → default
    assert path.read_text() == PHASE1_CONFIG


def test_init_does_not_overwrite_an_old_config(paper_repo: Path) -> None:
    from app.repo import init_repo

    path = paper_repo / ".rewrite" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(PHASE1_CONFIG)
    init_repo(paper_repo)
    assert path.read_text() == PHASE1_CONFIG


def test_concurrency_must_be_positive() -> None:
    with pytest.raises(ConfigError):
        parse_config("[runner]\nconcurrency = 0\n")


PHASE4_RUNNERS = """\
[runner.claude]
command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--tools", "", \
"--no-session-persistence"]
isolation_args = ["--setting-sources", "project", "--strict-mcp-config"]

[runner.codex]
command = ["codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", \
"--ephemeral"]
"""


def test_phase4_config_gets_the_context_boundary_in_memory() -> None:
    """Audit 2026-10-18 item 7: configs written before the fix get the verified
    instruction-discovery boundary when loaded (the file is not rewritten): codex's
    `-c project_doc_max_bytes=0 -c project_root_markers=[]` and claude's
    `--setting-sources ""` instead of the old `project`."""
    cfg = parse_config(PHASE4_RUNNERS)
    assert cfg.runner.codex.command == [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "project_root_markers=[]",
    ]
    assert cfg.runner.claude.isolation_args == ["--setting-sources", "", "--strict-mcp-config"]


def test_explicit_context_settings_are_kept() -> None:
    cfg = parse_config(
        "[runner.claude]\n"
        'isolation_args = ["--setting-sources", "project,local"]\n'
        "[runner.codex]\n"
        'command = ["codex", "exec", "-c", "project_doc_max_bytes=65536"]\n'
    )
    assert cfg.runner.claude.isolation_args == ["--setting-sources", "project,local"]
    assert cfg.runner.codex.command == [
        "codex",
        "exec",
        "-c",
        "project_doc_max_bytes=65536",
        "-c",
        "project_root_markers=[]",
    ]
