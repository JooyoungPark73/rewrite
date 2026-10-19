"""`.rewrite/config.toml`: pydantic model, defaults, load and save.

The file is read with `tomllib` and written with `tomli-w`. `rewrite init` writes
`DEFAULT_CONFIG_TOML` verbatim instead of serialising the model, because that text carries
comments (tomli-w cannot write comments). A test asserts the two stay in sync.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Thinking = Literal["low", "medium", "high", "xhigh", "max"]


class _Section(BaseModel):
    # Unknown keys are kept (not rejected) so a config written by a later phase
    # round-trips through an older backend without losing data.
    model_config = ConfigDict(extra="allow")


class PaperConfig(_Section):
    main: str = "main.tex"
    # A writing style: the name of a file in .rewrite/prompts/styles/ (without .md) whose
    # body is appended to system.md in every review pass as the field's standard of
    # evidence. Empty = none.
    style: str = ""


CompilerKind = Literal["native", "docker"]
COMPILER_KINDS: tuple[str, ...] = ("native", "docker")
TEXLIVE_YEAR_MIN = 2018  # range accepted by PUT /api/config
TEXLIVE_YEAR_MAX = 2030


class CompilerConfig(_Section):
    kind: CompilerKind = "native"
    texlive_year: int = 2024  # docker only
    # Docker only, normally unset: the repo is bind-mounted at its own host path, so paths in
    # the log and the .synctex.gz need no translation. Set this to mount it elsewhere inside
    # the container; log and SyncTeX paths under it are then mapped back to the repo.
    container_root: str | None = None


RunnerName = Literal["claude", "codex"]
RUNNER_NAMES: tuple[str, ...] = ("claude", "codex")
# Both installed CLIs accept all five (claude 2.1.303 `--effort`; codex 0.163.0
# `model_reasoning_effort`, whose API answers "Supported values are: none, minimal, low,
# medium, high, xhigh, max" for an unknown one). A level missing from a repo's
# `[runner.<x>].thinking` table is passed to the CLI as is (`RunnerCliConfig.thinking.get`).
THINKING_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
PASS_LEVELS: tuple[str, ...] = ("message", "claim", "section", "paragraph", "sentence")


class RunnerCliConfig(_Section):
    """How to run one LLM CLI (phase 4; verified against the installed CLIs).

    Every run is `command` plus the optional argument groups below. A group is added only
    when the installed CLI's `--help` lists every flag in it (probed once per process, see
    app/runner/base.py `probe_cli`), so a config written for a newer CLI degrades instead of
    breaking. `no_tools_args` is the exception: it is always added, so a CLI that cannot
    disable its tools fails loudly instead of running with them.

    Placeholders: `{thinking}` (the mapped thinking value), `{schema}` (the findings JSON
    schema as text) and `{schema_file}` (a temp file holding it).
    """

    command: list[str]
    model_flag: str = ""  # followed by the model; no flag at all when the model is empty
    thinking_args: list[str] = Field(default_factory=list)
    thinking: dict[Thinking, str] = Field(
        default_factory=lambda: {"low": "low", "medium": "medium", "high": "high"}
    )
    schema_args: list[str] = Field(default_factory=list)  # structured output
    no_tools_args: list[str] = Field(default_factory=list)  # always added
    isolation_args: list[str] = Field(default_factory=list)  # skip user-level settings
    # Whether `isolation_args` keep the user's global instruction file (~/.claude/CLAUDE.md,
    # ~/.codex/AGENTS.md) out of the run on the installed CLI version. A recorded fact, not a
    # switch; GET /api/runners reports it only when the CLI also accepts `isolation_args`.
    suppress_global: bool = False
    stream_args: list[str] = Field(default_factory=list)  # finer-grained progress events
    models: list[str] = Field(default_factory=list)  # suggestions for the UI (free text allowed)


# Verified 2026-10-18 on claude 2.1.302 (`claude --help` and real runs, see the phase-4 brief).
CLAUDE_DEFAULTS: dict[str, Any] = {
    "command": [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--no-session-persistence",
    ],
    "model_flag": "--model",
    "thinking_args": ["--effort", "{thinking}"],
    "thinking": {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"},
    "schema_args": ["--json-schema", "{schema}"],
    "no_tools_args": [],  # `--tools ""` is part of the command
    # `--setting-sources ""`: no user, project or local source. With `project` claude loads
    # every CLAUDE.md from the sandbox up to / (the paper root's too); see DEFAULT_CONFIG_TOML.
    "isolation_args": ["--setting-sources", "", "--strict-mcp-config"],
    "suppress_global": True,
    "stream_args": ["--include-partial-messages"],
    "models": ["opus", "sonnet", "haiku"],
}

# codex options that keep instruction files outside the sandbox out of a run (audit
# 2026-10-18 item 7, verified on codex-cli 0.163.0): no project docs (AGENTS.md), and the
# sandbox as the project root, so nothing above it (AGENTS.md, .agents/skills,
# .codex/skills of the paper repo) is discovered. Part of the codex command; configs that
# do not set these keys get them when loaded (`RunnerConfig._fill_missing_keys`).
CODEX_CONTEXT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("project_doc_max_bytes", "0"),
    ("project_root_markers", "[]"),
)
# The phase-4 default, replaced in memory by CLAUDE_DEFAULTS["isolation_args"].
PHASE4_CLAUDE_ISOLATION = ["--setting-sources", "project", "--strict-mcp-config"]

# Verified 2026-10-18 on codex-cli 0.162.1 (`codex exec --help` and real runs).
CODEX_DEFAULTS: dict[str, Any] = {
    "command": [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        *(arg for key, value in CODEX_CONTEXT_OPTIONS for arg in ("-c", f"{key}={value}")),
    ],
    "model_flag": "-m",
    "thinking_args": ["-c", "model_reasoning_effort={thinking}"],
    "thinking": {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"},
    "schema_args": ["--output-schema", "{schema_file}"],
    # `--sandbox read-only` still lets the model run (read-only) shell commands; these
    # feature switches remove the shell tools.
    "no_tools_args": ["--disable", "shell_tool", "--disable", "unified_exec"],
    "isolation_args": ["--ignore-user-config", "--ignore-rules"],
    # codex injects $CODEX_HOME/AGENTS.md whatever the flags; nothing turns it off.
    "suppress_global": False,
    "stream_args": [],
    # `codex debug models`, visibility "list", on codex-cli 0.162.1.
    "models": [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ],
}

RUNNER_DEFAULTS: dict[str, dict[str, Any]] = {"claude": CLAUDE_DEFAULTS, "codex": CODEX_DEFAULTS}


def cli_config_overrides(argv: list[str]) -> dict[str, str]:
    """codex `-c key=value` / `--config key=value` / `--config=key=value` overrides in an
    argv, as {key: raw value text}; a later one wins, like in codex."""
    out: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        raw: str | None = None
        if token in ("-c", "--config") and index + 1 < len(argv):
            raw = argv[index + 1]
            index += 1
        elif token.startswith("--config="):
            raw = token.removeprefix("--config=")
        index += 1
        if raw is not None and "=" in raw:
            key, _, value = raw.partition("=")
            out[key.strip()] = value.strip()
    return out


def runner_default(name: str) -> RunnerCliConfig:
    return RunnerCliConfig.model_validate(copy.deepcopy(RUNNER_DEFAULTS[name]))


class RunnerConfig(_Section):
    default: RunnerName = "claude"
    concurrency: int = Field(default=3, ge=1, le=16)
    timeout_s: float = Field(default=1200.0, gt=0)  # hard limit per run (20 minutes)
    # False: the CLIs run on the user's claude.ai / ChatGPT login; API-key and cloud-provider
    # variables (app/runner/base.py API_KEY_ENV) are stripped from their environment.
    allow_api_keys: bool = False
    claude: RunnerCliConfig = Field(default_factory=lambda: runner_default("claude"))
    codex: RunnerCliConfig = Field(default_factory=lambda: runner_default("codex"))

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_keys(cls, data: Any) -> Any:
        """Keys missing from a `[runner.claude]` / `[runner.codex]` table take the defaults
        for that CLI. Repos initialised in phase 1 have tables with only `command`,
        `model_flag` and `thinking`; they get the phase-4 keys in memory. The file is not
        rewritten (`rewrite init` never overwrites config.toml).

        The instruction-discovery boundary (audit 2026-10-18 item 7) is filled in the same
        way: a codex command that does not set one of CODEX_CONTEXT_OPTIONS gets
        `-c <key>=<value>` appended (set the key to another value to opt out), and claude's
        phase-4 `isolation_args` (`--setting-sources project`) become the current default."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name, defaults in RUNNER_DEFAULTS.items():
            table = data.get(name)
            if isinstance(table, dict):
                data[name] = {**copy.deepcopy(defaults), **table}
        codex = data.get("codex")
        if isinstance(codex, dict) and isinstance(codex.get("command"), list):
            command = list(codex["command"])
            present = cli_config_overrides([str(arg) for arg in command])
            for key, value in CODEX_CONTEXT_OPTIONS:
                if key not in present:
                    command += ["-c", f"{key}={value}"]
            codex["command"] = command
        claude = data.get("claude")
        if isinstance(claude, dict) and claude.get("isolation_args") == PHASE4_CLAUDE_ISOLATION:
            claude["isolation_args"] = list(CLAUDE_DEFAULTS["isolation_args"])
        return data

    def cli(self, name: str) -> RunnerCliConfig:
        if name == "claude":
            return self.claude
        if name == "codex":
            return self.codex
        raise KeyError(name)


class LevelModel(_Section):
    runner: RunnerName | None = None  # None → [runner] default
    model: str = ""  # empty → the runner CLI's own default model
    thinking: Thinking = "medium"


def _level(model: str, thinking: Thinking) -> LevelModel:
    return LevelModel(runner="claude", model=model, thinking=thinking)


class ModelsConfig(_Section):
    """Model tiering by pass level (PLAN.md "Prompt templates")."""

    message: LevelModel = Field(default_factory=lambda: _level("opus", "high"))
    claim: LevelModel = Field(default_factory=lambda: _level("opus", "high"))
    section: LevelModel = Field(default_factory=lambda: _level("opus", "high"))
    paragraph: LevelModel = Field(default_factory=lambda: _level("sonnet", "medium"))
    sentence: LevelModel = Field(default_factory=lambda: _level("sonnet", "low"))

    def for_level(self, level: str) -> LevelModel:
        value = getattr(self, level, None)
        return value if isinstance(value, LevelModel) else LevelModel()


class Config(_Section):
    paper: PaperConfig = Field(default_factory=PaperConfig)
    compiler: CompilerConfig = Field(default_factory=CompilerConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)


DEFAULT_CONFIG_TOML = """\
# rewrite configuration for this paper. Lives in the paper repo and is
# versioned with it. Edit freely; `rewrite init` never overwrites this file.
# Keys missing from an older file take the defaults below when it is loaded.

[paper]
main = "{main}"
# Writing style: a file in .rewrite/prompts/styles/ without .md (systems, machine-learning,
# ...), appended to system.md in every review pass; empty = none.
style = ""

[compiler]
kind = "native"            # native | docker
texlive_year = 2024        # docker only (texlive/texlive:TL<year>-historic)

[runner]
default = "claude"
concurrency = 3            # LLM runs at once; one run per pass and scope
timeout_s = 1200           # a run is killed after this many seconds
# false: the CLIs run on your claude.ai / ChatGPT login; API-key and cloud-provider
# variables are stripped from their environment
allow_api_keys = false

# Each run is `command` + the optional groups below. A group is used only if the
# installed CLI's --help lists its flags; `no_tools_args` is always used.
# Placeholders: {{thinking}} (mapped level), {{schema}} (JSON text), {{schema_file}} (path).
# The prompt always goes in on stdin; cwd is .rewrite/sandbox/.

[runner.claude]
# Verified 2026-10-18 against claude 2.1.302 (`claude --help` and real runs):
# --tools "" leaves only the StructuredOutput tool that --json-schema adds;
# --strict-mcp-config drops MCP servers. --bare is not used: it skips OAuth login.
# Re-verified 2026-10-18 on claude 2.1.303 (local /context and the request sent to a
# local capture server, no model call): --setting-sources project loads every CLAUDE.md
# and .claude/CLAUDE.md from the sandbox up to / -- the paper root's too -- so the run
# would see paper-level notes. --setting-sources "" loads no source: no project, local
# or user (~/.claude/CLAUDE.md) memory; the sandbox CLAUDE.md is dropped too, which is
# fine: the prompt itself starts with system.md. Settings files are read from the cwd
# only. A managed (system-wide) CLAUDE.md, if your machine has one, cannot be turned off.
command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--tools", "", \
"--no-session-persistence"]
model_flag = "--model"
thinking_args = ["--effort", "{{thinking}}"]
thinking = {{ low = "low", medium = "medium", high = "high", xhigh = "xhigh", max = "max" }}
schema_args = ["--json-schema", "{{schema}}"]
no_tools_args = []
isolation_args = ["--setting-sources", "", "--strict-mcp-config"]
suppress_global = true     # the global CLAUDE.md is not loaded with isolation_args
stream_args = ["--include-partial-messages"]
models = ["opus", "sonnet", "haiku"]

[runner.codex]
# Verified 2026-10-18 against codex-cli 0.162.1 (`codex exec --help` and real runs):
# --sandbox read-only still allows read-only shell commands; the --disable switches
# remove the shell tools. --ignore-user-config skips ~/.codex/config.toml (its model and
# effort too); --ignore-rules skips execpolicy .rules files. Neither, nor any other
# flag, stops codex from injecting $CODEX_HOME/AGENTS.md (checked with
# `codex debug prompt-input`), so suppress_global is false.
# Re-verified 2026-10-18 on codex-cli 0.163.0 (`codex debug prompt-input` and the request
# sent to a local capture server, no model call): from the sandbox codex also reads
# AGENTS.md in every directory up to the project root (the nearest ancestor with .git:
# the paper repo) and the repo skills in .agents/skills and .codex/skills.
# -c project_doc_max_bytes=0 turns project docs off (the sandbox AGENTS.md too; the
# prompt starts with system.md anyway) and -c project_root_markers=[] makes the sandbox
# the project root, so nothing above it is read. A config without these keys gets them
# when loaded; set a key to another value to opt out (GET /api/runners then lists what
# a run can see).
command = ["codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", \
"--ephemeral", "-c", "project_doc_max_bytes=0", "-c", "project_root_markers=[]"]
model_flag = "-m"
thinking_args = ["-c", "model_reasoning_effort={{thinking}}"]
thinking = {{ low = "low", medium = "medium", high = "high", xhigh = "xhigh", max = "max" }}
schema_args = ["--output-schema", "{{schema_file}}"]
no_tools_args = ["--disable", "shell_tool", "--disable", "unified_exec"]
isolation_args = ["--ignore-user-config", "--ignore-rules"]
suppress_global = false
stream_args = []
# From `codex debug models` (visibility "list"). Empty model = codex's own default.
models = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", \
"gpt-5.6-terra", "gpt-5.6-luna"]

# Model tiering by pass level: strong model and high thinking for coarse passes, a cheap
# fast model for sentence passes. A pass's front matter and each run can override it.
# The model applies only when the run uses this level's runner; an empty model means
# the CLI's default.
[models]
message = {{ runner = "claude", model = "opus", thinking = "high" }}
claim = {{ runner = "claude", model = "opus", thinking = "high" }}
section = {{ runner = "claude", model = "opus", thinking = "high" }}
paragraph = {{ runner = "claude", model = "sonnet", thinking = "medium" }}
sentence = {{ runner = "claude", model = "sonnet", thinking = "low" }}
"""


def default_config_text(main: str = "main.tex") -> str:
    """The commented config.toml that `rewrite init` writes."""
    # json-style escaping is valid for a TOML basic string.
    escaped = main.replace("\\", "\\\\").replace('"', '\\"')
    return DEFAULT_CONFIG_TOML.format(main=escaped)


class ConfigError(ValueError):
    """config.toml exists but is not valid TOML or does not match the model."""


def parse_config(text: str) -> Config:
    try:
        return Config.model_validate(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"invalid .rewrite/config.toml: {exc}") from exc


def load_config(path: Path) -> Config:
    """Load config.toml; a missing file yields the defaults."""
    if not path.is_file():
        return Config()
    return parse_config(path.read_text(encoding="utf-8"))


def save_config(path: Path, config: Config) -> None:
    """Write config.toml with tomli-w. Comments in an existing file are not preserved."""
    from app.files import atomic_write_text  # local import: files imports nothing from here

    # exclude_none: TOML has no null; an unset optional key is simply left out.
    atomic_write_text(path, tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)))
