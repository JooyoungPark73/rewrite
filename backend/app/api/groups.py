"""Run group endpoints (phase-6 brief "API contract").

POST /api/groups/both   {pass, scope, claude?: {model?, thinking?}, codex?: {model?, thinking?},
                         force?}                        → {group}
     400: malformed body, unknown/unrunnable pass, unresolvable scope, bad thinking, either
          runner unavailable; 409: a member's pass and scope already has an active run
POST /api/groups/fanout {pass, runner?, model?, thinking?, force?}
                                                        → {group}  (scope: the whole paper)
     400: as above, or no section is linked; 409 as above
GET  /api/groups?limit=50                               → {groups}  newest first
GET  /api/groups/{id}                                   → {group, runs, merged}
     `merged`: "both" → findings merged by agreement (agreements first); "fanout" → every
     finding as its own entry (agreement 1)
POST /api/groups/{id}/cancel                            → {group}  (cancels every active member)

Members are ordinary runs: their prompts, findings, cost and cancel are the run endpoints'.
WS: `group.updated {group}` on every member change.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter

from app import db, groups
from app.api.runs import ERRORS, SameOrigin, _parse
from app.groups import BothRequest, FanoutRequest, GroupDetail, GroupError, RunGroup
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class GroupResponse(BaseModel):
    group: RunGroup


class GroupsResponse(BaseModel):
    groups: list[RunGroup]


_BOTH = TypeAdapter(BothRequest)
_FANOUT = TypeAdapter(FanoutRequest)


def _error(exc: GroupError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.post(
    "/groups/both", response_model=GroupResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_both(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> GroupResponse | JSONResponse:
    request = _parse(_BOTH, body)
    if isinstance(request, JSONResponse):
        return request
    try:
        return GroupResponse(group=await state.groups.start_both(repo, request))
    except GroupError as exc:
        return _error(exc)


@router.post(
    "/groups/fanout", response_model=GroupResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_fanout(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> GroupResponse | JSONResponse:
    request = _parse(_FANOUT, body)
    if isinstance(request, JSONResponse):
        return request
    try:
        return GroupResponse(group=await state.groups.start_fanout(repo, request))
    except GroupError as exc:
        return _error(exc)


@router.get("/groups", response_model=GroupsResponse)
async def list_groups(
    repo: RepoDep, limit: Annotated[int, Query(ge=1, le=1000)] = 50
) -> GroupsResponse:
    def work() -> list[RunGroup]:
        if not db.db_path(repo).exists():
            return []
        order = groups.file_order(repo)
        with db.connect(repo) as conn:
            return groups.list_groups(conn, order, limit)

    return GroupsResponse(groups=await asyncio.to_thread(work))


@router.get("/groups/{group_id}", response_model=GroupDetail, responses=ERRORS)
async def get_group(group_id: str, repo: RepoDep) -> GroupDetail | JSONResponse:
    def work() -> GroupDetail | None:
        if not db.db_path(repo).exists():
            return None
        order = groups.file_order(repo)
        with db.connect(repo) as conn:
            return groups.get_detail(conn, group_id, order)

    detail = await asyncio.to_thread(work)
    if detail is None:
        return JSONResponse(status_code=404, content={"detail": f"unknown group {group_id!r}"})
    return detail


@router.post(
    "/groups/{group_id}/cancel",
    response_model=GroupResponse,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def cancel_group(
    group_id: str, repo: RepoDep, state: StateDep
) -> GroupResponse | JSONResponse:
    """Cancel every queued or running member (their CLI process groups are killed). A group
    whose members all ended is returned unchanged."""
    try:
        return GroupResponse(group=await state.groups.cancel(repo, group_id))
    except GroupError as exc:
        return _error(exc)
