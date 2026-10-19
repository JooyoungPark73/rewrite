"""LLM runners (PLAN.md "Backend architecture and interfaces", phase 4).

`ClaudeRunner` (`claude -p`) and `CodexRunner` (`codex exec`) share `CliRunner`: cwd
`.rewrite/sandbox/`, tools disabled, prompt on stdin, own process group. Their flags come
from `[runner.claude]` / `[runner.codex]` in config.toml, filtered by what the installed
CLI's `--help` lists. `paste` turns pasted JSON into a result. Runners never write the spec
file (see tests/test_invariants.py).
"""

from __future__ import annotations

from pathlib import Path

from app.config import RunnerCliConfig
from app.runner.base import (
    CliProbe,
    CliRunner,
    ProcessHandle,
    RunEvent,
    Runner,
    RunResult,
    RunUsage,
    Thinking,
    probe_cli,
)
from app.runner.claude import ClaudeRunner
from app.runner.codex import CodexRunner

RUNNERS: dict[str, type[CliRunner]] = {"claude": ClaudeRunner, "codex": CodexRunner}


def make_runner(
    name: str,
    config: RunnerCliConfig,
    probe: CliProbe,
    sandbox: Path,
    timeout_s: float,
    *,
    allow_api_keys: bool = False,
) -> CliRunner:
    return RUNNERS[name](config, probe, sandbox, timeout_s, allow_api_keys=allow_api_keys)


__all__ = [
    "RUNNERS",
    "ClaudeRunner",
    "CliProbe",
    "CliRunner",
    "CodexRunner",
    "ProcessHandle",
    "RunEvent",
    "RunResult",
    "RunUsage",
    "Runner",
    "Thinking",
    "make_runner",
    "probe_cli",
]
