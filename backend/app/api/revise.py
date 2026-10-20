"""Revise (phase 8): author comments as findings, and rewrite proposals on findings.

POST /api/findings/comment {file, line_start, line_end, quote, problem, why?, json_ref?, severity?}
                                        → {finding}
POST /api/revisions {finding_id, span?, runner?, model?, thinking?} → {revision}
GET  /api/revisions?finding_id=&limit=  → {revisions}
GET  /api/revisions/outstanding          → {count, by_file}   (\\aigen{...} wrappers in the paper)
GET  /api/revisions/{id}                 → {revision, run, finding}
POST /api/revisions/{id}/apply {text?}   → {revision, file, line_start, line_end, outstanding}
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import db
from app.api.runs import ERRORS, SameOrigin, _describe
from app.events import FindingsUpdated
from app.findings import Finding
from app.revise import (
    Applied,
    ApplyRequest,
    CommentRequest,
    Outstanding,
    ReviseError,
    Revision,
    RevisionDetail,
    RevisionRequest,
    apply,
    count_outstanding,
    get_detail,
    list_revisions,
)
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class FindingResponse(BaseModel):
    finding: Finding


class RevisionResponse(BaseModel):
    revision: Revision


class RevisionsResponse(BaseModel):
    revisions: list[Revision]


_COMMENT = TypeAdapter(CommentRequest)
_REQUEST = TypeAdapter(RevisionRequest)
_APPLY = TypeAdapter(ApplyRequest)


@router.post(
    "/findings/comment", response_model=FindingResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_comment(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> FindingResponse | JSONResponse:
    try:
        request = _COMMENT.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        finding = await state.revise.comment(repo, request)
    except ReviseError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    return FindingResponse(finding=finding)


@router.post(
    "/revisions", response_model=RevisionResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_revision(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> RevisionResponse | JSONResponse:
    try:
        request = _REQUEST.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        revision = await state.revise.start(repo, request)
    except ReviseError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    return RevisionResponse(revision=revision)


@router.get("/revisions", response_model=RevisionsResponse)
def get_revisions(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    finding_id: Annotated[str | None, Query()] = None,
) -> RevisionsResponse:
    with db.connect(repo) as conn:
        return RevisionsResponse(revisions=list_revisions(conn, limit, finding_id))


@router.get("/revisions/outstanding", response_model=Outstanding)
async def get_outstanding(repo: RepoDep) -> Outstanding:
    return await asyncio.to_thread(count_outstanding, repo)


@router.get("/revisions/{revision_id}", response_model=RevisionDetail, responses=ERRORS)
async def get_revision_detail(revision_id: str, repo: RepoDep) -> RevisionDetail | JSONResponse:
    def work() -> RevisionDetail | None:
        with db.connect(repo) as conn:
            return get_detail(conn, revision_id)

    detail = await asyncio.to_thread(work)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown revision {revision_id!r}"}
        )
    return detail


@router.post(
    "/revisions/{revision_id}/apply",
    response_model=Applied,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def post_apply(
    revision_id: str, body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> Applied | JSONResponse:
    try:
        request = _APPLY.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        async with state.source_lock(repo.root):
            applied = await asyncio.to_thread(apply, repo, revision_id, request)
    except ReviseError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    try:
        await state.runs.reanchor_file(repo, applied.file)
    except Exception:  # best effort, as for PUT /api/file
        pass
    # The finding is addressed now: refresh every list that shows it.
    state.bus.publish(FindingsUpdated(run_id=None, file=applied.file))
    return applied
