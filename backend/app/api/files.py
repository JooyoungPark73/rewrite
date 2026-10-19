"""GET /api/files, GET /api/file, PUT /api/file.

Saves hold the paper repo's source lock (AppState.source_lock), which marker insertion and
git pull hold too, so a save never lands in the middle of another source edit. A save may
name the revision it was made from (`base_sha256`, the `sha256` GET or PUT returned); if the
file is no longer that revision the save is refused with 409 instead of overwriting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import ConfigError
from app.files import (
    FileConflictError,
    NewFile,
    NotTextError,
    PathEscapeError,
    ProtectedPathError,
    TexFile,
    TooLargeError,
    content_sha256,
    create_file,
    normalize_rel,
    read_text_file,
    resolve_tex_tree,
    write_text_file,
)
from app.state import RepoDep, StateDep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class FileList(BaseModel):
    main: str
    files: list[TexFile]
    missing: list[TexFile] = []  # referenced by \input etc. but not found on disk


class FileContent(BaseModel):
    path: str
    content: str
    mtime: float  # seconds since the epoch
    sha256: str = ""  # + the revision: sha256 of the UTF-8 content (send as base_sha256)


class FileWrite(BaseModel):
    path: str
    content: str
    # + the revision `content` was edited from; the save is 409 if the file changed since
    base_sha256: str | None = None


class FileSaved(BaseModel):
    path: str
    mtime: float
    sha256: str = ""  # + the revision now on disk


class FileCreate(BaseModel):
    path: str
    # The file that should `\input` the new .tex file; default: the main file.
    include_in: str | None = None


class FileCreated(NewFile):
    files: FileList  # the tree after the change


@contextmanager
def _http_errors(path: str) -> Iterator[None]:
    try:
        yield
    except PathEscapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProtectedPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"file not found: {path}") from exc
    except TooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except NotTextError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except FileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (IsADirectoryError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=f"not a file: {path}") from exc


@router.get("/files", response_model=FileList)
def list_files(repo: RepoDep) -> FileList:
    try:
        main = repo.main
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tree = resolve_tex_tree(repo.root, main)
    return FileList(main=tree.main, files=tree.files, missing=tree.missing)


@router.get("/file", response_model=FileContent)
def get_file(path: Annotated[str, Query()], repo: RepoDep) -> FileContent:
    with _http_errors(path):
        content, mtime = read_text_file(repo.root, path)
    return FileContent(
        path=normalize_rel(path), content=content, mtime=mtime, sha256=content_sha256(content)
    )


@router.put("/file", response_model=FileSaved)
async def put_file(body: FileWrite, repo: RepoDep, state: StateDep) -> FileSaved:
    """Save a file (under the repo's source lock; 409 when `base_sha256` is given and the
    file is no longer that revision), then re-anchor the open findings in it (phase 4);
    the watcher's `file.changed` does the same, so findings follow edits made outside the
    app too."""
    async with state.source_lock(repo.root):
        with _http_errors(body.path):
            mtime = await asyncio.to_thread(
                write_text_file, repo.root, body.path, body.content, base_sha256=body.base_sha256
            )
    path = normalize_rel(body.path)
    try:
        await state.runs.reanchor_file(repo, path)
    except Exception:  # the save succeeded; re-anchoring is best-effort
        log.exception("re-anchoring findings in %s failed", path)
    return FileSaved(path=path, mtime=mtime, sha256=content_sha256(body.content))


@router.post("/files/new", response_model=FileCreated)
async def new_file(body: FileCreate, repo: RepoDep, state: StateDep) -> FileCreated:
    r"""Create an empty file (and its folders) under the repo. A `.tex` file also gets an
    `\input{<path>}` line in `include_in` (default: the main file) before its
    `\end{document}`, so it appears in the file tree. 409 if the path exists; the path
    rules of PUT /api/file apply (400 escape, 403 protected)."""
    try:
        main = repo.main
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with state.source_lock(repo.root):
        with _http_errors(body.include_in or main):
            created = await asyncio.to_thread(
                create_file, repo.root, body.path, main=main, include_in=body.include_in
            )
    with _http_errors(body.path):
        tree = resolve_tex_tree(repo.root, main)
    return FileCreated(
        path=created.path,
        included_by=created.included_by,
        files=FileList(main=tree.main, files=tree.files, missing=tree.missing),
    )
