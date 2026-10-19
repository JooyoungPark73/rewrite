"""SyncTeX in both directions: `synctex view` (source → PDF) and `synctex edit` (PDF → source).

Coordinate convention, measured 2026-10-18 on Linux with the synctex CLI 1.5 (parser 1.21)
from TeX Live 2023 and checked again on TL2024 in Docker. The sample paper (US letter,
612 x 792 pt) was compiled and each `synctex view` box compared with `pdftotext -bbox`, whose
origin is the page's top-left corner:

    source line                     synctex view: h, v, W, H        pdftotext yMin - yMax
    main.tex:11 (title, top)        72.00, 130.04, 468.00, 15.11    114.94 - 130.05
    main.tex:22 (abstract)          86.94, 267.40, 425.78,  8.80    258.60 - 267.40
    sections/intro.tex:22           72.00, 562.23, 468.00,  9.64    552.59 - 562.22
    sections/method.tex:5 (bottom)  72.00, 661.60, 468.00,  9.64    651.96 - 661.59

So `synctex view` reports big points (1/72 in) from the page's TOP-LEFT corner, y growing
downwards: a line near the top has a small `v`, a line near the bottom a large one. `h` is
the box's left edge, `v` its BOTTOM edge (baseline plus depth) and `H` (height plus depth)
extends upwards from `v`. The API therefore returns x=h, y=v-H, w=W, h=H. The `x:`/`y:`
fields of a record are a reference point inside the box and are not used.

`synctex edit -o page:x:y:pdf` takes a point in the same top-left convention: the centre of
a box, (h + W/2, v - H/2), maps back to the line it came from, e.g. (306.0, 381.24) →
sections/intro.tex:4 and (306.0, 656.78) → sections/method.tex:5. (A title typeset by
`\\maketitle` maps to the `\\maketitle` line, as usual for TeX.) `Line:` is 1-based;
`Column:` is -1 when unknown and is returned as 0.

Input names: pdfTeX records every input as an absolute path with a `./` segment relative to
the compile directory, e.g. `<repo>/./sections/intro.tex`, and the Docker compiler records
host paths too, because the repo is mounted at its own host path. `synctex view -i` accepts
that exact name and also the repo-relative, `./`-prefixed and absolute forms, but not a bare
file name. `forward` passes the exact recorded name (read from the .synctex.gz), then falls
back to the repo-relative and absolute forms.

`synctex` runs on the host when it is on PATH. Otherwise, with the Docker compiler, it runs
inside the same TeX Live image (`docker run --rm -v <repo>:<repo> -w <repo> <image> synctex
...`), whose output paths are host paths already. Each call has a 20 s timeout.
"""

from __future__ import annotations

import functools
import gzip
import math
import os
import posixpath
import shutil
import subprocess
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from app.compiler.docker import container_name, docker_image, kill_container, run_prefix
from app.config import CompilerConfig
from app.repo import REWRITE_DIR

SYNCTEX_TIMEOUT_S = 20.0
BUILD_DIR_REL = f"{REWRITE_DIR}/build"
RESULT_BEGIN = "SyncTeX result begin"
RESULT_END = "SyncTeX result end"


class SyncBox(BaseModel):
    """A box on a PDF page: 1-based page, PDF points, origin at the page's top-left corner."""

    page: int
    x: float
    y: float
    w: float
    h: float


class SyncEdit(BaseModel):
    """A source location: repo-relative file, 1-based line, 0-based column (0 if unknown)."""

    file: str
    line: int
    col: int


class SynctexError(RuntimeError):
    """synctex could not run or failed (→ 500)."""


class SynctexUnavailable(SynctexError):
    """No synctex: not on PATH and no Docker fallback (→ 503)."""


class SynctexTimeout(SynctexError):
    """synctex did not answer within the timeout (→ 504)."""


class NoSynctexData(FileNotFoundError):
    """No PDF or no .synctex(.gz) in the build directory yet (→ 404)."""


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class ViewRecord:
    """One `synctex view` result, in synctex's own convention (see module docstring)."""

    page: int
    h: float
    v: float
    width: float
    height: float
    output: str = ""


@dataclass(frozen=True)
class EditRecord:
    """One `synctex edit` result. `column` is -1 when synctex does not know it."""

    input: str
    line: int
    column: int
    output: str = ""


def _blocks(text: str, keys: Iterable[str]) -> list[dict[str, str]]:
    """Split synctex stdout into `key: value` records.

    Only lines between `SyncTeX result begin` and `... end` are read when those markers are
    present. A record starts at `Output:` or when a key repeats within the current record.
    Values are kept verbatim after the first colon (paths may contain colons).
    """
    known = set(keys)
    lines = text.splitlines()
    stripped = [ln.strip() for ln in lines]
    if RESULT_BEGIN in stripped:
        start = stripped.index(RESULT_BEGIN) + 1
        end = stripped.index(RESULT_END, start) if RESULT_END in stripped[start:] else len(lines)
        lines = lines[start:end]
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in known:
            continue
        if current is None or key == "Output" or key in current:
            current = {}
            records.append(current)
        current[key] = value.strip()
    return records


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_view_output(text: str) -> list[ViewRecord]:
    """Parse `synctex view` stdout. Records missing a field are skipped."""
    out: list[ViewRecord] = []
    keys = ("Output", "Page", "x", "y", "h", "v", "W", "H", "before", "offset", "middle", "after")
    for rec in _blocks(text, keys):
        page = _int(rec.get("Page"))
        h, v = _float(rec.get("h")), _float(rec.get("v"))
        width, height = _float(rec.get("W")), _float(rec.get("H"))
        if page is None or page < 1 or None in (h, v, width, height):
            continue
        assert h is not None and v is not None and width is not None and height is not None
        out.append(ViewRecord(page, h, v, width, height, rec.get("Output", "")))
    return out


def parse_edit_output(text: str) -> list[EditRecord]:
    """Parse `synctex edit` stdout. Records without Input or Line are skipped."""
    out: list[EditRecord] = []
    keys = ("Output", "Input", "Line", "Column", "Offset", "Context")
    for rec in _blocks(text, keys):
        name, line = rec.get("Input", ""), _int(rec.get("Line"))
        if not name or line is None:
            continue
        column = _int(rec.get("Column"))
        out.append(EditRecord(name, line, -1 if column is None else column, rec.get("Output", "")))
    return out


def view_box(record: ViewRecord) -> SyncBox:
    """synctex's (h, v = bottom edge, W, H upwards) → a top-left-origin box."""
    return SyncBox(
        page=record.page,
        x=record.h,
        y=record.v - record.height,
        w=record.width,
        h=record.height,
    )


def _dedupe_boxes(boxes: Iterable[SyncBox]) -> list[SyncBox]:
    seen: set[tuple[int, float, float, float, float]] = set()
    out: list[SyncBox] = []
    for box in boxes:
        key = (box.page, round(box.x, 2), round(box.y, 2), round(box.w, 2), round(box.h, 2))
        if key not in seen:
            seen.add(key)
            out.append(box)
    return out


# --------------------------------------------------------------------------- paths


def _clean_abs(path: str) -> str:
    norm = posixpath.normpath(path)
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm


def _toggle_private(path: str) -> str | None:
    """macOS: /var, /tmp and /etc are symlinks into /private; TeX may report either form."""
    if path == "/private" or path.startswith("/private/"):
        rest = path[len("/private") :]
        return rest if rest.startswith(("/var", "/tmp", "/etc")) else None
    if path in ("/var", "/tmp", "/etc") or path.startswith(("/var/", "/tmp/", "/etc/")):
        return "/private" + path
    return None


def _spellings(path: str, *, resolve: bool) -> list[str]:
    """`path` normalised, its /private twin, and (optionally) its symlink-resolved forms."""
    base = [_clean_abs(path)]
    if resolve:
        try:
            base.append(os.path.realpath(path))
        except (OSError, ValueError):
            pass
    out: list[str] = []
    for p in base:
        for candidate in (p, _toggle_private(p)):
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def _under(path: str, root: str) -> str | None:
    """`path` relative to `root` (both absolute and normalised), or None if outside."""
    root = root.rstrip("/")
    if path == root or (not root and path == "/"):
        return "."
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


def _root_spellings(repo: Path) -> list[str]:
    out = _spellings(str(repo), resolve=False)
    try:
        resolved = str(repo.resolve())
    except (OSError, RuntimeError):
        resolved = ""
    for candidate in _spellings(resolved, resolve=False) if resolved else []:
        if candidate not in out:
            out.append(candidate)
    return out


def _build_rel(repo: Path, build_dir: Path | None, roots: list[str]) -> str | None:
    if build_dir is None:
        return BUILD_DIR_REL
    for candidate in _spellings(str(build_dir), resolve=True):
        for root in roots:
            rel = _under(candidate, root)
            if rel not in (None, "."):
                return rel
    return None


def normalize_path(
    raw: str,
    repo: Path,
    *,
    build_dir: Path | None = None,
    container_root: str | None = None,
) -> str | None:
    """Map a path reported by TeX or synctex to a repo-relative POSIX path (None if outside).

    Handles absolute paths with `./` or `//` segments, `./`-prefixed relative paths,
    macOS's `/private/var/...` vs `/var/...` on either side, symlinked repo roots (both
    sides are resolved), paths relative to the build directory, and a configured Docker
    `container_root` (by default the container path equals the host path, so no mapping is
    needed). Relative paths are taken relative to the repo (the compile directory), falling
    back to the build directory (where the .synctex.gz lives) when only that file exists.
    """
    text = raw.strip()
    if not text or "\x00" in text:
        return None
    repo = Path(repo)
    roots = _root_spellings(repo)
    build_rel = _build_rel(repo, build_dir, roots)

    croot = (container_root or "").rstrip("/")
    if croot and (text == croot or text.startswith(croot + "/")):
        text = str(repo) + text[len(croot) :]

    rel: str | None = None
    if text.startswith("/"):
        for candidate in _spellings(text, resolve=True):
            for root in roots:
                rel = _under(candidate, root)
                if rel is not None:
                    break
            if rel is not None:
                break
    else:
        rel = posixpath.normpath(text)
        if rel == ".." or rel.startswith("../"):
            return None  # relative to the compile directory (the repo), so outside it
        if build_rel and not (repo / rel).exists() and (repo / build_rel / rel).exists():
            rel = posixpath.join(build_rel, rel)
    if rel is None or rel in ("", "."):
        return None

    # A source file reported under the build dir (some synctex versions join a relative
    # input name with the directory of the .synctex.gz): use the real source file.
    if build_rel and rel.startswith(build_rel + "/"):
        rest = rel[len(build_rel) + 1 :]
        if not (repo / rel).exists() and (repo / rest).exists():
            rel = rest
    return PurePosixPath(rel).as_posix()


# --------------------------------------------------------------------------- running synctex

SynctexRun = Callable[[Sequence[str]], str]
"""Runs `synctex <args>` with the repo as working directory and returns its stdout."""


def _run(
    cmd: list[str], *, cwd: Path, timeout_s: float, on_timeout: Callable[[], None] | None = None
) -> str:
    try:
        done = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        if on_timeout is not None:
            on_timeout()
        raise SynctexTimeout(f"synctex did not answer within {timeout_s:.0f} s") from exc
    except OSError as exc:
        raise SynctexError(f"could not run synctex: {exc}") from exc
    if done.returncode != 0:
        lines = [ln.strip() for ln in (done.stderr + "\n" + done.stdout).splitlines()]
        errors = [ln for ln in lines if "ERROR" in ln or "Error" in ln]
        detail = (errors or [ln for ln in lines if ln] or ["no output"])[0]
        raise SynctexError(f"synctex exited with {done.returncode}: {detail}")
    return done.stdout


def host_runner(
    repo: Path, *, synctex: str = "synctex", timeout_s: float = SYNCTEX_TIMEOUT_S
) -> SynctexRun:
    def run(args: Sequence[str]) -> str:
        return _run([synctex, *args], cwd=repo, timeout_s=timeout_s)

    return run


def docker_runner(
    repo: Path,
    image: str,
    *,
    container_root: str | None = None,
    docker: str = "docker",
    timeout_s: float = SYNCTEX_TIMEOUT_S,
) -> SynctexRun:
    """synctex inside the TeX Live image, repo mounted as for the Docker compiler."""

    def run(args: Sequence[str]) -> str:
        name = container_name()
        prefix = run_prefix(repo, image, container_root=container_root, name=name, docker=docker)
        return _run(
            [*prefix, "synctex", *args],
            cwd=repo,
            timeout_s=timeout_s,
            on_timeout=lambda: kill_container(name, docker),
        )

    return run


def select_runner(
    repo: Path, compiler: CompilerConfig, *, synctex: str = "synctex", docker: str = "docker"
) -> SynctexRun:
    """Host synctex if on PATH, else (Docker compiler only) synctex inside the image."""
    if shutil.which(synctex) is not None:
        return host_runner(repo, synctex=synctex)
    if compiler.kind == "docker":
        if shutil.which(docker) is None:
            raise SynctexUnavailable("synctex is not on PATH and docker was not found")
        return docker_runner(
            repo,
            docker_image(compiler.texlive_year),
            container_root=compiler.container_root,
            docker=docker,
        )
    raise SynctexUnavailable(
        "synctex was not found on PATH (it ships with TeX Live; or switch to the docker compiler)"
    )


# --------------------------------------------------------------------------- queries


def build_outputs(build_dir: Path, main: str) -> tuple[Path, Path]:
    """(PDF, .synctex.gz or .synctex) for `main`; NoSynctexData if either is missing."""
    stem = PurePosixPath(main).stem
    pdf = build_dir / f"{stem}.pdf"
    if not pdf.is_file():
        raise NoSynctexData("no PDF yet; compile first")
    for suffix in (".synctex.gz", ".synctex"):
        data = build_dir / f"{stem}{suffix}"
        if data.is_file():
            return pdf, data
    raise NoSynctexData("no SyncTeX data yet; compile first")


@functools.lru_cache(maxsize=8)
def _read_inputs(path: str, mtime_ns: int, size: int) -> tuple[str, ...] | None:
    del mtime_ns, size  # cache key only: a new compile invalidates the entry
    opener = gzip.open if path.endswith(".gz") else open
    names: list[str] = []
    try:
        with opener(path, "rb") as fh:
            for raw in fh:
                if raw.startswith(b"Input:"):
                    # Input:<tag>:<name>
                    _, _, name = raw[len(b"Input:") :].rstrip(b"\r\n").partition(b":")
                    decoded = name.decode("utf-8", errors="surrogateescape")
                    if decoded and decoded not in names:
                        names.append(decoded)
    except (OSError, EOFError, zlib.error):
        return None
    return tuple(names)


def recorded_inputs(synctex_file: Path) -> tuple[str, ...] | None:
    """Every input name in a .synctex(.gz), as TeX recorded it; None if unreadable."""
    try:
        st = synctex_file.stat()
    except OSError:
        return None
    return _read_inputs(str(synctex_file), st.st_mtime_ns, st.st_size)


def _pdf_arg(repo: Path, pdf: Path) -> str:
    """The PDF relative to the repo (the cwd, also inside the container), else absolute."""
    try:
        return pdf.relative_to(repo).as_posix()
    except ValueError:
        try:
            return pdf.resolve().relative_to(repo.resolve()).as_posix()
        except (ValueError, OSError):
            return str(pdf)


def forward(
    repo: Path,
    build_dir: Path,
    main: str,
    file: str,
    line: int,
    col: int = 0,
    *,
    run: SynctexRun | None = None,
    container_root: str | None = None,
) -> list[SyncBox]:
    """Boxes for `file:line` (repo-relative file, 1-based line), top-left PDF points.

    Empty when the file is not part of the compiled document or synctex finds nothing.
    Raises NoSynctexData before the first compile, SynctexError if synctex fails.
    """
    repo = Path(repo)
    pdf, data = build_outputs(build_dir, main)
    rel = posixpath.normpath(file)
    run = run or host_runner(repo)

    candidates: list[str] = []
    names = recorded_inputs(data)
    if names is not None:
        candidates = [
            name
            for name in names
            if normalize_path(name, repo, build_dir=build_dir, container_root=container_root) == rel
        ]
        if not candidates:
            return []  # this file is not an input of the compiled document
    for fallback in (rel, str(repo / rel)):
        if fallback not in candidates:
            candidates.append(fallback)

    pdf_arg = _pdf_arg(repo, pdf)
    for name in candidates:
        out = run(["view", "-i", f"{line}:{max(col, 0)}:{name}", "-o", pdf_arg])
        boxes = _dedupe_boxes(view_box(r) for r in parse_view_output(out))
        if boxes:
            return boxes
    return []


def reverse(
    repo: Path,
    build_dir: Path,
    main: str,
    page: int,
    x: float,
    y: float,
    *,
    run: SynctexRun | None = None,
    container_root: str | None = None,
) -> SyncEdit | None:
    """The source location under a point (1-based page, top-left PDF points), or None.

    Records outside the repo (class and package files) and generated files under
    `.rewrite/` (e.g. the .bbl) are skipped.
    """
    repo = Path(repo)
    pdf, _ = build_outputs(build_dir, main)
    run = run or host_runner(repo)
    out = run(["edit", "-o", f"{page}:{x:.3f}:{y:.3f}:{_pdf_arg(repo, pdf)}"])
    for record in parse_edit_output(out):
        rel = normalize_path(record.input, repo, build_dir=build_dir, container_root=container_root)
        if rel is None or rel.split("/", 1)[0] == REWRITE_DIR:
            continue
        return SyncEdit(file=rel, line=max(record.line, 1), col=max(record.column, 0))
    return None
