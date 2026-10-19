"""GET /api/health, GET /api/repo, POST /api/repo/open, POST /api/repo/init."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import __version__
from app.compiler.docker import image_present
from app.config import ConfigError
from app.repo import Repo, init_repo
from app.state import AppState, RepoDep, StateDep

router = APIRouter(prefix="/api")

TOOLS = ("latexmk", "docker", "claude", "codex", "git")


class Tools(BaseModel):
    latexmk: bool
    docker: bool
    claude: bool
    codex: bool
    git: bool


class Health(BaseModel):
    ok: bool
    version: str
    tools: Tools
    repo: str | None
    # Whether the configured TeX Live Docker image is pulled; null without docker or a repo.
    docker_image_present: bool | None = None


class RepoInfo(BaseModel):
    path: str
    main: str
    initialized: bool


class RepoPath(BaseModel):
    path: str


def detect_tools() -> Tools:
    return Tools(**{name: shutil.which(name) is not None for name in TOOLS})


def repo_info(repo: Repo) -> RepoInfo:
    try:
        main = repo.main
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RepoInfo(path=str(repo.root), main=main, initialized=repo.is_initialized)


def _directory(raw: str) -> Repo:
    if not raw.strip():
        raise HTTPException(status_code=400, detail="path is empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"path must be absolute: {raw}")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {raw}")
    return Repo.at(path)


def _docker_image_present(state: AppState, tools: Tools) -> bool | None:
    if not tools.docker or state.repo is None:
        return None
    try:
        year = state.repo.load_config().compiler.texlive_year
    except ConfigError:
        return None
    return image_present(year)


@router.get("/health", response_model=Health)
def health(state: StateDep) -> Health:
    tools = detect_tools()
    return Health(
        ok=True,
        version=__version__,
        tools=tools,
        repo=str(state.repo.root) if state.repo else None,
        docker_image_present=_docker_image_present(state, tools),
    )


@router.get("/repo", response_model=RepoInfo)
def get_repo(repo: RepoDep) -> RepoInfo:
    return repo_info(repo)


@router.post("/repo/open", response_model=RepoInfo)
async def open_repo(body: RepoPath, state: StateDep) -> RepoInfo:
    repo = _directory(body.path)
    if not repo.is_initialized:
        raise HTTPException(
            status_code=400,
            detail=f"{repo.root} has no .rewrite/; run `rewrite init` or POST /api/repo/init",
        )
    info = repo_info(repo)
    await state.open_repo(repo)
    return info


@router.post("/repo/init", response_model=RepoInfo)
async def init(body: RepoPath, state: StateDep) -> RepoInfo:
    repo = _directory(body.path)
    try:
        init_repo(repo.root)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"init failed: {exc}") from exc
    info = repo_info(repo)
    await state.open_repo(repo)
    return info
