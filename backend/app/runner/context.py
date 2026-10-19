"""Instruction files outside the sandbox that an LLM CLI could pick up (audit 2026-10-18
item 7).

Runs use `.rewrite/sandbox/` as cwd, but both CLIs also look *up* the directory tree
(verified 2026-10-18 on claude 2.1.303 and codex-cli 0.163.0, offline: claude's local
`/context`, `codex debug prompt-input`, and the requests both sent to a local capture
server):

- claude, when `--setting-sources` includes `project` (or is absent): every `CLAUDE.md`
  and `.claude/CLAUDE.md` from the cwd up to `/`, and the paper repo's `.claude/agents`
  and `.claude/skills`; with `local`, `CLAUDE.local.md` the same way. Settings files
  (`.claude/settings*.json`) are read from the cwd only, never from an ancestor.
- codex: `AGENTS.md` (and `AGENTS.override.md`) in every directory from the project root
  (the nearest ancestor holding a root marker, `.git` by default: the paper repo) down to
  the cwd, and repo skills in `.agents/skills` and `.codex/skills`.
  `-c project_doc_max_bytes=0` stops the docs; `-c project_root_markers=[]` makes the cwd
  the project root, which stops both.

`context_leaks` lists what exists outside the sandbox, from its parent directory up to
`/`, and whether the argv a run uses keeps each entry out (`suppressed`). Entries are
judged from the flags alone, so for codex an entry above the git root may be reported as
not suppressed although codex would not reach it. The CLI's own config directory
(`~/.claude`, `~/.codex`, or CLAUDE_CONFIG_DIR / CODEX_HOME) is left out: user-level files
are reported as `global_instructions` by GET /api/runners. Nothing here writes anything.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from app.config import cli_config_overrides

ALL_CLAUDE_SOURCES = frozenset({"user", "project", "local"})

# (path relative to each ancestor directory, what makes the CLI read it)
ClaudeNeed = Literal["project", "local", "cwd-only"]
CLAUDE_CANDIDATES: tuple[tuple[str, ClaudeNeed], ...] = (
    ("CLAUDE.md", "project"),
    (".claude/CLAUDE.md", "project"),
    ("CLAUDE.local.md", "local"),
    (".claude/agents", "project"),
    (".claude/skills", "project"),
)
CodexNeed = Literal["docs", "root"]
CODEX_CANDIDATES: tuple[tuple[str, CodexNeed], ...] = (
    ("AGENTS.md", "docs"),
    ("AGENTS.override.md", "docs"),
    (".agents/skills", "root"),
    (".codex", "root"),
)


class ContextLeak(BaseModel):
    """GET /api/runners `context_leaks` entry."""

    runner: str
    path: str  # absolute
    suppressed: bool  # the argv in use keeps it out of a run


# --------------------------------------------------------------------------- argv


def claude_sources(argv: Sequence[str]) -> frozenset[str]:
    """The setting sources `--setting-sources` selects (the last one given); every source
    when the flag is absent."""
    value: str | None = None
    for index, token in enumerate(argv):
        if token == "--setting-sources" and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith("--setting-sources="):
            value = token.removeprefix("--setting-sources=")
    if value is None:
        return ALL_CLAUDE_SOURCES
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _toml_value(text: str) -> Any:
    """A `-c` value as codex reads it: TOML, else the raw string."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def codex_boundary(argv: Sequence[str]) -> tuple[bool, bool]:
    """(project docs off, cwd is the project root) for a codex argv."""
    overrides = cli_config_overrides(list(argv))
    docs_off = "project_doc_max_bytes" in overrides and (
        _toml_value(overrides["project_doc_max_bytes"]) == 0
    )
    root_here = "project_root_markers" in overrides and (
        _toml_value(overrides["project_root_markers"]) == []
    )
    return docs_off, root_here


# --------------------------------------------------------------------------- the probe


def _config_home(runner: str) -> Path:
    if runner == "claude":
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else Path.home() / ".claude"
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _exists(path: Path) -> bool:
    try:
        return os.path.lexists(path)
    except OSError:
        return False


def _inside(path: Path, directory: Path) -> bool:
    try:
        return path == directory or path.is_relative_to(directory)
    except ValueError:
        return False


def context_leaks(sandbox: Path, runner: str, argv: Sequence[str]) -> list[ContextLeak]:
    """Instruction files and folders outside `sandbox` (its parent up to `/`) that `runner`
    could discover, nearest first, each marked `suppressed` when `argv` keeps it out."""
    start = sandbox.resolve()
    try:
        home = _config_home(runner).resolve()
    except (OSError, RuntimeError):
        home = None
    if runner == "claude":
        sources = claude_sources(argv)

        def suppressed(need: str) -> bool:
            return need == "cwd-only" or need not in sources

        candidates: Sequence[tuple[str, str]] = CLAUDE_CANDIDATES
    elif runner == "codex":
        docs_off, root_here = codex_boundary(argv)

        def suppressed(need: str) -> bool:
            return root_here or (need == "docs" and docs_off)

        candidates = CODEX_CANDIDATES
    else:
        return []
    out: list[ContextLeak] = []
    for directory in start.parents:
        found: list[tuple[Path, str]] = [
            (directory / rel, need) for rel, need in candidates if _exists(directory / rel)
        ]
        if runner == "claude":  # `.claude/settings*.json`: never read from an ancestor
            try:
                settings = sorted((directory / ".claude").glob("settings*.json"))
            except OSError:
                settings = []
            found += [(path, "cwd-only") for path in settings]
        for path, need in found:
            if home is not None and _inside(path, home):
                continue  # user-level: GET /api/runners reports it as global_instructions
            out.append(ContextLeak(runner=runner, path=str(path), suppressed=suppressed(need)))
    return out


def context_warning(leaks: Sequence[ContextLeak]) -> str | None:
    """The warning stored on a run whose argv lets instruction files from outside the
    sandbox in; None when every entry is suppressed."""
    exposed = [leak for leak in leaks if not leak.suppressed]
    if not exposed:
        return None
    runner = exposed[0].runner
    paths = ", ".join(leak.path for leak in exposed)
    return (
        f"{runner} may read instruction files from outside .rewrite/sandbox/ that are not "
        f"part of the stored prompt, so this run's context is not guaranteed clean: {paths}. "
        f"Check [runner.{runner}] in .rewrite/config.toml."
    )
