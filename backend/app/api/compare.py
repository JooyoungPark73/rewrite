"""Blind A/B comparison endpoints (phase-5 brief "API contract").

POST /api/compare {file, node_id?, line_start?, line_end?, before: {kind, ref}, runner?,
                   model?, thinking?}       → {comparison}  (queued or running)
     400: neither node_id nor a line range, the paragraph cannot be found at the ref or in
          the working tree, an unknown ref/snapshot, identical versions, an unavailable runner
     409: the spec file is missing or invalid
GET  /api/compare?limit=50                  → {comparisons}  (mapping always null)
GET  /api/compare/{id}?reveal=false         → {comparison, before_text, after_text, run}
     `mapping` is non-null only with reveal=true once the comparison is done.

The model call is a Run with pass `ab-compare` (scope kind `selection`): its prompt, cost and
cancel are the run endpoints'. WS: `compare.finished {comparison}` (mapping null).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import compare, db
from app.api.runs import ERRORS, SameOrigin, _describe
from app.compare import CompareDetail, CompareError, CompareRequest, Comparison
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class ComparisonResponse(BaseModel):
    comparison: Comparison


class ComparisonsResponse(BaseModel):
    comparisons: list[Comparison]


_REQUEST = TypeAdapter(CompareRequest)


@router.post(
    "/compare", response_model=ComparisonResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_compare(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> ComparisonResponse | JSONResponse:
    try:
        request = _REQUEST.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        comparison = await state.compare.start(repo, request)
    except CompareError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    return ComparisonResponse(comparison=comparison)


@router.get("/compare", response_model=ComparisonsResponse)
def list_compare(
    repo: RepoDep, limit: Annotated[int, Query(ge=1, le=1000)] = 50
) -> ComparisonsResponse:
    if not db.db_path(repo).exists():
        return ComparisonsResponse(comparisons=[])
    with db.connect(repo) as conn:
        return ComparisonsResponse(comparisons=compare.list_comparisons(conn, limit))


@router.get("/compare/{comparison_id}", response_model=CompareDetail, responses=ERRORS)
async def get_compare(
    comparison_id: str, repo: RepoDep, reveal: bool = False
) -> CompareDetail | JSONResponse:
    def work() -> CompareDetail | None:
        if not db.db_path(repo).exists():
            return None
        with db.connect(repo) as conn:
            return compare.get_detail(conn, comparison_id, reveal)

    detail = await asyncio.to_thread(work)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown comparison {comparison_id!r}"}
        )
    return detail
