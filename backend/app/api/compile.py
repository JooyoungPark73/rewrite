"""POST /api/compile, GET /api/pdf and POST /api/compile/docker/pull."""

from __future__ import annotations

import asyncio
import shutil

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.ws import origin_allowed
from app.compiler import CompileResult, LogError
from app.config import ConfigError
from app.events import CompileFinished, CompileStarted
from app.files import PathEscapeError, resolve_in_repo
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class PullStarted(BaseModel):
    started: bool
    image: str = ""  # the image being pulled
    already_running: bool = False  # joined a pull of the same image that was in progress


@router.post("/compile", response_model=CompileResult)
async def compile_paper(repo: RepoDep, state: StateDep) -> CompileResult:
    """Compile the main file. Concurrent requests queue behind one lock."""
    try:
        config = repo.load_config()
        resolve_in_repo(repo.root, config.paper.main)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PathEscapeError as exc:
        raise HTTPException(status_code=400, detail=f"[paper] main: {exc}") from exc
    main = config.paper.main

    async with state.compile_lock:
        compiler = state.compiler_factory(config.compiler)
        state.bus.publish(CompileStarted(main=main))
        try:
            result = await asyncio.to_thread(compiler.compile, repo.root, main)
        except NotImplementedError as exc:
            message = f"compiler '{config.compiler.kind}' is not implemented yet ({exc})"
            result = CompileResult(
                ok=False,
                pdf=None,
                log=message,
                errors=[LogError(file=main, line=None, message=message, severity="error")],
                duration_ms=0,
            )
        state.bus.publish(CompileFinished(result=result))
    return result


@router.post("/compile/docker/pull", response_model=PullStarted)
async def pull_docker_image(request: Request, repo: RepoDep, state: StateDep) -> PullStarted:
    """Pull the TeX Live image for the configured year; returns at once.

    Progress arrives over the WebSocket as `docker.pull` events and one final
    `docker.pull.finished` event. Pulls never overlap (see AppState.start_pull).
    """
    # A body-less POST is a "simple" request a foreign page could send; refuse those.
    if not origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    try:
        config = repo.load_config()
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if shutil.which(state.docker_cli) is None:
        raise HTTPException(status_code=503, detail="docker was not found on PATH")
    image, already_running = state.start_pull(config.compiler.texlive_year)
    return PullStarted(started=True, image=image, already_running=already_running)


@router.get(
    "/pdf",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}}, 404: {"description": "not built"}},
)
def get_pdf(repo: RepoDep) -> FileResponse:
    try:
        pdf = repo.build_output(".pdf")
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not pdf.is_file():
        raise HTTPException(status_code=404, detail="no PDF yet; POST /api/compile first")
    return FileResponse(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})
