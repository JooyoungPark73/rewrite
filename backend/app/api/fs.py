"""GET /api/fs/dirs: the folders under a directory, so the Open dialog can browse to a paper repo.

Read-only and names only (no file contents); the loopback binding is the boundary, as for
every other endpoint. Hidden folders (dotfiles) are left out. Each entry says whether the
folder already has `.rewrite/` (initialised) and whether it holds `.tex` files, so a paper
repo is recognisable without opening it.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/fs")


class DirEntry(BaseModel):
    name: str
    path: str
    # `.rewrite/` exists: `POST /api/repo/open` would accept it.
    initialized: bool
    # At least one `.tex` file directly inside: probably a LaTeX project.
    has_tex: bool
    is_git: bool


class DirListing(BaseModel):
    # The listed directory, symlinks resolved.
    path: str
    # Its parent, or null at the filesystem root.
    parent: str | None
    # The user's home directory, the browser's starting point.
    home: str
    dirs: list[DirEntry]


def _has_tex(directory: Path) -> bool:
    try:
        with os.scandir(directory) as it:
            return any(e.name.endswith(".tex") and e.is_file() for e in it)
    except OSError:
        return False


def _entry(child: os.DirEntry[str]) -> DirEntry | None:
    if child.name.startswith("."):
        return None
    try:
        if not child.is_dir():  # follows symlinks, so a linked folder counts
            return None
    except OSError:
        return None
    path = Path(child.path)
    return DirEntry(
        name=child.name,
        path=str(path),
        initialized=(path / ".rewrite").is_dir(),
        has_tex=_has_tex(path),
        is_git=(path / ".git").exists(),
    )


def list_directory(raw: str | None) -> DirListing:
    """The listing for `raw` (absolute, `~` allowed; empty or None = home)."""
    home = Path.home()
    if raw is None or not raw.strip():
        target = home
    else:
        target = Path(raw.strip()).expanduser()
        if not target.is_absolute():
            raise HTTPException(status_code=400, detail=f"path must be absolute: {raw}")
    try:
        target = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no such directory: {raw}") from exc
    except (NotADirectoryError, PermissionError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot resolve {raw}: {exc}") from exc
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"not a directory: {raw}")
    try:
        with os.scandir(target) as it:
            children = list(it)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"not readable: {target}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"cannot list {target}: {exc}") from exc
    entries = [e for e in (_entry(c) for c in children) if e is not None]
    entries.sort(key=lambda e: e.name.lower())
    parent = target.parent
    return DirListing(
        path=str(target),
        parent=str(parent) if parent != target else None,
        home=str(home),
        dirs=entries,
    )


@router.get("/dirs", response_model=DirListing)
def dirs(path: str | None = Query(default=None)) -> DirListing:
    return list_directory(path)
