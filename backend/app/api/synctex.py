"""GET /api/synctex/forward and GET /api/synctex/reverse (phase 2 API contract).

Coordinates are PDF points with the origin at the page's top-left corner; app/synctex.py
documents how they relate to what the synctex CLI prints.

Query parameters are parsed here rather than by FastAPI so that every bad argument is a 400
with a string `detail`, as the contract says (FastAPI's own validation answers 422).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import Config, ConfigError
from app.files import PathEscapeError, normalize_rel, resolve_in_repo
from app.repo import Repo
from app.state import RepoDep
from app.synctex import (
    NoSynctexData,
    SyncBox,
    SyncEdit,
    SynctexError,
    SynctexRun,
    SynctexTimeout,
    SynctexUnavailable,
    build_outputs,
    forward,
    reverse,
    select_runner,
)

router = APIRouter(prefix="/api/synctex")


class ForwardResult(BaseModel):
    boxes: list[SyncBox]


def _int_param(name: str, raw: str | None, *, minimum: int, default: int | None = None) -> int:
    if raw is None or not raw.strip():
        if default is None:
            raise HTTPException(status_code=400, detail=f"missing query parameter: {name}")
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{name} must be an integer, got {raw!r}"
        ) from exc
    if value < minimum:
        raise HTTPException(status_code=400, detail=f"{name} must be at least {minimum}")
    return value


def _float_param(name: str, raw: str | None) -> float:
    if raw is None or not raw.strip():
        raise HTTPException(status_code=400, detail=f"missing query parameter: {name}")
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{name} must be a number, got {raw!r}"
        ) from exc
    if not math.isfinite(value):
        raise HTTPException(status_code=400, detail=f"{name} must be a finite number")
    return max(value, 0.0)  # a click a hair outside the page edge is still on the page


def _prepare(repo: Repo) -> tuple[Config, SynctexRun]:
    """Config and a synctex runner, after checking a compile has produced SyncTeX data."""
    try:
        config = repo.load_config()
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _synctex_errors():
        build_outputs(repo.build_dir, config.paper.main)
        run = select_runner(repo.root, config.compiler)
    return config, run


@contextmanager
def _synctex_errors() -> Iterator[None]:
    try:
        yield
    except NoSynctexData as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SynctexUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SynctexTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except SynctexError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/forward", response_model=ForwardResult)
async def synctex_forward(
    repo: RepoDep,
    file: Annotated[str | None, Query(description="repo-relative source file")] = None,
    line: Annotated[str | None, Query(description="1-based line")] = None,
    col: Annotated[str | None, Query(description="0-based column; 0 = whole line")] = None,
) -> ForwardResult:
    """Source line → boxes on the PDF (top-left origin, PDF points). Empty when no match."""
    if file is None or not file.strip():
        raise HTTPException(status_code=400, detail="missing query parameter: file")
    try:
        resolve_in_repo(repo.root, file)
    except PathEscapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rel = normalize_rel(file)
    line_no = _int_param("line", line, minimum=1)
    col_no = _int_param("col", col, minimum=0, default=0)

    config, run = _prepare(repo)
    with _synctex_errors():
        boxes = await asyncio.to_thread(
            forward,
            repo.root,
            repo.build_dir,
            config.paper.main,
            rel,
            line_no,
            col_no,
            run=run,
            container_root=config.compiler.container_root,
        )
    return ForwardResult(boxes=boxes)


@router.get("/reverse", response_model=SyncEdit)
async def synctex_reverse(
    repo: RepoDep,
    page: Annotated[str | None, Query(description="1-based page")] = None,
    x: Annotated[str | None, Query(description="PDF points from the left edge")] = None,
    y: Annotated[str | None, Query(description="PDF points from the top edge")] = None,
) -> SyncEdit:
    """Point on a PDF page → repo-relative file, 1-based line, column (0 if unknown)."""
    page_no = _int_param("page", page, minimum=1)
    x_pt = _float_param("x", x)
    y_pt = _float_param("y", y)

    config, run = _prepare(repo)
    with _synctex_errors():
        found = await asyncio.to_thread(
            reverse,
            repo.root,
            repo.build_dir,
            config.paper.main,
            page_no,
            x_pt,
            y_pt,
            run=run,
            container_root=config.compiler.container_root,
        )
    if found is None:
        raise HTTPException(status_code=404, detail="no source location at that point")
    return found
