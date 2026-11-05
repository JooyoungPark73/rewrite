"""Figure PDFs rendered to PNG for the LLM runs (2026-10-26).

Claude's Read tool shows a PDF page as an image by itself; codex can only view image files
and, under its read-only sandbox, cannot write one. So before a prompt is assembled the app
renders every PDF of the paper repo (figures, and any paper PDF kept in the repo) to
`.rewrite/build/figures/<path>.png` with pypdfium2 (a wheel in the uv environment, no
system tool), and the prompt's "Paper files" section maps each PDF to its image. Page 1 is
`<stem>.png`; pages 2 to FIGURE_MAX_PAGES are `<stem>-p<n>.png`.

Renders are cached by the source file's sha256 in `index.json` next to the images and
written atomically (temp file and rename), so concurrent assemblies never see a half
image. A PDF that cannot be rendered is listed with its error and never raises. The build
directory is gitignored in the paper repo.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

FIGURES_DIR = "figures"  # under .rewrite/build/
INDEX_NAME = "index.json"
FIGURE_DPI = 200  # 72 pt = 1 in; a 3.5 in wide figure becomes ~970 px
FIGURE_MAX_SIDE = 2400  # px; a large poster-size PDF is scaled down to this
FIGURE_MAX_PAGES = 3  # pages rendered per PDF (a figure has one; a paper copy many)
FIGURE_MAX_BYTES = 64 * 1024 * 1024
PDF_SUFFIX = ".pdf"


class RenderedFigure(BaseModel):
    source: str  # repo-relative path of the PDF
    sha256: str
    pages: int = 0  # pages in the PDF (0 when it could not be opened)
    images: list[str] = []  # repo-relative paths of the PNGs, page order
    error: str | None = None  # why it has no images


class FigureIndex(BaseModel):
    figures: dict[str, RenderedFigure] = {}


def is_pdf(rel: str) -> bool:
    return rel.lower().endswith(PDF_SUFFIX)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_index(path: Path) -> FigureIndex:
    try:
        return FigureIndex.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return FigureIndex()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def image_paths(out_dir: Path, rel: str, pages: int) -> list[Path]:
    """Where the PNGs of `rel` (a repo-relative PDF path) go: `<stem>.png`, then
    `<stem>-p2.png`, ... up to FIGURE_MAX_PAGES."""
    base = out_dir / Path(rel).with_suffix("")
    shown = min(pages, FIGURE_MAX_PAGES)
    return [
        base.with_name(base.name + ("" if i == 0 else f"-p{i + 1}") + ".png") for i in range(shown)
    ]


def _render(pdf_path: Path, out_dir: Path, rel: str) -> tuple[int, list[Path]]:
    """Render `pdf_path` and return (page count, written PNGs). Raises on a bad PDF."""
    import pypdfium2 as pdfium  # loads the pdfium library; kept out of app start-up

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        pages = len(document)
        targets = image_paths(out_dir, rel, pages)
        for index, target in enumerate(targets):
            page = document[index]
            try:
                width, height = page.get_size()
                scale = min(FIGURE_DPI / 72, FIGURE_MAX_SIDE / max(width, height, 1.0))
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                page.close()
            tmp_target = target.with_name(target.name + ".render")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                image.save(tmp_target, format="PNG")
                os.replace(tmp_target, target)
            except BaseException:
                tmp_target.unlink(missing_ok=True)
                raise
        return pages, targets
    finally:
        document.close()


def render_figures(root: Path, build_dir: Path, sources: list[str]) -> list[RenderedFigure]:
    """Render the PDFs among `sources` (repo-relative paths under `root`) that changed since
    the last call, drop images of PDFs that are gone, and return one entry per PDF in the
    order given. Never raises for a bad PDF: its entry carries `error`."""
    out_dir = build_dir / FIGURES_DIR
    index_path = out_dir / INDEX_NAME
    index = _load_index(index_path)
    wanted = [rel for rel in sources if is_pdf(rel)]
    result: list[RenderedFigure] = []
    changed = False
    for rel in wanted:
        path = root / rel
        try:
            size = path.stat().st_size
            if size > FIGURE_MAX_BYTES:
                entry = RenderedFigure(
                    source=rel, sha256=f"size:{size}", error=f"not rendered: {size} bytes"
                )
                if index.figures.get(rel) != entry:
                    changed = True
                index.figures[rel] = entry
                result.append(entry)
                continue
            digest = _sha256(path)
        except OSError as exc:
            entry = RenderedFigure(source=rel, sha256="", error=f"cannot read: {exc}")
            index.figures[rel] = entry
            result.append(entry)
            changed = True
            continue
        cached = index.figures.get(rel)
        if (
            cached is not None
            and cached.sha256 == digest
            and all((root / image).is_file() for image in cached.images)
        ):
            result.append(cached)
            continue
        try:
            pages, targets = _render(path, out_dir, rel)
            entry = RenderedFigure(
                source=rel,
                sha256=digest,
                pages=pages,
                images=[target.relative_to(root).as_posix() for target in targets],
            )
        except Exception as exc:  # pypdfium2 raises its own and OSError
            log.warning("could not render %s: %s", path, exc)
            entry = RenderedFigure(source=rel, sha256=digest, error=f"could not render: {exc}")
        index.figures[rel] = entry
        result.append(entry)
        changed = True
    for rel in list(index.figures):
        if rel in wanted:
            continue
        for image in index.figures[rel].images:
            (root / image).unlink(missing_ok=True)
        del index.figures[rel]
        changed = True
    if changed:
        _atomic_write_bytes(index_path, index.model_dump_json(indent=2).encode("utf-8") + b"\n")
    return result
