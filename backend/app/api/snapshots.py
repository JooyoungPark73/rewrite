"""Revision snapshots ("flag major revision"; phase-5 brief "API contract").

GET  /api/snapshots         → {snapshots: Snapshot[]}  newest revision first (empty outside git)
POST /api/snapshots {note}  → {snapshot} (+ commit: the sha committed first when the tree was
                              dirty, else null)   400 empty note / not a git repo, 409 busy

A snapshot is the annotated tag `rewrite/rev-<n>-<slug>` on HEAD (app/snapshots.py).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app import snapshots
from app.api.git import git_error
from app.api.runs import ERRORS, SameOrigin, _describe
from app.gitops import GitError
from app.snapshots import Snapshot, SnapshotCreated
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class SnapshotsResponse(BaseModel):
    snapshots: list[Snapshot]


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str


_REQUEST = TypeAdapter(SnapshotRequest)


@router.get("/snapshots", response_model=SnapshotsResponse, responses=ERRORS)
def get_snapshots(repo: RepoDep) -> SnapshotsResponse | JSONResponse:
    try:
        return SnapshotsResponse(snapshots=snapshots.list_snapshots(repo))
    except GitError as exc:
        return git_error(exc)


@router.post(
    "/snapshots", response_model=SnapshotCreated, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_snapshot(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> SnapshotCreated | JSONResponse:
    try:
        request = _REQUEST.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        async with state.git.exclusive("snapshot"):
            return await asyncio.to_thread(snapshots.create_snapshot, repo, request.note)
    except GitError as exc:
        return git_error(exc)
