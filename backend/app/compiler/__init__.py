"""Compiler interface (PLAN.md "Backend architecture and interfaces").

Two implementations: `NativeLatexmk` (phase 1) and `DockerLatexmk` (phase 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from app.config import CompilerConfig

Severity = Literal["error", "warning", "info"]


class LogError(BaseModel):
    """One parsed log entry. `file` is repo-relative (falls back to the main file)."""

    file: str
    line: int | None
    message: str
    severity: Severity


class CompileResult(BaseModel):
    ok: bool
    pdf: str | None  # repo-relative path of the PDF, if one exists after the run
    log: str  # the TeX log (falls back to latexmk's console output when there is none)
    errors: list[LogError]
    duration_ms: int
    synctex: str | None = None  # repo-relative path of the .synctex.gz, if present
    exit_code: int | None = None  # latexmk's exit status; null if it could not run


class Compiler(Protocol):
    def compile(self, repo: Path, main: str) -> CompileResult: ...


def get_compiler(config: CompilerConfig) -> Compiler:
    from app.compiler.docker import DockerLatexmk
    from app.compiler.native import NativeLatexmk

    if config.kind == "docker":
        return DockerLatexmk(texlive_year=config.texlive_year, container_root=config.container_root)
    return NativeLatexmk()


__all__ = ["CompileResult", "Compiler", "LogError", "Severity", "get_compiler"]
