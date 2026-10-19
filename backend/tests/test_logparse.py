"""TeX log parser against real pdfTeX logs (tests/fixtures/logs)."""

from __future__ import annotations

import random

import pytest

from app.compiler import LogError
from app.compiler.logparse import parse_log, unwrap
from tests.conftest import FIXTURES

LOGS = FIXTURES / "logs"
LONG = "sections/a-rather-long-directory-name-to-force-wrapping/and-a-long-file-name-too.tex"


def read(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8", errors="replace")


def by_severity(entries: list[LogError], severity: str) -> list[tuple[str, int | None, str]]:
    return [(e.file, e.line, e.message) for e in entries if e.severity == severity]


@pytest.mark.parametrize("name", ["errors-unwrapped.log", "errors-wrapped.log"])
def test_errors_and_warnings(name: str) -> None:
    entries = parse_log(read(name), "main.tex")
    assert by_severity(entries, "error") == [
        ("sections/intro.tex", 5, "Undefined control sequence: \\undefinedmacro"),
        (LONG, 2, "Undefined control sequence: \\alsoundefined"),
    ]
    warnings = by_severity(entries, "warning")
    assert ("main.tex", 7, "Reference `sec:nowhere' on page 1 undefined") in warnings
    assert ("main.tex", None, "There were undefined references.") in warnings
    info = by_severity(entries, "info")
    assert (
        "sections/intro.tex",
        3,
        "Overfull \\hbox (164.59943pt too wide) detected at line 3",
    ) in info
    # LaTeX Font Warning (multi-line, "(Font)" continuation) is info, with its input line
    assert (
        "main.tex",
        4,
        "Font shape `OT1/cmr/bx/sc' undefined using `OT1/cmr/bx/n' instead",
    ) in info


def test_missing_input_file_and_emergency_stop() -> None:
    entries = parse_log(read("missing-input.log"), "main.tex")
    assert by_severity(entries, "error") == [
        ("sections/method.tex", 23, "Undefined control sequence: \\undefinedmacro"),
        ("main.tex", 35, "LaTeX Error: File `sections/missing.tex' not found."),
    ]
    natbib = [e for e in entries if e.message.startswith("Package natbib:")]
    assert [(e.file, e.line) for e in natbib][:3] == [
        ("sections/intro.tex", 5),
        ("sections/intro.tex", 9),
        ("sections/intro.tex", 18),
    ]


def test_absolute_repo_paths_map_to_relative() -> None:
    log = (
        "(/home/u/paper/main.tex (/usr/share/texlive/article.cls)\n"
        "(/home/u/paper/sections/x.tex\n"
        "! Undefined control sequence.\n"
        "l.12 \\foo\n"
        "          \n"
        "\n"
        "))\n"
    )
    entries = parse_log(log, "main.tex", "/home/u/paper")
    assert [(e.file, e.line, e.message) for e in entries] == [
        ("sections/x.tex", 12, "Undefined control sequence: \\foo")
    ]


def test_extra_roots_map_container_paths() -> None:
    log = (
        "(/workdir/./main.tex (/workdir/./sections/x.tex\n"
        "! Undefined control sequence.\nl.3 \\foo\n\n))\n"
    )
    entries = parse_log(log, "main.tex", "/home/u/paper", extra_roots=["/workdir/"])
    assert [(e.file, e.line) for e in entries] == [("sections/x.tex", 3)]


def test_error_inside_package_has_no_line() -> None:
    log = (
        "(./main.tex (/usr/share/texlive/foo.sty\n"
        "! Package foo Error: broken.\n"
        "\n"
        "l.99 \\PackageError{foo}{broken}{}\n"
        "\n"
        "))\n"
    )
    [entry] = parse_log(log, "main.tex")
    assert (entry.file, entry.line, entry.severity) == ("main.tex", None, "error")


def test_file_line_error_format() -> None:
    log = "(./main.tex\n./sections/m.tex:23: Undefined control sequence.\nl.23 \\bad\n\n)\n"
    [entry] = parse_log(log, "main.tex")
    assert (entry.file, entry.line, entry.message) == (
        "sections/m.tex",
        23,
        "Undefined control sequence: \\bad",
    )


def test_multiline_package_warning() -> None:
    log = (
        "(./main.tex\n"
        "Package hyperref Warning: Token not allowed in a PDF string (Unicode):\n"
        "(hyperref)                removing `\\textsc' on input line 10.\n"
        "\n)\n"
    )
    [entry] = parse_log(log, "main.tex")
    assert entry.line == 10 and entry.severity == "warning"
    assert entry.message == (
        "Package hyperref: Token not allowed in a PDF string (Unicode): removing `\\textsc'"
    )


def test_unbalanced_parens_in_box_content_do_not_break_stack() -> None:
    log = (
        "(./main.tex (./a.tex\n"
        "Overfull \\hbox (1.0pt too wide) in paragraph at lines 3--4\n"
        "[]\\OT1/cmr/m/n/10 text (with an open paren\n"
        "\n"
        ") (./b.tex\n"
        "! Undefined control sequence.\n"
        "l.7 \\oops\n"
        "\n"
        "))\n"
    )
    entries = parse_log(log, "main.tex")
    assert [(e.file, e.line, e.severity) for e in entries] == [
        ("a.tex", 3, "info"),
        ("b.tex", 7, "error"),
    ]


def test_unwrap_joins_79_column_lines() -> None:
    first = "x" * 79
    assert unwrap(["banner", first, "tail", "short"]) == ["banner", first + "tail", "short"]
    # a body line longer than 79 means the log is not wrapped (the banner line is exempt)
    unwrapped = ["banner", "y" * 120, first, "tail"]
    assert unwrap(unwrapped) == unwrapped


def test_never_raises_on_garbage() -> None:
    rng = random.Random(1234)
    alphabet = "()[]{}<>!l.0123456789 \n\\/abcLaTeX Warning:Overfull\"'"
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
        result = parse_log(text, "main.tex")
        assert isinstance(result, list)
    assert parse_log("", "main.tex") == []
