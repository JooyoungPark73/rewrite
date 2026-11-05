"""app/figures.py: figure PDFs rendered to PNG for the runs (2026-10-26)."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from app import figures
from app.figures import FigureIndex, image_paths, render_figures
from tests.conftest import FIXTURES

PLOT = FIXTURES / "figures" / "plot.pdf"  # a one-page tikz plot (plot.tex next to it)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def paper(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "paper"
    (root / "figures").mkdir(parents=True)
    shutil.copy(PLOT, root / "figures" / "plot.pdf")
    build = root / ".rewrite" / "build"
    build.mkdir(parents=True)
    return root, build


def multipage(path: Path, pages: int) -> None:
    document = pdfium.PdfDocument.new()
    source = pdfium.PdfDocument(str(PLOT))
    document.import_pages(source, [0] * pages)
    document.save(str(path))


def test_render_writes_png_and_index(tmp_path: Path) -> None:
    root, build = paper(tmp_path)
    (entry,) = render_figures(root, build, ["main.tex", "figures/plot.pdf"])
    assert entry.source == "figures/plot.pdf" and entry.error is None and entry.pages == 1
    assert entry.images == [".rewrite/build/figures/figures/plot.png"]
    png = root / entry.images[0]
    assert png.read_bytes()[:8] == PNG_MAGIC
    from PIL import Image

    with Image.open(png) as image:
        width, height = image.size
    assert 500 < width < 1000 and 200 < height < 500  # 231x109 pt at 200 dpi
    index = FigureIndex.model_validate_json((build / "figures" / "index.json").read_text())
    assert index.figures["figures/plot.pdf"] == entry
    assert len(entry.sha256) == 64


def test_render_is_cached_by_content(tmp_path: Path) -> None:
    root, build = paper(tmp_path)
    (first,) = render_figures(root, build, ["figures/plot.pdf"])
    png = root / first.images[0]
    stamp = png.stat().st_mtime_ns
    time.sleep(0.02)
    (again,) = render_figures(root, build, ["figures/plot.pdf"])
    assert again == first and png.stat().st_mtime_ns == stamp  # not rewritten
    source = root / "figures" / "plot.pdf"
    source.write_bytes(source.read_bytes() + b"\n% touched\n")  # still a valid PDF
    (changed,) = render_figures(root, build, ["figures/plot.pdf"])
    assert changed.sha256 != first.sha256 and changed.error is None
    assert png.stat().st_mtime_ns != stamp  # re-rendered
    png.unlink()  # a missing image is rendered again even when the source is unchanged
    (restored,) = render_figures(root, build, ["figures/plot.pdf"])
    assert restored == changed and png.is_file()


def test_removed_pdf_drops_its_image(tmp_path: Path) -> None:
    root, build = paper(tmp_path)
    (entry,) = render_figures(root, build, ["figures/plot.pdf"])
    png = root / entry.images[0]
    assert png.is_file()
    assert render_figures(root, build, ["main.tex"]) == []
    assert not png.exists()
    assert (
        FigureIndex.model_validate_json((build / "figures" / "index.json").read_text()).figures
        == {}
    )


def test_bad_pdf_is_reported_not_raised(tmp_path: Path) -> None:
    root, build = paper(tmp_path)
    (root / "figures" / "broken.pdf").write_bytes(b"%PDF-1.4 not really\n")
    broken, plot = render_figures(root, build, ["figures/broken.pdf", "figures/plot.pdf"])
    assert broken.error is not None and broken.error.startswith("could not render")
    assert broken.images == [] and plot.error is None
    assert not list((build / "figures" / "figures").glob("broken*"))


def test_multipage_pdf_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, build = paper(tmp_path)
    multipage(root / "paper.pdf", 5)
    monkeypatch.setattr(figures, "FIGURE_MAX_PAGES", 2)
    (entry,) = render_figures(root, build, ["paper.pdf"])
    assert entry.pages == 5
    assert entry.images == [
        ".rewrite/build/figures/paper.png",
        ".rewrite/build/figures/paper-p2.png",
    ]
    assert all((root / image).read_bytes()[:8] == PNG_MAGIC for image in entry.images)
    assert [p.name for p in image_paths(build / "figures", "a/b.pdf", 3)] == [
        "b.png",
        "b-p2.png",
    ]


def test_corrupt_index_is_ignored(tmp_path: Path) -> None:
    root, build = paper(tmp_path)
    (build / "figures").mkdir()
    (build / "figures" / "index.json").write_text("{not json")
    (entry,) = render_figures(root, build, ["figures/plot.pdf"])
    assert entry.error is None
    assert json.loads((build / "figures" / "index.json").read_text())["figures"]
