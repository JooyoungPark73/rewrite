"""The LaTeX skeleton of the paper: headings, paragraphs and `% @p` markers in document order.

Pure source scanning over the include tree (main file first, each `\\input` expanded where
it appears). Nothing here knows the spec model; app/anchors.py joins the two. Line numbers
are 1-based and count `\\n`, `\\r\\n` and `\\r` breaks, like the editor.

Rules (phase-3 brief):

- Heading: `\\section{...}` or `\\subsection{...}` (starred, or with a `[short]` title) at the
  start of a line outside excluded environments. Its text is the braces content with
  whitespace collapsed. A `\\label{...}` on the heading line, or on the lines right after it
  before any text or blank line, is the heading's label.
- Paragraph: a run of non-blank lines in the document body (after `\\begin{document}`, before
  `\\end{document}`) with at least one text line. Not text: headings, pure comments, lines
  inside figure/table/equation/align/verbatim (and similar) environments, `\\begin`/`\\end`-only
  lines, and lines that are only commands with arguments (`\\label{..}`, `\\maketitle`).
  Those lines do not split a run (a display equation stays in its paragraph); a blank line,
  a heading, a marker line or an `\\input` line does. A paragraph spans its first to its last
  text line.
- Marker: a `% @p <id>` line (`%@p <id>` and surrounding spaces tolerated). It belongs to the
  paragraph that starts after it with no blank line in between.
- Marker text, which the stale hash covers: the lines after the marker up to the next blank
  line, next marker, next `\\section`/`\\subsection` or `\\end{document}`, with comments
  stripped and whitespace collapsed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.files import (
    FileConflictError,
    PathEscapeError,
    content_sha256,
    include_targets,
    normalize_rel,
    read_text_file,
    resolve_in_repo,
    strip_include_commands,
    strip_tex_comment,
    to_repo_relative,
    write_text_file,
)

HeadingKind = Literal["section", "subsection"]

PREVIEW_CHARS = 80
MAX_INCLUDE_DEPTH = 32
MAX_HEADING_LINES = 6  # a heading's braces may span this many lines

MARKER_RE = re.compile(r"^[ \t]*%+[ \t]*@p[ \t]+(?P<id>\S+)[ \t]*$")
_HEADING_RE = re.compile(r"^\s*\\(?P<kind>section|subsection)\*?\s*(?:\[[^\]]*\]\s*)?\{")
_SECTIONING_RE = re.compile(r"^\s*\\(?:sub)?section\*?\s*[\[{]")
_BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")
_END_DOC_RE = re.compile(r"\\end\s*\{document\}")
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]+)\}")
_ENV_RE = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")
_INCLUDE_HINT_RE = re.compile(r"\\(?:input|include|subfile)(?![A-Za-z@])")
_COMMAND_ONLY_RE = re.compile(r"^\s*(?:\\[A-Za-z@]+\*?(?:\s*\[[^\]]*\])*(?:\s*\{[^{}]*\})*\s*)+$")
_COMMAND_RE = re.compile(r"\\[A-Za-z@]+")
_WORD_RE = re.compile(r"\w")
_LINE_KEEPENDS_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")

_EXCLUDED_BASE = (
    "figure",
    "table",
    "equation",
    "align",
    "verbatim",
    "gather",
    "multline",
    "eqnarray",
    "displaymath",
    "flalign",
    "alignat",
    "lstlisting",
    "minted",
    "algorithm",
    "wrapfigure",
    "comment",
)
EXCLUDED_ENVS = frozenset(_EXCLUDED_BASE) | frozenset(name + "*" for name in _EXCLUDED_BASE)
# Inside these only their own \end counts; the body is not LaTeX.
VERBATIM_ENVS = frozenset({"verbatim", "verbatim*", "lstlisting", "minted", "comment"})


# --------------------------------------------------------------------------- text helpers


def split_lines(text: str) -> list[str]:
    """Lines without their endings; a final newline does not start an extra line."""
    return [part.rstrip("\r\n") for part in _LINE_KEEPENDS_RE.findall(text)]


def split_lines_keepends(text: str) -> list[str]:
    return _LINE_KEEPENDS_RE.findall(text)


def collapse(text: str) -> str:
    return " ".join(text.split())


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def marker_line(marker_id: str) -> str:
    return f"% @p {marker_id}"


def paragraph_marker(marker_id: str) -> str:
    """The anchor `marker` string stored in the spec for a paragraph."""
    return f"@p {marker_id}"


def _braces_content(text: str, open_pos: int) -> tuple[str, int] | None:
    """Content of the balanced group opening at text[open_pos] == '{' and the index after its
    closing brace; None when it does not close in `text`."""
    depth = 0
    i = open_pos
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_pos + 1 : i], i + 1
        i += 1
    return None


# --------------------------------------------------------------------------- scan result


@dataclass
class Heading:
    file: str
    line: int
    kind: HeadingKind
    heading: str  # braces content, whitespace collapsed
    label: str | None
    ordinal: int  # document-order position of the heading line


@dataclass
class Paragraph:
    file: str
    line_start: int
    line_end: int
    text: str  # text lines, comments stripped, whitespace collapsed
    section_heading: str | None  # nearest preceding heading in document order
    marker_id: str | None  # the `% @p` marker that belongs to this paragraph
    marker_line: int | None
    ordinal: int  # document-order position of line_start

    @property
    def preview(self) -> str:
        return self.text[:PREVIEW_CHARS]


@dataclass(frozen=True)
class Marker:
    id: str
    file: str
    line: int
    ordinal: int


@dataclass
class DocScan:
    main: str
    files: list[str] = field(default_factory=list)  # visited .tex files, document order
    lines: dict[str, list[str]] = field(default_factory=dict)
    headings: list[Heading] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)  # every marker line, document order
    labels: dict[str, tuple[str, int]] = field(default_factory=dict)  # first occurrence
    ordinals: dict[tuple[str, int], int] = field(default_factory=dict)
    order: list[tuple[str, int]] = field(default_factory=list)  # (file, line) by ordinal - 1
    end_ordinal: int | None = None  # the `\\end{document}` line, when there is one

    def ordinal(self, file: str, line: int) -> int | None:
        return self.ordinals.get((file, line))

    def paragraph_at(self, file: str, line: int) -> Paragraph | None:
        """The paragraph whose line range contains `line` (or whose marker sits on it)."""
        for para in self.paragraphs:
            if para.file != file:
                continue
            if para.line_start <= line <= para.line_end or para.marker_line == line:
                return para
        return None

    def heading_at(self, file: str, line: int) -> Heading | None:
        for heading in self.headings:
            if heading.file == file and heading.line == line:
                return heading
        return None


# --------------------------------------------------------------------------- scanner


@dataclass
class _OpenParagraph:
    file: str
    line_start: int
    line_end: int
    parts: list[str]
    section_heading: str | None
    marker_id: str | None
    marker_line: int | None
    ordinal: int


class _Scanner:
    def __init__(self, root: Path, main: str, body: bool) -> None:
        self.root = root
        self.scan = DocScan(main=main)
        self.seen: set[str] = set()
        self.counter = 0
        self.body = body  # past \begin{document}
        self.done = False  # past \end{document}
        self.stack: list[str] = []  # open excluded environments
        self.para: _OpenParagraph | None = None
        self.pending: tuple[str, int] | None = None  # (marker id, line) awaiting a paragraph
        self.awaiting_label: Heading | None = None
        self.current_heading: str | None = None

    def _ord(self, rel: str, line: int) -> int:
        key = (rel, line)
        if key not in self.scan.ordinals:
            self.counter += 1
            self.scan.ordinals[key] = self.counter
            self.scan.order.append(key)
        return self.scan.ordinals[key]

    def _flush(self) -> None:
        para, self.para = self.para, None
        if para is None:
            return
        self.scan.paragraphs.append(
            Paragraph(
                file=para.file,
                line_start=para.line_start,
                line_end=para.line_end,
                text=collapse(" ".join(para.parts)),
                section_heading=para.section_heading,
                marker_id=para.marker_id,
                marker_line=para.marker_line,
                ordinal=para.ordinal,
            )
        )

    def _boundary(self) -> None:
        self._flush()
        self.pending = None
        self.awaiting_label = None

    def _track_envs(self, active: str) -> bool:
        touched = False
        for m in _ENV_RE.finditer(active):
            kind, name = m.group(1), m.group(2).strip()
            if name not in EXCLUDED_ENVS:
                continue
            touched = True
            if kind == "begin":
                self.stack.append(name)
            elif name in self.stack:
                del self.stack[len(self.stack) - 1 - self.stack[::-1].index(name)]
        return touched

    def _includes(self, rel: str, active: str, depth: int) -> bool:
        if not _INCLUDE_HINT_RE.search(active):
            return False
        for target in include_targets(self.root, rel, active):
            if target not in self.seen and depth < MAX_INCLUDE_DEPTH:
                self.visit(target, depth + 1)
        return True

    def visit(self, rel: str, depth: int) -> None:
        self.seen.add(rel)
        self.scan.files.append(rel)
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        lines = split_lines(text)
        self.scan.lines[rel] = lines
        i = 0
        while i < len(lines):
            lineno = i + 1
            raw = lines[i]
            i += 1
            ordinal = self._ord(rel, lineno)

            if self.stack and self.stack[-1] in VERBATIM_ENVS:
                top = self.stack[-1]
                if re.search(r"\\end\s*\{" + re.escape(top) + r"\}", raw):
                    self.stack.pop()
                continue

            marker = MARKER_RE.match(raw)
            if marker:
                self.scan.markers.append(Marker(marker.group("id"), rel, lineno, ordinal))
                if self.body and not self.done:
                    self._boundary()
                    self.pending = (marker.group("id"), lineno)
                continue

            if not raw.strip():
                if self.body and not self.done:
                    self._boundary()
                continue
            active = strip_tex_comment(raw)
            if not active.strip():
                continue  # a comment line does not end a paragraph
            if self.done:
                continue

            if not self.body:
                if _BEGIN_DOC_RE.search(active):
                    self.body = True
                self._includes(rel, active, depth)
                continue

            if _END_DOC_RE.search(active):
                self._boundary()
                self.done = True
                self.scan.end_ordinal = ordinal
                continue

            for label in _LABEL_RE.finditer(active):
                self.scan.labels.setdefault(label.group(1).strip(), (rel, lineno))

            inside = bool(self.stack)
            if not inside:
                heading = _HEADING_RE.match(active)
                if heading:
                    i = self._heading(rel, lines, i, lineno, ordinal, active, heading)
                    continue
                if _INCLUDE_HINT_RE.search(active):
                    self._boundary()
                    self._includes(rel, active, depth)
                    continue
            elif self._includes(rel, active, depth):
                continue

            touched = self._track_envs(active)
            if inside or touched or self.stack:
                continue
            if _COMMAND_ONLY_RE.match(active) or not _WORD_RE.search(_COMMAND_RE.sub("", active)):
                if self.awaiting_label is not None and self.awaiting_label.label is None:
                    found = _LABEL_RE.search(active)
                    if found:
                        self.awaiting_label.label = found.group(1).strip()
                continue

            # a text line
            self.awaiting_label = None
            if self.para is None:
                marker_id, marker_at = self.pending if self.pending else (None, None)
                self.pending = None
                self.para = _OpenParagraph(
                    file=rel,
                    line_start=lineno,
                    line_end=lineno,
                    parts=[],
                    section_heading=self.current_heading,
                    marker_id=marker_id,
                    marker_line=marker_at,
                    ordinal=ordinal,
                )
            self.para.line_end = lineno
            self.para.parts.append(active.strip())
        self._boundary()

    def _heading(
        self,
        rel: str,
        lines: list[str],
        i: int,
        lineno: int,
        ordinal: int,
        active: str,
        match: re.Match[str],
    ) -> int:
        """Record a heading starting on `lineno`; returns the index of the next unread line."""
        self._boundary()
        text = active
        consumed = 0
        parsed = _braces_content(text, match.end() - 1)
        while parsed is None and consumed < MAX_HEADING_LINES - 1 and i + consumed < len(lines):
            text += " " + strip_tex_comment(lines[i + consumed])
            consumed += 1
            self._ord(rel, lineno + consumed)
            parsed = _braces_content(text, match.end() - 1)
        if parsed is None:  # unbalanced: keep what there is
            content, rest = text[match.end() :], ""
        else:
            content, end = parsed
            rest = text[end:]
        label = _LABEL_RE.search(rest)
        kind: HeadingKind = "section" if match.group("kind") == "section" else "subsection"
        heading = Heading(
            file=rel,
            line=lineno,
            kind=kind,
            heading=collapse(content),
            label=label.group(1).strip() if label else None,
            ordinal=ordinal,
        )
        self.scan.headings.append(heading)
        self.current_heading = heading.heading
        self.awaiting_label = heading if heading.label is None else None
        return i + consumed


def scan_document(root: Path, main: str) -> DocScan:
    """Scan the include tree from `main`. A missing main file gives an empty scan."""
    main_rel = normalize_rel(main)
    try:
        main_path = resolve_in_repo(root, main_rel)
    except PathEscapeError:
        return DocScan(main=main_rel)
    if not main_path.is_file():
        return DocScan(main=main_rel)
    main_rel = to_repo_relative(root, main_path)
    try:
        head = main_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return DocScan(main=main_rel)
    has_body_marker = any(
        _BEGIN_DOC_RE.search(strip_tex_comment(line)) for line in split_lines(head)
    )
    scanner = _Scanner(root.resolve(), main_rel, body=not has_body_marker)
    scanner.visit(main_rel, 0)
    return scanner.scan


# --------------------------------------------------------------------------- markers


def find_marker(scan: DocScan, marker_id: str, prefer_file: str | None = None) -> Marker | None:
    """The marker line for `marker_id`: first in `prefer_file`, else first in document order."""
    found = [m for m in scan.markers if m.id == marker_id]
    if prefer_file is not None:
        for m in found:
            if m.file == prefer_file:
                return m
    return found[0] if found else None


def marker_text(scan: DocScan, marker: Marker) -> str:
    """The paragraph text a marker covers (see the module docstring)."""
    out: list[str] = []
    for raw in scan.lines.get(marker.file, [])[marker.line :]:
        if not raw.strip() or MARKER_RE.match(raw):
            break
        active = strip_tex_comment(raw)
        if _SECTIONING_RE.match(active) or _END_DOC_RE.search(active):
            break
        out.append(active)
    return collapse(" ".join(out))


def marker_hash(scan: DocScan, marker: Marker) -> str:
    return text_hash(marker_text(scan, marker))


def marker_paragraph(lines: list[str], marker_id: str) -> tuple[int, int, int] | None:
    """(marker line, first line, last line), 1-based, of the paragraph under the first
    `% @p <marker_id>` line in `lines`, by the marker-text rule above: the lines after the
    marker up to the next blank line, marker, `\\section`/`\\subsection` or
    `\\end{document}`. Works on any version of a file (e.g. `git show <ref>:<path>`).
    None when the marker is absent or nothing follows it."""
    for index, raw in enumerate(lines):
        found = MARKER_RE.match(raw)
        if found is None or found.group("id") != marker_id:
            continue
        marker_at = index + 1
        last = marker_at
        for number in range(marker_at + 1, len(lines) + 1):
            line = lines[number - 1]
            if not line.strip() or MARKER_RE.match(line):
                break
            active = strip_tex_comment(line)
            if _SECTIONING_RE.match(active) or _END_DOC_RE.search(active):
                break
            last = number
        return (marker_at, marker_at + 1, last) if last > marker_at else None
    return None


# --------------------------------------------------------------------------- section anchors

SECTION_MARKER_RE = re.compile(r"^\\(?P<kind>section|subsection)\*?\{(?P<text>.*)\}$", re.DOTALL)
LABEL_MARKER_RE = re.compile(r"^\\label\{(?P<label>[^{}]+)\}$")


class AmbiguousHeadingError(ValueError):
    """A heading that no anchor marker can name uniquely: its text is repeated in its own
    file and it has no label of its own (→ 409 on adoption)."""


def section_marker(scan: DocScan, heading: Heading) -> str:
    """The anchor marker for `heading`, chosen so that `find_section` (with the anchor's
    file) finds this heading and no other. PLAN: never anchor by position.

    - `\\section{<heading>}` when no other heading of that kind has the same text;
    - else `\\label{...}` when the heading has a label no other heading carries (the
      phase-3 label fallback);
    - else `\\section{<heading>}` when every duplicate is in another file (the anchor's
      `file` then picks this one);
    - else AmbiguousHeadingError: the human adds a `\\label{...}` in the editor first.
    """
    same = [h for h in scan.headings if h.kind == heading.kind and h.heading == heading.heading]
    text_marker = f"\\{heading.kind}{{{heading.heading}}}"
    if len(same) == 1:
        return text_marker
    if heading.label and sum(1 for h in scan.headings if h.label == heading.label) == 1:
        return f"\\label{{{heading.label}}}"
    if sum(1 for h in same if h.file == heading.file) == 1:
        return text_marker
    others = ", ".join(f"{h.file}:{h.line}" for h in same if h is not heading)
    raise AmbiguousHeadingError(
        f"{text_marker} at {heading.file}:{heading.line} is not unique (also at {others}), "
        "so its text cannot anchor it. Add a label right after this heading in the editor, "
        f"e.g. \\label{{sec:{_label_hint(heading.heading)}-{heading.line}}}, save, and adopt "
        "it again."
    )


def _label_hint(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", _COMMAND_RE.sub(" ", text).lower())
    return "-".join(words)[:24].strip("-") or "section"


def _pick(hits: list[Heading], prefer_file: str | None) -> Heading | None:
    """The one heading a marker names: the only match in the anchor's file, else the only
    match in the document. None when that is not a single heading: an ambiguous marker
    is unresolved (orphaned), never resolved to the first match."""
    if prefer_file is not None:
        in_file = [h for h in hits if h.file == prefer_file]
        if in_file:
            return in_file[0] if len(in_file) == 1 else None
    return hits[0] if len(hits) == 1 else None


def find_section(
    scan: DocScan, marker: str, prefer_file: str | None = None
) -> tuple[str, int] | None:
    """(file, line) a section anchor marker resolves to, or None.

    `\\section{X}` / `\\subsection{X}` match a heading of that kind whose collapsed text is
    exactly X (collapsed). `\\label{x}` matches the heading labelled x, else any `\\label{x}`
    in the document body. Several matches resolve only when exactly one of them is in
    `prefer_file` (the anchor's file); otherwise the marker is ambiguous and gives None.
    """
    marker = marker.strip()
    m = SECTION_MARKER_RE.match(marker)
    if m:
        text = collapse(m.group("text"))
        hits = [h for h in scan.headings if h.kind == m.group("kind") and h.heading == text]
    else:
        lm = LABEL_MARKER_RE.match(marker)
        if lm is None:
            return None
        label = lm.group("label").strip()
        hits = [h for h in scan.headings if h.label == label]
        if not hits:
            return scan.labels.get(label)
    best = _pick(hits, prefer_file)
    return None if best is None else (best.file, best.line)


# --------------------------------------------------------------------------- section extent

# Back matter that may sit between a section's last text and the next section (or the end
# of the document) without belonging to it.
_TAIL_COMMANDS_RE = re.compile(
    r"\\(?:bibliographystyle|printbibliography|appendix|clearpage|cleardoublepage|newpage)"
    r"(?![A-Za-z@])\*?\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\})?"
)


def _inert_tail(raw: str) -> bool:
    """A line that may be dropped from the end of a section: blank, or only include and
    back-matter commands (the `\\input` that leads into the next section, the
    bibliography). Comment lines stay: `% @p` markers and the author's notes are content."""
    if not raw.strip():
        return True
    active = strip_tex_comment(raw)
    if not active.strip():
        return False
    return not _TAIL_COMMANDS_RE.sub("", strip_include_commands(active)).strip()


def section_segments(scan: DocScan, file: str, line: int) -> list[tuple[str, int, int]]:
    """The section whose heading is at file:line as (file, first line, last line) segments
    in document order (audit 2026-10-18 item 8).

    The section runs from its heading to the next heading of the same or a higher level
    (a `\\section` for either kind; a `\\subsection` only ends a subsection) or
    `\\end{document}`, in document order: through `\\input`/`\\include` files where they
    are pulled in, so a body kept in an included file belongs to the heading before it,
    and a heading inside an included file ends the section there. Each segment is a run
    of consecutive lines of one file, numbered by that file's own lines. Blank lines at a
    segment's edges and include/back-matter lines at the very end are left out.

    A line that is not a heading (a section anchored by a `\\label` placed below its
    heading) starts a section-level extent there. Empty when the scan never reached
    file:line."""
    heading = scan.heading_at(file, line)
    start = heading.ordinal if heading is not None else scan.ordinal(file, line)
    if start is None:
        return []
    kinds = {"section", "subsection"} if heading and heading.kind == "subsection" else {"section"}
    end = len(scan.order) + 1  # first ordinal after the section
    for other in scan.headings:
        if other.ordinal > start and other.kind in kinds:
            end = min(end, other.ordinal)
            break
    if scan.end_ordinal is not None and scan.end_ordinal > start:
        end = min(end, scan.end_ordinal)
    keys = scan.order[start - 1 : end - 1]

    def text(key: tuple[str, int]) -> str:
        lines = scan.lines.get(key[0], [])
        return lines[key[1] - 1] if 0 < key[1] <= len(lines) else ""

    while len(keys) > 1 and _inert_tail(text(keys[-1])):  # never past the heading
        keys.pop()
    runs: list[list[tuple[str, int]]] = []
    for key in keys:
        if runs and runs[-1][-1][0] == key[0] and runs[-1][-1][1] + 1 == key[1]:
            runs[-1].append(key)
        else:
            runs.append([key])
    segments: list[tuple[str, int, int]] = []
    for run in runs:
        while run and not text(run[0]).strip():
            run.pop(0)
        while run and not text(run[-1]).strip():
            run.pop()
        if run:
            segments.append((run[0][0], run[0][1], run[-1][1]))
    return segments


# --------------------------------------------------------------------------- marker insertion


class SourceChangedError(Exception):
    """The .tex file changed on disk between the scan and the edit (→ 409). `written` lists
    the files already written before the conflict was found (for their file.changed)."""

    def __init__(self, detail: str, written: list[tuple[str, float]] | None = None) -> None:
        super().__init__(detail)
        self.written = written or []


@dataclass
class MarkerEdit:
    file: str
    line: int  # 1-based line of the marker after the edit
    written: list[tuple[str, float]]  # (repo-relative path, new mtime) per file written


def place_marker(root: Path, scan: DocScan, paragraph: Paragraph, marker_id: str) -> MarkerEdit:
    """Put `% @p <marker_id>` directly above `paragraph`. Idempotent.

    When the paragraph already carries that marker nothing is written. Otherwise the marker
    is inserted above `line_start` (indented like that line) and every other line carrying
    the same marker id is removed, so a marker id appears exactly once in the source.
    Writes go through files.write_text_file (atomic, path-checked).

    Every file must still be the text `scan` saw, and each write is conditional on the file
    being exactly the revision read here, checked right before its atomic rename (audit
    2026-10-18 item 5): a save that lands in between makes this raise SourceChangedError
    (→ 409) instead of writing the old prose back. The API also holds the repo's source
    lock, which PUT /api/file takes too. Files that only lose a stale marker are written
    first and the paragraph's file last, so a late conflict leaves at most a missing
    marker (an orphaned node), never a duplicate.
    """
    if paragraph.marker_id == marker_id and paragraph.marker_line is not None:
        return MarkerEdit(paragraph.file, paragraph.marker_line, [])

    removals: dict[str, set[int]] = {}
    for m in scan.markers:
        if m.id == marker_id:
            removals.setdefault(m.file, set()).add(m.line)
    targets = [paragraph.file, *sorted(f for f in removals if f != paragraph.file)]

    edits: list[tuple[str, str, str]] = []  # (file, new text, revision it is based on)
    new_line = paragraph.line_start
    for rel in targets:
        text, _ = read_text_file(root, rel)
        parts = split_lines_keepends(text)
        if [p.rstrip("\r\n") for p in parts] != scan.lines.get(rel):
            raise SourceChangedError(f"{rel} changed on disk; reload and try again")
        drop = removals.get(rel, set())
        out: list[str] = []
        for number, part in enumerate(parts, start=1):
            if rel == paragraph.file and number == paragraph.line_start:
                indent = part[: len(part) - len(part.lstrip(" \t"))]
                ending = part[len(part.rstrip("\r\n")) :] or _first_newline(text)
                out.append(indent + marker_line(marker_id) + ending)
            if number not in drop:
                out.append(part)
        if rel == paragraph.file:
            new_line = paragraph.line_start - sum(1 for d in drop if d < paragraph.line_start)
        edits.append((rel, "".join(out), content_sha256(text)))

    written: list[tuple[str, float]] = []
    for rel, content, base in [*edits[1:], edits[0]]:  # the paragraph's file last
        try:
            written.append((rel, write_text_file(root, rel, content, base_sha256=base)))
        except FileConflictError as exc:
            raise SourceChangedError(
                f"{rel} changed on disk while the marker was being inserted, so it was not "
                "written; reload and try again",
                written,
            ) from exc
    return MarkerEdit(paragraph.file, new_line, written)


def _first_newline(text: str) -> str:
    m = re.search(r"\r\n|\r|\n", text)
    return m.group(0) if m else "\n"


# --------------------------------------------------------------------------- labels

# `\label{...}` targets the spec can refer to by name (fig:pipeline rather than "Figure 1",
# which renumbers). Kind comes from the innermost environment the label sits in, or from a
# heading for section labels; the caption is the `\caption{...}` of that environment.

_CAPTION_RE = re.compile(r"\\caption\s*(?:\[[^\]]*\])?\s*\{")
_FIGURE_ENVS = {"figure", "figure*", "wrapfigure", "subfigure", "sidewaysfigure"}
_TABLE_ENVS = {"table", "table*", "sidewaystable", "longtable", "tabular"}
_EQUATION_ENVS = {
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "flalign",
    "flalign*",
    "alignat",
    "alignat*",
}
_LISTING_ENVS = {"lstlisting", "algorithm", "algorithmic", "algorithm2e", "listing", "minted"}


@dataclass
class LabelInfo:
    label: str
    kind: str  # figure | table | equation | listing | section | other
    file: str
    line: int  # 1-based line of the \label
    caption: str | None  # the environment's caption, whitespace collapsed, or None


def _env_kind(env: str) -> str | None:
    if env in _FIGURE_ENVS:
        return "figure"
    if env in _TABLE_ENVS:
        return "table"
    if env in _EQUATION_ENVS:
        return "equation"
    if env in _LISTING_ENVS:
        return "listing"
    return None


def scan_labels(scan: DocScan) -> list[LabelInfo]:
    """Every `\\label{...}` of the scanned document, in document order (first occurrence of a
    duplicated label wins, as in `DocScan.labels`)."""
    heading_labels = {(h.file, h.label) for h in scan.headings if h.label}
    heading_names = {h.label for h in scan.headings if h.label}
    out: list[LabelInfo] = []
    seen: set[str] = set()
    for rel in scan.files:
        lines = scan.lines.get(rel, [])
        # Open environments, innermost last: (name, labels found so far, caption so far).
        stack: list[tuple[str, list[LabelInfo], str | None]] = []
        for lineno, raw in enumerate(lines, start=1):
            active = strip_tex_comment(raw)
            # Events of this line in source order, so `\begin{table}\caption{T}\label{t}\end{table}`
            # on one line is read left to right.
            events: list[tuple[int, str, re.Match[str]]] = []
            events += [(m.start(), "env", m) for m in _ENV_RE.finditer(active)]
            events += [(m.start(), "caption", m) for m in _CAPTION_RE.finditer(active)]
            events += [(m.start(), "label", m) for m in _LABEL_RE.finditer(active)]
            events.sort(key=lambda e: e[0])
            for _, what, m in events:
                if what == "env":
                    if m.group(1) == "begin":
                        stack.append((m.group(2).strip(), [], None))
                    elif stack:
                        _, found, caption = stack.pop()
                        for info in found:
                            if info.caption is None:
                                info.caption = caption
                elif what == "caption":
                    if not stack:
                        continue
                    tail = active[m.end() - 1 :] + "\n" + "\n".join(lines[lineno:])
                    content = _braces_content(tail, 0)
                    caption = collapse(content[0]) if content else None
                    name, found, _ = stack[-1]
                    stack[-1] = (name, found, caption)
                    for info in found:
                        if info.caption is None:
                            info.caption = caption
                else:
                    label = m.group(1).strip()
                    if label in seen:
                        continue
                    seen.add(label)
                    kind = "other"
                    for name, _, _ in reversed(stack):
                        env_kind = _env_kind(name)
                        if env_kind:
                            kind = env_kind
                            break
                    if kind == "other" and (
                        label in heading_names or (rel, label) in heading_labels
                    ):
                        kind = "section"
                    info = LabelInfo(label=label, kind=kind, file=rel, line=lineno, caption=None)
                    if stack:
                        name, found, caption = stack[-1]
                        found.append(info)
                        info.caption = caption
                    out.append(info)
        # Unclosed environments at the end of a file: keep whatever caption was seen.
    return out
