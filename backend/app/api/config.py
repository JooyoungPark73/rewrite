"""GET /api/config and PUT /api/config: the compiler settings the UI may change.

Only `[compiler] kind` and `texlive_year` are writable here. The Docker image name is
derived from the year, never taken from a request, because it runs with the repo mounted.
A PUT rewrites `.rewrite/config.toml` through the config model, so every other section
(`[paper]`, `[runner.*]`, `[models]`, unknown keys) is kept but comments are dropped; a PUT
that changes nothing leaves the file untouched.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.compiler.docker import docker_available, docker_image, image_present
from app.config import (
    COMPILER_KINDS,
    TEXLIVE_YEAR_MAX,
    TEXLIVE_YEAR_MIN,
    CompilerKind,
    Config,
    ConfigError,
    save_config,
)
from app.repo import Repo
from app.state import RepoDep

router = APIRouter(prefix="/api")


class CompilerSettings(BaseModel):
    kind: CompilerKind
    texlive_year: int
    image: str  # texlive/texlive:TL<year>-historic (used when kind is docker)


class DockerStatus(BaseModel):
    available: bool  # docker CLI on PATH and its daemon answers
    image_present: bool  # the image for texlive_year is pulled


class ConfigView(BaseModel):
    main: str
    compiler: CompilerSettings
    docker: DockerStatus


class CompilerUpdate(BaseModel):
    kind: str | None = None  # native | docker
    texlive_year: int | None = None  # 2018..2030


class ConfigUpdate(BaseModel):
    compiler: CompilerUpdate = Field(default_factory=CompilerUpdate)


def _load(repo: Repo) -> Config:
    try:
        return repo.load_config()
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def config_view(config: Config) -> ConfigView:
    year = config.compiler.texlive_year
    available = docker_available()
    return ConfigView(
        main=config.paper.main,
        compiler=CompilerSettings(
            kind=config.compiler.kind, texlive_year=year, image=docker_image(year)
        ),
        docker=DockerStatus(available=available, image_present=available and image_present(year)),
    )


# Plain `def` handlers: the docker probes are blocking subprocess calls, so FastAPI runs
# these in its thread pool.


@router.get("/config", response_model=ConfigView)
def get_config(repo: RepoDep) -> ConfigView:
    return config_view(_load(repo))


@router.put("/config", response_model=ConfigView)
def put_config(body: ConfigUpdate, repo: RepoDep) -> ConfigView:
    config = _load(repo)
    update = body.compiler
    if update.kind is not None and update.kind not in COMPILER_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"compiler.kind must be one of {', '.join(COMPILER_KINDS)}; got {update.kind!r}",
        )
    year = update.texlive_year
    if year is not None and not TEXLIVE_YEAR_MIN <= year <= TEXLIVE_YEAR_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                f"compiler.texlive_year must be between {TEXLIVE_YEAR_MIN} and "
                f"{TEXLIVE_YEAR_MAX}; got {year}"
            ),
        )

    changed = False
    if update.kind is not None and update.kind != config.compiler.kind:
        config.compiler.kind = cast(CompilerKind, update.kind)
        changed = True
    if year is not None and year != config.compiler.texlive_year:
        config.compiler.texlive_year = year
        changed = True
    if changed:
        try:
            save_config(repo.config_path, config)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not write config: {exc}") from exc
    return config_view(config)
