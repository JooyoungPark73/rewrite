"""NativeLatexmk on the sample paper (skipped when latexmk is missing)."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from app.compiler import LogError
from app.compiler.native import NativeLatexmk
from tests.conftest import requires_latexmk


@requires_latexmk
def test_sample_compiles_to_pdf_and_synctex(initialized_repo: Path) -> None:
    result = NativeLatexmk().compile(initialized_repo, "main.tex")
    assert result.ok, result.errors
    assert result.exit_code == 0
    assert result.pdf == ".rewrite/build/main.pdf"
    assert (initialized_repo / result.pdf).read_bytes()[:5] == b"%PDF-"
    assert result.synctex == ".rewrite/build/main.synctex.gz"
    assert (initialized_repo / result.synctex).stat().st_size > 0
    # the example paper compiles cleanly: no errors and no warnings
    assert [e for e in result.errors if e.severity != "info"] == []
    assert "Output written on .rewrite/build/main.pdf" in result.log
    assert result.duration_ms > 0


@requires_latexmk
def test_undefined_macro_reports_file_and_line(initialized_repo: Path) -> None:
    method = initialized_repo / "sections" / "method.tex"
    lines = method.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("Figure~\\ref"))
    lines.insert(index, "\\undefinedmacro")
    method.write_text("\n".join(lines) + "\n")

    result = NativeLatexmk().compile(initialized_repo, "main.tex")

    assert not result.ok
    assert result.exit_code not in (0, None)
    assert (
        LogError(
            file="sections/method.tex",
            line=index + 1,
            message="Undefined control sequence: \\undefinedmacro",
            severity="error",
        )
        in result.errors
    )


def test_missing_latexmk_is_reported(initialized_repo: Path) -> None:
    result = NativeLatexmk(latexmk="definitely-not-latexmk").compile(initialized_repo, "main.tex")
    assert not result.ok and result.pdf is None
    assert result.errors[0].message == "latexmk was not found on PATH"


def test_missing_main_is_reported(initialized_repo: Path) -> None:
    result = NativeLatexmk().compile(initialized_repo, "nope.tex")
    assert not result.ok
    assert result.errors[0].message == "main file not found: nope.tex"


def test_timeout_kills_the_process_group(initialized_repo: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "slow-latexmk"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    result = NativeLatexmk(timeout_s=1.0, latexmk=str(script)).compile(initialized_repo, "main.tex")

    assert not result.ok and result.exit_code is None
    assert any("timed out" in e.message for e in result.errors)
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild process survived the timeout")
