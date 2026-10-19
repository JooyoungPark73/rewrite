"""Paper repo files: strict path containment, atomic writes, and the `\\input` tree.

Every path that arrives over HTTP is repo-relative and goes through `resolve_in_repo`,
which rejects absolute paths, `..` components and symlinks that resolve outside the repo.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel

from app.repo import PAPER_JSON_NAME, REWRITE_DIR

MAX_TEXT_BYTES = 10 * 1024 * 1024


class PathError(ValueError):
    """A request path that cannot be served. Mapped to an HTTP status by the API layer."""


class PathEscapeError(PathError):
    """The path is absolute, contains `..`, or resolves outside the repo (→ 400)."""


class ProtectedPathError(PathError):
    """The path is inside the repo but must not be written through the file API (→ 403)."""


class NotTextError(ValueError):
    """The file is not UTF-8 text (→ 415)."""


class TooLargeError(ValueError):
    """The file or the new content exceeds MAX_TEXT_BYTES (→ 413)."""


class FileConflictError(ValueError):
    """The file on disk is not the revision the write was based on (→ 409)."""


# --------------------------------------------------------------------------- paths


def _validate_relative(rel: str) -> PurePosixPath:
    if not rel or not rel.strip():
        raise PathEscapeError("empty path")
    if "\x00" in rel:
        raise PathEscapeError("path contains a NUL byte")
    if "\\" in rel:
        raise PathEscapeError("use forward slashes in repo paths")
    posix = PurePosixPath(rel)
    if posix.is_absolute() or PureWindowsPath(rel).is_absolute() or PureWindowsPath(rel).drive:
        raise PathEscapeError(f"absolute paths are not allowed: {rel}")
    if any(part == ".." for part in posix.parts):
        raise PathEscapeError(f"'..' is not allowed in paths: {rel}")
    return posix


def resolve_in_repo(root: Path, rel: str) -> Path:
    """Resolve a repo-relative path, following symlinks, and require it to stay in the repo."""
    posix = _validate_relative(rel)
    root_resolved = root.resolve()
    resolved = (root_resolved / Path(*posix.parts)).resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise PathEscapeError(f"path resolves outside the repo: {rel}")
    return resolved


def to_repo_relative(root: Path, path: Path) -> str:
    """Repo-relative posix path for a path known to be inside the repo."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def normalize_rel(rel: str) -> str:
    """Canonical spelling of a repo-relative path (`./a//b.tex` → `a/b.tex`)."""
    return PurePosixPath(posixpath.normpath(rel)).as_posix()


def is_protected_for_write(rel: str) -> bool:
    """Paths the generic file API must never write.

    The spec file (PAPER_JSON_NAME) is written only by the spec editor (phase 3,
    app/spec.py). `.git/` belongs to git. Under `.rewrite/`, `build/` and `state.db*`
    are app-owned outputs and `sandbox/` is regenerated from prompts/system.md.
    """
    # Compared case-insensitively: macOS file systems are case-insensitive by default, so
    # "Paper.JSON" names the same file as the spec.
    parts = tuple(part.casefold() for part in PurePosixPath(normalize_rel(rel)).parts)
    if not parts:
        return True
    if parts[0] == ".git":
        return True
    if parts == (PAPER_JSON_NAME.casefold(),):
        return True
    if parts[0] == REWRITE_DIR.casefold() and len(parts) > 1:
        if parts[1] in ("build", "sandbox"):
            return True
        if parts[1].startswith("state.db"):
            return True
    return False


# --------------------------------------------------------------------------- read / write


def atomic_write_text(path: Path, text: str, check: Callable[[], None] | None = None) -> None:
    """Write via a temp file in the same directory and an atomic rename.

    Keeps the existing file's permission bits. Newlines are written exactly as given.
    `check`, when given, runs after the temp file is written and right before the rename
    (the last moment the old file can be inspected); if it raises, nothing is replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if check is not None:
            check()
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_text_file(root: Path, rel: str) -> tuple[str, float]:
    """Return (content, mtime). Raises FileNotFoundError, PathError, TooLargeError, NotTextError."""
    path = resolve_in_repo(root, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    stat = path.stat()
    if stat.st_size > MAX_TEXT_BYTES:
        raise TooLargeError(f"file is larger than {MAX_TEXT_BYTES} bytes: {rel}")
    data = path.read_bytes()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotTextError(f"not a UTF-8 text file: {rel}") from exc
    return content, stat.st_mtime


def content_sha256(content: str) -> str:
    """The revision token of a text file: sha256 of its UTF-8 bytes (GET/PUT /api/file)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _disk_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def write_text_file(root: Path, rel: str, content: str, *, base_sha256: str | None = None) -> float:
    """Atomically write a repo file and return its new mtime.

    Writes go to the symlink target (when the target is inside the repo), so a symlinked
    file stays a symlink. With `base_sha256` (the revision the new content was made from,
    see `content_sha256`) the write happens only if the file still is that revision,
    checked right before the atomic rename; otherwise FileConflictError and nothing is
    written. A missing file never matches a base revision.
    """
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise TooLargeError(f"content is larger than {MAX_TEXT_BYTES} bytes")
    path = resolve_in_repo(root, rel)
    rel_target = to_repo_relative(root, path)
    if is_protected_for_write(rel) or is_protected_for_write(rel_target):
        raise ProtectedPathError(f"this path cannot be written through the file API: {rel}")
    if path.exists() and not path.is_file():
        raise PathEscapeError(f"not a regular file: {rel}")

    def unchanged() -> None:
        if _disk_sha256(path) != base_sha256:
            raise FileConflictError(
                f"{normalize_rel(rel)} changed on disk since the version this edit is based "
                "on (another save, a marker insertion or a git pull); reload it and apply "
                "the edit again"
            )

    if base_sha256 is not None:
        unchanged()  # fail early, before writing the temp file
    atomic_write_text(path, content, unchanged if base_sha256 is not None else None)
    return path.stat().st_mtime


# --------------------------------------------------------------------------- \input tree


class TexFile(BaseModel):
    path: str  # repo-relative posix path
    included_by: str | None  # repo-relative path of the including file; null for main
    kind: Literal["tex", "bib"] = "tex"


class TexTree(BaseModel):
    main: str
    files: list[TexFile]
    missing: list[TexFile] = []


# `\cmd{arg}` (with an optional `[...]`), or TeX's primitive form of `\input`: an unbraced
# name, `\input section` / `\input sub/x.tex` (the name ends at a space, a brace or the next
# macro, as TeX reads it) or `\input "a b"` (quoted, may hold spaces). `\include` and the
# others take a macro argument, so only `\input` has an unbraced form.
_INCLUDE_RE = re.compile(
    r"\\(?:(?P<cmd>input|include|subfile|bibliography|addbibresource)(?![A-Za-z@])"
    r"\s*(?:\[[^\]]*\])?\s*\{(?P<arg>[^{}]*)\}"
    r"|(?P<bare>input)(?![A-Za-z@])[ \t]*(?:\"(?P<quoted>[^\"]+)\"|(?P<word>[^\s\\{}\"%]+)))"
)


def _include_match(m: re.Match[str]) -> tuple[str, str]:
    """(command, raw argument) of an `_INCLUDE_RE` match, braced or not."""
    if m.group("cmd") is not None:
        return m.group("cmd"), m.group("arg")
    return m.group("bare"), m.group("quoted") or m.group("word") or ""


_BEGIN_COMMENT_RE = re.compile(r"\\begin\s*\{comment\}")
_END_COMMENT_RE = re.compile(r"\\end\s*\{comment\}")


def strip_tex_comment(line: str) -> str:
    """Drop everything from the first unescaped `%` (`\\%` is a literal percent sign)."""
    backslashes = 0
    for i, ch in enumerate(line):
        if ch == "%" and backslashes % 2 == 0:
            return line[:i]
        backslashes = backslashes + 1 if ch == "\\" else 0
    return line


def _active_source(text: str) -> list[str]:
    """Source lines with comments and `comment` environments removed."""
    out: list[str] = []
    in_comment_env = False
    for raw in text.splitlines():
        line = strip_tex_comment(raw)
        if in_comment_env:
            if _END_COMMENT_RE.search(line):
                in_comment_env = False
            out.append("")
            continue
        if _BEGIN_COMMENT_RE.search(line):
            in_comment_env = True
            line = _BEGIN_COMMENT_RE.split(line, maxsplit=1)[0]
        out.append(line)
    return out


def _candidates(cmd: str, name: str) -> list[str]:
    if cmd == "include":
        return [name + ".tex"]
    if cmd in ("bibliography",):
        return [name if name.endswith(".bib") else name + ".bib"]
    if cmd == "addbibresource":
        return [name]
    if name.endswith(".tex"):
        return [name]
    return [name + ".tex", name]


def _find(root: Path, base_dirs: list[str], cmd: str, name: str) -> tuple[str | None, str]:
    """Return (resolved repo-relative path or None, display path for a missing file)."""
    candidates = _candidates(cmd, name)
    display = normalize_rel(candidates[0])
    for base in base_dirs:
        for cand in candidates:
            rel = normalize_rel(posixpath.join(base, cand)) if base else normalize_rel(cand)
            try:
                path = resolve_in_repo(root, rel)
            except PathEscapeError:
                continue
            if path.is_file():
                return to_repo_relative(root, path), rel
    return None, display


INCLUDE_COMMANDS = ("input", "include", "subfile")


def strip_include_commands(line: str) -> str:
    """A comment-stripped line without its `\\input`/`\\include`/`\\subfile` and
    bibliography commands (braced or not): what is left is the line's own content."""
    return _INCLUDE_RE.sub("", line)


def include_targets(root: Path, including: str, line: str) -> list[str]:
    """Repo-relative .tex files that one active (comment-stripped) source line pulls in
    with \\input, \\include or \\subfile, in order, resolved like `resolve_tex_tree`.
    Missing files and computed names are skipped."""
    here = posixpath.dirname(including)
    base_dirs = [""] if not here else ["", here]
    found: list[str] = []
    for m in _INCLUDE_RE.finditer(line):
        cmd, arg = _include_match(m)
        if cmd not in INCLUDE_COMMANDS:
            continue
        name = arg.strip().strip('"').strip()
        if not name or "#" in name or "\\" in name:
            continue
        target, _ = _find(root, base_dirs, cmd, name)
        if target is not None:
            found.append(target)
    return found


def resolve_tex_tree(root: Path, main: str, max_depth: int = 32) -> TexTree:
    """Files reachable from `main` via \\input (braced or not), \\include, \\subfile (and
    bibliographies).

    Order is depth-first in document order, main first. Each file appears once (cycles and
    repeated includes are cut). Paths resolve against the repo root, which is TeX's working
    directory when latexmk runs, falling back to the including file's directory.
    """
    main_rel = normalize_rel(main)
    files: list[TexFile] = []
    missing: list[TexFile] = []
    seen: set[str] = set()
    missing_seen: set[str] = set()

    try:
        main_path = resolve_in_repo(root, main_rel)
    except PathEscapeError:
        return TexTree(main=main_rel, files=[], missing=[TexFile(path=main_rel, included_by=None)])
    if not main_path.is_file():
        return TexTree(main=main_rel, files=[], missing=[TexFile(path=main_rel, included_by=None)])

    def visit(rel: str, included_by: str | None, depth: int) -> None:
        seen.add(rel)
        files.append(TexFile(path=rel, included_by=included_by))
        if depth >= max_depth:
            return
        try:
            text = resolve_in_repo(root, rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        here = posixpath.dirname(rel)
        base_dirs = [""] if not here else ["", here]
        for line in _active_source(text):
            for m in _INCLUDE_RE.finditer(line):
                cmd, arg = _include_match(m)
                names = [arg]
                if cmd == "bibliography":
                    names = arg.split(",")
                for raw_name in names:
                    name = raw_name.strip().strip('"').strip()
                    if not name or "#" in name or "\\" in name:
                        continue  # macro arguments and computed names cannot be resolved
                    found, display = _find(root, base_dirs, cmd, name)
                    kind: Literal["tex", "bib"] = (
                        "bib" if cmd in ("bibliography", "addbibresource") else "tex"
                    )
                    if found is None:
                        if display not in missing_seen and not display.startswith(".."):
                            missing_seen.add(display)
                            missing.append(TexFile(path=display, included_by=rel, kind=kind))
                        continue
                    if found in seen:
                        continue
                    if kind == "bib":
                        seen.add(found)
                        files.append(TexFile(path=found, included_by=rel, kind="bib"))
                    else:
                        visit(found, rel, depth + 1)

    visit(to_repo_relative(root, main_path), None, 0)
    return TexTree(main=main_rel, files=files, missing=missing)
