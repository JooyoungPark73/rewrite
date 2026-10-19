"""TeX log parser → [LogError(file, line, message, severity)].

Recognises:
- `! <message>` errors, with the line number taken from the following `l.<n>` context line,
- `file:line: message` errors (the `-file-line-error` format), in case a user's latexmkrc
  enables it,
- `LaTeX Warning:`, `Package <x> Warning:`, `Class <x> Warning:` (multi-line, with
  `on input line <n>`), `LaTeX Font Warning:` (severity info), `pdfTeX warning`,
- `Overfull` / `Underfull` boxes (severity info, line from `at lines <a>--<b>`).

The current file is tracked with the usual parenthesis heuristic: TeX prints `(<path>` when
it opens a file and `)` when it closes it. Every `(` is pushed (a file path or a
placeholder) so unrelated parentheses in messages stay balanced; error help text, error
context lines and box contents are skipped because they can contain unbalanced ones.
Files outside the repo (TeX Live packages) are not reported; an error raised while one of
them is the current file is attributed to the innermost repo file without a line number.
When no file is known the main file is used.

The parser never raises; on unexpected input it returns what it has collected.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from pathlib import Path

from app.compiler import LogError, Severity

WRAP_WIDTH = 79  # TeX Live's default max_print_line

_LINE_REF_RE = re.compile(r"^l\.(\d+)(?:\s(.*))?$")
_FILE_LINE_ERROR_RE = re.compile(
    r"^(?P<file>(?:\.{1,2}/|/)?[^\s:()]+\.[A-Za-z0-9]+):(?P<line>\d+): (?P<msg>.+)$"
)
_WARNING_RE = re.compile(
    r"^(?:(?P<font>LaTeX Font)|(?P<latex>LaTeX)|Package (?P<pkg>[\w@.:-]+)"
    r"|Class (?P<cls>[\w@.:-]+)) Warning: (?P<msg>.*)$"
)
_PDFTEX_WARNING_RE = re.compile(r"pdfTeX warning(?: \((?P<what>[^)]*)\))?: (?P<msg>.*)$")
_BOX_RE = re.compile(r"^(?:Overfull|Underfull) \\[hv]box\b")
_BOX_LINES_RE = re.compile(r"at lines? (\d+)")
_INPUT_LINE_RE = re.compile(r"\s*on input line (\d+)\.?")
_CONTROL_SEQ_RE = re.compile(r"\\(?:[A-Za-z@]+|.)")
_PATH_START_RE = re.compile(r"^(?:\.{1,2}/|/|~/|[A-Za-z]:[\\/])")
_BARE_FILE_RE = re.compile(r"^[\w+-][\w./+-]*\.[A-Za-z][A-Za-z0-9]{0,7}$")
_TOKEN_STOP = set(' \t()[]{}<>"')


def unwrap(lines: list[str], width: int = WRAP_WIDTH) -> list[str]:
    """Rejoin lines TeX hard-wrapped at `max_print_line` characters.

    Only applied when no line exceeds the width, i.e. when the log really is wrapped. The
    banner (first line) and the memory statistics at the end are never wrapped by TeX, so
    they are ignored for that check.
    """
    body = lines[1:]
    for idx, line in enumerate(body):
        if line.startswith("Here is how much of TeX's memory you used"):
            body = body[:idx]
            break
    if any(len(line) > width for line in body):
        return lines
    out: list[str] = []
    buf = ""
    for line in lines:
        buf += line
        if len(line) == width:
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _read_token(line: str, start: int) -> tuple[str, int]:
    """Read a file-name candidate starting at `start`; return (token, end index)."""
    if start < len(line) and line[start] == '"':
        end = line.find('"', start + 1)
        if end == -1:
            return line[start + 1 :], len(line)
        return line[start + 1 : end], end + 1
    end = start
    while end < len(line) and line[end] not in _TOKEN_STOP:
        end += 1
    return line[start:end], end


def _looks_like_file(token: str) -> bool:
    if not token:
        return False
    return bool(_PATH_START_RE.match(token) or _BARE_FILE_RE.match(token))


class _Paths:
    """Classifies log paths as repo files (→ repo-relative) or outside files."""

    def __init__(self, repo_roots: Iterable[str]) -> None:
        self.roots = [r.rstrip("/") + "/" for r in repo_roots if r]

    def repo_relative(self, token: str) -> str | None:
        if token.startswith("/"):
            for root in self.roots:
                if token.startswith(root):
                    return posixpath.normpath(token[len(root) :])
            return None
        if _PATH_START_RE.match(token) and not token.startswith(("./", "../")):
            return None  # ~/ or a Windows drive path
        rel = posixpath.normpath(token)
        if rel.startswith("../") or rel == "..":
            return None
        return rel


class _Parser:
    def __init__(self, main: str, repo_roots: Iterable[str]) -> None:
        self.main = posixpath.normpath(main)
        self.paths = _Paths(repo_roots)
        self.stack: list[str | None] = []
        self.out: list[LogError] = []
        self.pending: list[LogError] = []  # errors waiting for their `l.<n>` line
        self.want_cs: LogError | None = None  # "Undefined control sequence" awaiting its name

    # -- file stack -----------------------------------------------------------------------

    def location(self) -> tuple[str, bool]:
        """(repo file to attribute to, whether line numbers refer to that file)."""
        files = [f for f in self.stack if f is not None]
        if not files:
            return self.main, True
        top_is_repo = True
        for token in reversed(files):
            rel = self.paths.repo_relative(token)
            if rel is not None:
                return rel, top_is_repo
            top_is_repo = False
        return self.main, False

    def track_parens(self, line: str) -> None:
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "(":
                token, end = _read_token(line, i + 1)
                if _looks_like_file(token):
                    self.stack.append(token)
                    i = end
                    continue
                self.stack.append(None)
            elif ch == ")":
                if self.stack:
                    self.stack.pop()
            i += 1

    # -- entries --------------------------------------------------------------------------

    def add(self, message: str, severity: Severity, line: int | None) -> LogError:
        file, line_ok = self.location()
        entry = LogError(
            file=file, line=line if line_ok else None, message=message, severity=severity
        )
        self.out.append(entry)
        return entry

    def add_error(self, message: str) -> None:
        if message.startswith("==> Fatal error occurred") and self.has_errors():
            return
        if message == "Emergency stop." and self.has_errors():
            return
        entry = self.add(message, "error", None)
        self.pending.append(entry)
        if message.startswith("Undefined control sequence"):
            self.want_cs = entry

    def has_errors(self) -> bool:
        return any(e.severity == "error" for e in self.out)

    def resolve_pending(self, number: int) -> None:
        _, line_ok = self.location()
        for entry in self.pending:
            if entry.line is None and line_ok:
                entry.line = number
        self.pending.clear()

    def note_control_sequence(self, context: str) -> None:
        entry, self.want_cs = self.want_cs, None
        if entry is None:
            return
        found = _CONTROL_SEQ_RE.findall(context)
        if found:
            entry.message = f"Undefined control sequence: {found[-1]}"

    # -- main loop ------------------------------------------------------------------------

    def parse(self, lines: list[str]) -> None:
        mode = "normal"  # normal | error | box
        skip_next = False
        i = 0
        while i < len(lines):
            line = lines[i]
            i += 1
            stripped = line.strip()

            if mode == "box":
                if not stripped:
                    mode = "normal"
                continue

            ref = _LINE_REF_RE.match(line)
            if ref:
                if self.want_cs is not None:
                    self.note_control_sequence(ref.group(2) or "")
                if self.pending:
                    self.resolve_pending(int(ref.group(1)))
                skip_next = True  # the next line is the rest of the source line
                continue
            if skip_next:
                skip_next = False
                if mode == "error" and not stripped:
                    mode = "normal"
                continue

            if line.startswith("!"):
                self.add_error(line[1:].strip())
                mode = "error"
                continue

            fle = _FILE_LINE_ERROR_RE.match(line)
            if fle:
                rel = self.paths.repo_relative(fle.group("file")) or self.main
                entry = LogError(
                    file=rel,
                    line=int(fle.group("line")),
                    message=fle.group("msg"),
                    severity="error",
                )
                self.out.append(entry)
                if entry.message.startswith("Undefined control sequence"):
                    self.want_cs = entry
                mode = "error"
                continue

            if mode == "error":
                if self.want_cs is not None and stripped:
                    self.note_control_sequence(stripped)
                if not stripped:
                    mode = "normal"
                continue

            warn = _WARNING_RE.match(line)
            if warn:
                i = self.warning(warn, lines, i)
                continue

            pdf_warn = _PDFTEX_WARNING_RE.search(line)
            if pdf_warn:
                what = pdf_warn.group("what")
                prefix = f"pdfTeX warning ({what})" if what else "pdfTeX warning"
                self.add(f"{prefix}: {pdf_warn.group('msg').strip()}", "warning", None)
                continue

            if _BOX_RE.match(line):
                m = _BOX_LINES_RE.search(line)
                self.add(stripped, "info", int(m.group(1)) if m else None)
                mode = "box"
                continue

            self.track_parens(line)

    def warning(self, warn: re.Match[str], lines: list[str], i: int) -> int:
        """Consume a (possibly multi-line) warning starting at lines[i-1]; return next index."""
        name = warn.group("pkg") or warn.group("cls")
        prefixes = ["(Font)", "(LaTeX)"]
        if name:
            prefixes.append(f"({name})")
        parts = [warn.group("msg").strip()]
        while i < len(lines):
            nxt = lines[i]
            matched = next((p for p in prefixes if nxt.startswith(p)), None)
            if matched:
                parts.append(nxt[len(matched) :].strip())
            elif nxt.startswith("    ") and nxt.strip() and not nxt.lstrip().startswith("("):
                parts.append(nxt.strip())
            else:
                break
            i += 1
        text = " ".join(p for p in parts if p)
        number: int | None = None
        m = _INPUT_LINE_RE.search(text)
        if m:
            number = int(m.group(1))
            text = (text[: m.start()] + text[m.end() :]).strip()
        if warn.group("pkg"):
            text = f"Package {warn.group('pkg')}: {text}"
        elif warn.group("cls"):
            text = f"Class {warn.group('cls')}: {text}"
        severity: Severity = "info" if warn.group("font") else "warning"
        self.add(text, severity, number)
        return i


def _dedupe(entries: list[LogError]) -> list[LogError]:
    seen: set[tuple[str, int | None, str, str]] = set()
    out: list[LogError] = []
    for e in entries:
        key = (e.file, e.line, e.message, e.severity)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def parse_log(
    text: str,
    main: str,
    repo_root: Path | str | None = None,
    *,
    extra_roots: Iterable[str] = (),
) -> list[LogError]:
    """Parse a TeX log. `repo_root` lets absolute paths inside the repo map to repo files.

    `extra_roots` are further spellings of the repo root, e.g. a Docker `container_root`.
    """
    roots: list[str] = [r.rstrip("/") for r in extra_roots if r and r.strip("/")]
    if repo_root is not None:
        roots.append(str(repo_root))
        try:
            resolved = str(Path(repo_root).resolve())
        except OSError:
            resolved = ""
        if resolved and resolved not in roots:
            roots.append(resolved)
        # macOS reports /private/var/... for paths the user knows as /var/...
        for r in list(roots):
            if r.startswith("/private/"):
                roots.append(r[len("/private") :])
            elif r.startswith(("/var/", "/tmp/")):
                roots.append("/private" + r)
    parser = _Parser(main, roots)
    try:
        parser.parse(unwrap(text.splitlines()))
    except Exception:  # noqa: BLE001 - the parser must never take down a compile
        pass
    return _dedupe(parser.out)
