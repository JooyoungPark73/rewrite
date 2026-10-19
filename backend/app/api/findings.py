"""Findings endpoints (phase-4 brief "API contract").

GET   /api/findings?file=&state=&run_id=  → {findings}  (every filter optional)
PATCH /api/findings/{id} {state}          → {finding}   400 bad state, 404 unknown id
POST  /api/findings/dismiss-flagged {run_id}
                                          → {updated}   (phase 6) the run's open findings
                                            whose triage verdict is wrong or trivial become
                                            dismissed; 400 bad body, 404 unknown run

A state change publishes `findings.updated {run_id, file}`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db, runstore
from app.api.runs import ERRORS, SameOrigin
from app.findings import FINDING_STATES, Finding
from app.runs import RunError
from app.state import RepoDep, StateDep
from app.triage import TriageError

router = APIRouter(prefix="/api")


class FindingsResponse(BaseModel):
    findings: list[Finding]


class FindingResponse(BaseModel):
    finding: Finding


class DismissedResponse(BaseModel):
    updated: int


@router.post(
    "/findings/dismiss-flagged",
    response_model=DismissedResponse,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def dismiss_flagged(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> DismissedResponse | JSONResponse:
    run_id = body.get("run_id")
    if set(body) != {"run_id"} or not isinstance(run_id, str) or not run_id:
        return JSONResponse(status_code=400, content={"detail": "body must be {run_id}"})
    try:
        return DismissedResponse(updated=await state.triage.dismiss_flagged(repo, run_id))
    except TriageError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.get("/findings", response_model=FindingsResponse)
def list_findings(
    repo: RepoDep,
    file: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
) -> FindingsResponse:
    if not db.db_path(repo).exists():
        return FindingsResponse(findings=[])
    with db.connect(repo) as conn:
        findings = runstore.list_findings(
            conn, file=file or None, state=state or None, run_id=run_id or None
        )
    return FindingsResponse(findings=findings)


@router.patch(
    "/findings/{finding_id}",
    response_model=FindingResponse,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def patch_finding(
    finding_id: str,
    body: Annotated[dict[str, Any], Body()],
    repo: RepoDep,
    state: StateDep,
) -> FindingResponse | JSONResponse:
    value = body.get("state")
    if set(body) != {"state"} or value not in FINDING_STATES:
        return JSONResponse(
            status_code=400,
            content={"detail": f"body must be {{state}} with state one of {FINDING_STATES}"},
        )
    try:
        finding = await state.runs.set_finding_state(repo, finding_id, value)
    except RunError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    return FindingResponse(finding=finding)
