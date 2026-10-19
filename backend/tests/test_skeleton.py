"""LaTeX skeleton scanner, markers and marker insertion (app/skeleton.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skeleton import (
    AmbiguousHeadingError,
    DocScan,
    SourceChangedError,
    find_marker,
    find_section,
    marker_hash,
    marker_text,
    place_marker,
    scan_document,
    section_marker,
    split_lines,
)
from tests.conftest import EXAMPLE

# --------------------------------------------------------------------------- sample paper


def test_sample_paper_skeleton() -> None:
    scan = scan_document(EXAMPLE, "main.tex")
    assert scan.files == [
        "main.tex",
        "sections/intro.tex",
        "sections/method.tex",
        "sections/results.tex",
        "sections/conclusion.tex",
    ]
    assert [(h.file, h.line, h.kind, h.heading, h.label) for h in scan.headings] == [
        ("sections/intro.tex", 1, "section", "Introduction", "sec:intro"),
        ("sections/method.tex", 1, "section", "Design", "sec:method"),
        ("sections/results.tex", 1, "section", "Evaluation", "sec:results"),
        ("sections/conclusion.tex", 1, "section", "Conclusion", "sec:conclusion"),
    ]
    # Read off the files: the abstract body (main.tex 22-28), four intro paragraphs, four
    # in the design section (the display equation stays inside 9-16; the figure is not a
    # paragraph), three in the evaluation (the table is not) and one in the conclusion.
    assert [(p.file, p.line_start, p.line_end, p.section_heading) for p in scan.paragraphs] == [
        ("main.tex", 22, 28, None),
        ("sections/intro.tex", 4, 9, "Introduction"),
        ("sections/intro.tex", 11, 14, "Introduction"),
        ("sections/intro.tex", 16, 20, "Introduction"),
        ("sections/intro.tex", 22, 25, "Introduction"),
        ("sections/method.tex", 4, 7, "Design"),
        ("sections/method.tex", 9, 16, "Design"),
        ("sections/method.tex", 18, 21, "Design"),
        ("sections/method.tex", 23, 23, "Design"),
        ("sections/results.tex", 4, 7, "Evaluation"),
        ("sections/results.tex", 9, 12, "Evaluation"),
        ("sections/results.tex", 29, 31, "Evaluation"),
        ("sections/conclusion.tex", 4, 7, "Conclusion"),
    ]
    assert all(p.marker_id is None for p in scan.paragraphs) and scan.markers == []
    first = scan.paragraphs[1]
    assert first.preview == first.text[:80] and len(first.preview) == 80
    assert first.preview.startswith("Function-as-a-Service (FaaS) platforms bill tenants")
    assert scan.paragraphs[8].text == "Figure~\\ref{fig:pipeline} summarizes the restore path."
    assert scan.labels["fig:pipeline"] == ("sections/method.tex", 37)
    # document order: every line has a position, main's abstract before the intro heading
    assert scan.ordinal("main.tex", 22) < scan.ordinal("sections/intro.tex", 1)  # type: ignore[operator]
    assert scan.ordinal("sections/intro.tex", 25) < scan.ordinal("sections/method.tex", 1)  # type: ignore[operator]


def test_missing_main_gives_an_empty_scan(tmp_path: Path) -> None:
    scan = scan_document(tmp_path, "main.tex")
    assert scan.files == [] and scan.paragraphs == [] and scan.headings == []
    assert scan_document(tmp_path, "../escape.tex").files == []


# --------------------------------------------------------------------------- edge rules

MAIN = """\
\\documentclass{article}
\\newcommand{\\foo}{bar}
Preamble text that is not a paragraph.
\\input{macros}
\\begin{document}
\\section*[Short]{A \\textbf{bold}
  title}
\\label{sec:a}
First paragraph line one,
% a comment inside the paragraph
line two.

%@p p-7
Marked paragraph.
% @p p-8
Same run, new marker.

\\begin{figure}
  Text in a figure.\\label{fig:x}
\\end{figure}

\\begin{verbatim}
\\section{Fake}

text
\\end{verbatim}

\\begin{comment}
Commented out.
\\end{comment}

\\subsection{Sub}\\label{sec:sub}
\\begin{itemize}
  \\item An item with words.
\\end{itemize}

\\begin{equation}
  x = 1
\\end{equation}

% @p p-9

After a blank line.
\\input{part}
\\section{A \\textbf{bold} title}
Dup heading text.
\\end{document}
Trailing text.
"""


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    (tmp_path / "main.tex").write_text(MAIN)
    (tmp_path / "macros.tex").write_text("\\newcommand{\\baz}{qux}\nMacro file text.\n")
    (tmp_path / "part.tex").write_text("Included paragraph.\n")
    return tmp_path


def test_edge_rules(doc: Path) -> None:
    scan = scan_document(doc, "main.tex")
    assert scan.files == ["main.tex", "macros.tex", "part.tex"]
    assert [(h.line, h.kind, h.heading, h.label) for h in scan.headings] == [
        (6, "section", "A \\textbf{bold} title", "sec:a"),  # starred, short title, 2 lines
        (32, "subsection", "Sub", "sec:sub"),
        (45, "section", "A \\textbf{bold} title", None),
    ]
    got = [
        (p.file, p.line_start, p.line_end, p.text, p.marker_id, p.marker_line, p.section_heading)
        for p in scan.paragraphs
    ]
    bold = "A \\textbf{bold} title"
    assert got == [
        ("main.tex", 9, 11, "First paragraph line one, line two.", None, None, bold),
        ("main.tex", 14, 14, "Marked paragraph.", "p-7", 13, bold),
        ("main.tex", 16, 16, "Same run, new marker.", "p-8", 15, bold),
        ("main.tex", 34, 34, "\\item An item with words.", None, None, "Sub"),
        ("main.tex", 43, 43, "After a blank line.", None, None, "Sub"),  # p-9 is cut off
        ("part.tex", 1, 1, "Included paragraph.", None, None, "Sub"),
        ("main.tex", 46, 46, "Dup heading text.", None, None, bold),
    ]
    assert [(m.id, m.line) for m in scan.markers] == [("p-7", 13), ("p-8", 15), ("p-9", 41)]
    assert scan.labels == {
        "sec:a": ("main.tex", 8),
        "fig:x": ("main.tex", 19),
        "sec:sub": ("main.tex", 32),
    }
    assert scan.paragraph_at("main.tex", 10) is scan.paragraphs[0]
    assert scan.paragraph_at("main.tex", 13) is scan.paragraphs[1]  # its marker line
    assert scan.paragraph_at("main.tex", 12) is None
    assert scan.paragraph_at("main.tex", 19) is None  # inside the figure


def test_marker_regex_tolerance(tmp_path: Path) -> None:
    lines = [
        "% @p a-1",
        "%@p a-2",
        "  %  @p   a-3  \t",
        "%% @p a-4",
        "% @p a 5",
        "% @pa-6",
        "x % @p a-7",
    ]
    (tmp_path / "main.tex").write_text("\n\n".join(f"{line}\nText." for line in lines) + "\n")
    scan = scan_document(tmp_path, "main.tex")
    assert [m.id for m in scan.markers] == ["a-1", "a-2", "a-3", "a-4"]


def test_marker_text_and_hash(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(
        "% @p p-1\nOne   two % comment\n% only a comment\nthree.\n\nNext.\n"
        "% @p p-2\nShort.\n% @p p-3\nBody\n\\section{X}\n% @p p-4\n"
    )
    scan = scan_document(tmp_path, "main.tex")
    texts = {m.id: marker_text(scan, m) for m in scan.markers}
    assert texts == {"p-1": "One two three.", "p-2": "Short.", "p-3": "Body", "p-4": ""}
    before = marker_hash(scan, scan.markers[0])
    (tmp_path / "main.tex").write_text(
        "% @p p-1\nOne two % other comment\n  three.\n\nNext changed.\n"
    )
    rescanned = scan_document(tmp_path, "main.tex")
    assert marker_hash(rescanned, rescanned.markers[0]) == before  # whitespace/comments only
    (tmp_path / "main.tex").write_text("% @p p-1\nOne two four.\n")
    rescanned = scan_document(tmp_path, "main.tex")
    assert marker_hash(rescanned, rescanned.markers[0]) != before


def test_find_marker_prefers_the_anchor_file(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("% @p p-1\nA.\n\\input{b}\n")
    (tmp_path / "b.tex").write_text("% @p p-1\nB.\n")
    scan = scan_document(tmp_path, "main.tex")
    assert find_marker(scan, "p-1").file == "main.tex"  # type: ignore[union-attr]
    assert find_marker(scan, "p-1", "b.tex").file == "b.tex"  # type: ignore[union-attr]
    assert find_marker(scan, "p-2") is None


def test_section_resolution(doc: Path) -> None:
    scan = scan_document(doc, "main.tex")
    first, sub, dup = scan.headings
    assert section_marker(scan, first) == "\\label{sec:a}"  # text not unique → label
    # Same text twice in one file and no label of its own: no marker names it (audit
    # 2026-10-18 item 10); the text marker would resolve to `first`.
    with pytest.raises(AmbiguousHeadingError, match=r"main\.tex:45 is not unique.*\\label"):
        section_marker(scan, dup)
    assert section_marker(scan, sub) == "\\subsection{Sub}"
    assert find_section(scan, "\\section{A  \\textbf{bold}\n title}") is None  # 2 headings
    assert find_section(scan, "\\section{A \\textbf{bold} title}", "main.tex") is None
    assert find_section(scan, "\\label{sec:a}") == ("main.tex", 6)
    assert find_section(scan, "\\section*{Sub}") is None  # kind must match
    assert find_section(scan, "\\subsection{Sub}") == ("main.tex", 32)
    assert find_section(scan, "\\label{sec:sub}") == ("main.tex", 32)
    assert find_section(scan, "\\label{fig:x}") == ("main.tex", 19)  # any label, fallback
    assert find_section(scan, "\\label{nope}") is None
    assert find_section(scan, "Intro") is None


def test_section_resolution_prefers_anchor_file(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("\\section{Results}\nA.\n\\input{b}\n")
    (tmp_path / "b.tex").write_text("\\section{Results}\nB.\n")
    scan = scan_document(tmp_path, "main.tex")
    # Duplicates in different files: the anchor's file picks one; without it (or with a
    # file holding neither) the marker is ambiguous, not "the first one".
    assert find_section(scan, "\\section{Results}") is None
    assert find_section(scan, "\\section{Results}", "main.tex") == ("main.tex", 1)
    assert find_section(scan, "\\section{Results}", "b.tex") == ("b.tex", 1)
    assert find_section(scan, "\\section{Results}", "c.tex") is None
    assert [section_marker(scan, h) for h in scan.headings] == ["\\section{Results}"] * 2
    (tmp_path / "b.tex").write_text("\\section{Moved}\nB.\n")
    scan = scan_document(tmp_path, "main.tex")
    assert find_section(scan, "\\section{Moved}", "old.tex") == ("b.tex", 1)  # unique anywhere


def test_no_document_environment_means_all_body(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("\\section{Only}\nText here.\n")
    scan = scan_document(tmp_path, "main.tex")
    assert len(scan.headings) == 1 and len(scan.paragraphs) == 1


def test_split_lines_matches_the_editor() -> None:
    assert split_lines("a\u2028b\x0cc\r\nd\re\n") == ["a\u2028b\x0cc", "d", "e"]
    assert split_lines("") == [] and split_lines("x") == ["x"] and split_lines("\n") == [""]


# --------------------------------------------------------------------------- place_marker


def rescan(root: Path) -> DocScan:
    return scan_document(root, "main.tex")


def test_place_marker_inserts_exactly_once_and_is_idempotent(doc: Path) -> None:
    scan = rescan(doc)
    edit = place_marker(doc, scan, scan.paragraphs[0], "p-10")
    assert (edit.file, edit.line) == ("main.tex", 9)
    assert [rel for rel, _ in edit.written] == ["main.tex"]
    text = (doc / "main.tex").read_text()
    assert text.count("% @p p-10\n") == 1
    assert text.splitlines()[8:10] == ["% @p p-10", "First paragraph line one,"]
    assert text.replace("% @p p-10\n", "", 1) == MAIN  # nothing else changed

    scan = rescan(doc)
    para = scan.paragraph_at("main.tex", 10)
    assert para is not None and para.marker_id == "p-10" and para.line_start == 10
    again = place_marker(doc, scan, para, "p-10")
    assert again.written == [] and (again.file, again.line) == ("main.tex", 9)
    assert (doc / "main.tex").read_text() == text


def test_place_marker_moves_an_existing_marker(doc: Path) -> None:
    scan = rescan(doc)
    target = scan.paragraph_at("main.tex", 43)
    assert target is not None
    edit = place_marker(doc, scan, target, "p-7")  # p-7 currently sits on line 13
    assert edit.line == 42  # one line above was removed
    lines = (doc / "main.tex").read_text().splitlines()
    assert [i + 1 for i, line in enumerate(lines) if line.strip().endswith("p-7")] == [42]
    assert lines[41:43] == ["% @p p-7", "After a blank line."]
    scan = rescan(doc)
    assert [m.id for m in scan.markers].count("p-7") == 1


def test_place_marker_across_files(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("% @p p-1\nA.\n\\input{b}\n")
    (tmp_path / "b.tex").write_text("B.\n")
    scan = rescan(tmp_path)
    edit = place_marker(tmp_path, scan, scan.paragraphs[1], "p-1")
    assert sorted(rel for rel, _ in edit.written) == ["b.tex", "main.tex"]
    assert (tmp_path / "main.tex").read_text() == "A.\n\\input{b}\n"
    assert (tmp_path / "b.tex").read_text() == "% @p p-1\nB.\n"


def test_place_marker_keeps_crlf_and_indentation(tmp_path: Path) -> None:
    raw = "\\begin{itemize}\r\n  \\item Words here.\r\n\\end{itemize}"  # no final newline
    (tmp_path / "main.tex").write_bytes(raw.encode())
    scan = rescan(tmp_path)
    place_marker(tmp_path, scan, scan.paragraphs[0], "p-1")
    assert (tmp_path / "main.tex").read_bytes() == (
        b"\\begin{itemize}\r\n  % @p p-1\r\n  \\item Words here.\r\n\\end{itemize}"
    )
    (tmp_path / "main.tex").write_bytes(b"Last line without newline")
    scan = rescan(tmp_path)
    place_marker(tmp_path, scan, scan.paragraphs[0], "p-2")
    assert (tmp_path / "main.tex").read_bytes() == b"% @p p-2\nLast line without newline"


def test_place_marker_refuses_a_changed_file(doc: Path) -> None:
    scan = rescan(doc)
    (doc / "main.tex").write_text(MAIN.replace("line two.", "line 2."))
    with pytest.raises(SourceChangedError):
        place_marker(doc, scan, scan.paragraphs[0], "p-10")
    assert "p-10" not in (doc / "main.tex").read_text()


def test_section_segments_follow_document_order(tmp_path: Path) -> None:
    """Audit 2026-10-18 item 8 (the scan half): extents cross include boundaries, stop at
    the next heading of the same or a higher level wherever it is, leave out the include
    line that leads into the next section and trailing back matter, and also start from a
    label line that is not on a heading."""
    from app.skeleton import section_segments

    (tmp_path / "main.tex").write_text(
        "\\begin{document}\n"  # 1
        "\\section{A}\n"  # 2
        "Text in A, then \\label{sec:a-late}.\n"  # 3
        "\\input{a-body}\n"  # 4
        "\n"  # 5
        "Back in main.\n"  # 6
        "\\input{b}\n"  # 7
        "\\bibliographystyle{plain}\n"  # 8
        "\\bibliography{refs}\n"  # 9
        "\\end{document}\n"  # 10
    )
    (tmp_path / "a-body.tex").write_text("\\subsection{A1}\nBody of A1.\n\n")
    (tmp_path / "b.tex").write_text("\\section{B}\nBody of B.\n% @p p-9\n")
    scan = scan_document(tmp_path, "main.tex")
    assert section_segments(scan, "main.tex", 2) == [
        ("main.tex", 2, 4),
        ("a-body.tex", 1, 2),
        ("main.tex", 6, 6),
    ]
    assert section_segments(scan, "a-body.tex", 1) == [("a-body.tex", 1, 2), ("main.tex", 6, 6)]
    assert section_segments(scan, "b.tex", 1) == [("b.tex", 1, 3)]  # comments stay
    assert find_section(scan, "\\label{sec:a-late}") == ("main.tex", 3)
    assert section_segments(scan, "main.tex", 3)[0] == ("main.tex", 3, 4)
    assert section_segments(scan, "nope.tex", 1) == []
