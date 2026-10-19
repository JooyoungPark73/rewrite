"""NativeLatexmk: runs the host's latexmk with the repo as working directory."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from app.compiler import CompileResult, LogError
from app.compiler.logparse import parse_log
from app.repo import REWRITE_DIR

BUILD_DIR = f"{REWRITE_DIR}/build"
DEFAULT_TIMEOUT_S = 300.0

# TeX hard-wraps log lines at max_print_line (79) characters, which splits file paths and
# messages. These kpathsea variables can be overridden from the environment.
TEX_LOG_ENV = {"max_print_line": "10000", "error_line": "254", "half_error_line": "238"}


def latexmk_command(main: str) -> list[str]:
    return [
        "latexmk",
        "-pdf",
        "-synctex=1",
        "-interaction=nonstopmode",
        f"-output-directory={BUILD_DIR}",
        main,
    ]


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def run_process(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    on_timeout: Callable[[], None] | None = None,
) -> tuple[str, int | None]:
    """Run `cmd` in its own process group; return (console output, exit code).

    The exit code is None when the command timed out. Then `on_timeout` runs first (the
    Docker compiler stops its container there) and the whole process group is killed.
    """
    proc = subprocess.Popen(
        list(cmd),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,  # own process group, so a timeout kills pdflatex too
    )
    try:
        console, _ = proc.communicate(timeout=timeout_s)
        return console or "", proc.returncode
    except subprocess.TimeoutExpired:
        if on_timeout is not None:
            on_timeout()
        _kill_group(proc)
        console, _ = proc.communicate()
        return console or "", None


def timeout_error(main: str, what: str, timeout_s: float) -> LogError:
    return LogError(
        file=main,
        line=None,
        severity="error",
        message=f"{what} timed out after {timeout_s:.0f} s and was stopped",
    )


def failure_result(
    main: str, started: float, message: str, log: str | None = None
) -> CompileResult:
    """A CompileResult for a compile that could not run at all (nothing in build/ is read)."""
    return CompileResult(
        ok=False,
        pdf=None,
        log=log or message,
        errors=[LogError(file=main, line=None, message=message, severity="error")],
        duration_ms=int((time.monotonic() - started) * 1000),
        exit_code=None,
    )


def collect_result(
    repo: Path,
    main: str,
    *,
    exit_code: int | None,
    console: str,
    started: float,
    extra_errors: list[LogError] | None = None,
    extra_roots: Sequence[str] = (),
) -> CompileResult:
    """Build a CompileResult from what latexmk left in .rewrite/build/.

    Shared by the native and Docker compilers. `extra_roots` are other spellings of the repo
    root that may appear in the log (a configured Docker `container_root`).
    """
    stem = Path(main).stem
    build = repo / BUILD_DIR
    log_path = build / f"{stem}.log"
    pdf_path = build / f"{stem}.pdf"
    synctex_path = build / f"{stem}.synctex.gz"

    log = ""
    errors: list[LogError] = list(extra_errors or [])
    if log_path.is_file():
        log = log_path.read_text(encoding="utf-8", errors="replace")
        errors.extend(parse_log(log, main, repo, extra_roots=extra_roots))
    if exit_code not in (0, None) and not any(e.severity == "error" for e in errors):
        tail = [ln for ln in console.strip().splitlines() if ln.strip()][-3:]
        detail = " / ".join(tail) if tail else "no output"
        errors.append(
            LogError(
                file=main,
                line=None,
                message=f"latexmk exited with {exit_code}: {detail}",
                severity="error",
            )
        )

    pdf = f"{BUILD_DIR}/{stem}.pdf" if pdf_path.is_file() else None
    synctex = f"{BUILD_DIR}/{stem}.synctex.gz" if synctex_path.is_file() else None
    ok = exit_code == 0 and pdf is not None and not any(e.severity == "error" for e in errors)
    return CompileResult(
        ok=ok,
        pdf=pdf,
        log=log or console,
        errors=errors,
        duration_ms=int((time.monotonic() - started) * 1000),
        synctex=synctex,
        exit_code=exit_code,
    )


class NativeLatexmk:
    """`latexmk -pdf -synctex=1 -interaction=nonstopmode -output-directory=.rewrite/build`."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S, latexmk: str = "latexmk") -> None:
        self.timeout_s = timeout_s
        self.latexmk = latexmk

    def compile(self, repo: Path, main: str) -> CompileResult:
        started = time.monotonic()
        repo = repo.resolve()
        if shutil.which(self.latexmk) is None:
            return failure_result(main, started, "latexmk was not found on PATH")
        if not (repo / main).is_file():
            return failure_result(main, started, f"main file not found: {main}")
        (repo / BUILD_DIR).mkdir(parents=True, exist_ok=True)

        cmd = latexmk_command(main)
        cmd[0] = self.latexmk
        console, exit_code = run_process(
            cmd, cwd=repo, env={**os.environ, **TEX_LOG_ENV}, timeout_s=self.timeout_s
        )
        extra = [] if exit_code is not None else [timeout_error(main, "latexmk", self.timeout_s)]
        return collect_result(
            repo, main, exit_code=exit_code, console=console, started=started, extra_errors=extra
        )
