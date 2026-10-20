"""Drafting from the spec (phase 7): POST /api/drafts, GET /api/drafts, GET /api/drafts/{id},
POST /api/drafts/{id}/apply."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import db
from app.api.runs import ERRORS, SameOrigin, _describe
from app.drafts import (
    Applied,
    ApplyRequest,
    Draft,
    DraftDetail,
    DraftError,
    DraftRequest,
    apply,
    get_detail,
    list_drafts,
)
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class DraftResponse(BaseModel):
    draft: Draft


class DraftsResponse(BaseModel):
    drafts: list[Draft]


_REQUEST = TypeAdapter(DraftRequest)
_APPLY = TypeAdapter(ApplyRequest)


@router.post("/drafts", response_model=DraftResponse, responses=ERRORS, dependencies=[SameOrigin])
async def post_draft(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> DraftResponse | JSONResponse:
    try:
        request = _REQUEST.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        draft = await state.drafts.start(repo, request)
    except DraftError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    return DraftResponse(draft=draft)


@router.get("/drafts", response_model=DraftsResponse)
def get_drafts(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    node_id: Annotated[str | None, Query()] = None,
) -> DraftsResponse:
    with db.connect(repo) as conn:
        return DraftsResponse(drafts=list_drafts(conn, limit, node_id))


@router.get("/drafts/{draft_id}", response_model=DraftDetail, responses=ERRORS)
async def get_draft_detail(draft_id: str, repo: RepoDep) -> DraftDetail | JSONResponse:
    def work() -> DraftDetail | None:
        with db.connect(repo) as conn:
            return get_detail(conn, draft_id)

    detail = await asyncio.to_thread(work)
    if detail is None:
        return JSONResponse(status_code=404, content={"detail": f"unknown draft {draft_id!r}"})
    return detail


@router.post(
    "/drafts/{draft_id}/apply", response_model=Applied, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_apply(
    draft_id: str, body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> Applied | JSONResponse:
    """Write the draft into the .tex (replace the paragraph under the node's marker, or insert
    it with a new marker line at file:line). Under the source lock; findings re-anchor."""
    try:
        request = _APPLY.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        async with state.source_lock(repo.root):
            applied = await asyncio.to_thread(apply, repo, draft_id, request)
    except DraftError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    try:
        await state.runs.reanchor_file(repo, applied.file)
    except Exception:  # best effort, as for PUT /api/file
        pass
    return applied
