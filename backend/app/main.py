"""FastAPI application factory.

Development: `cd backend && uv run uvicorn app.main:app --reload` (repo from
$REWRITE_REPO, or opened from the UI). Normal use: `rewrite serve --repo <path>`,
which always binds 127.0.0.1.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import routers
from app.repo import Repo
from app.state import AppState

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
# Host headers accepted. Requests for any other host name (e.g. a DNS-rebinding page
# that resolves its own domain to 127.0.0.1) get a 400.
TRUSTED_HOSTS = ("127.0.0.1", "localhost")
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class RootInfo(BaseModel):
    name: str
    version: str
    message: str
    health: str


def repo_from_env() -> Path | None:
    """$REWRITE_REPO, if set and initialised."""
    value = os.environ.get("REWRITE_REPO", "").strip()
    if not value:
        return None
    repo = Repo.at(value)
    if not repo.is_initialized:
        log.warning("REWRITE_REPO=%s has no .rewrite/; starting without a repo", value)
        return None
    return repo.root


def _watch_default() -> bool:
    return os.environ.get("REWRITE_NO_WATCH", "").strip() not in ("1", "true", "yes")


def create_app(
    repo: Path | None = None,
    *,
    watch: bool | None = None,
    frontend_dist: Path | None = FRONTEND_DIST,
    trusted_hosts: Sequence[str] = TRUSTED_HOSTS,
) -> FastAPI:
    """Build the app for one paper repo (or none, until POST /api/repo/open)."""
    opened: Repo | None = None
    if repo is not None:
        opened = Repo.at(repo)
        if not opened.is_initialized:
            raise ValueError(f"{opened.root} has no .rewrite/; run `rewrite init` first")
    state = AppState(opened, watch=_watch_default() if watch is None else watch)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.start()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="rewrite", version=__version__, lifespan=lifespan)
    app.state.pb = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(trusted_hosts))
    for router in routers:
        app.include_router(router)

    if frontend_dist is not None and (frontend_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    else:

        @app.get("/", response_model=RootInfo)
        def root() -> RootInfo:
            return RootInfo(
                name="rewrite API",
                version=__version__,
                message=(
                    "The frontend is not built. Run `cd frontend && npm run build` and "
                    "restart, or use the Vite dev server (`cd frontend && npm run dev`)."
                ),
                health="/api/health",
            )

    return app


app = create_app(repo_from_env())
