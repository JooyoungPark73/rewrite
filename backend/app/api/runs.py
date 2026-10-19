"""Pass runner endpoints (phase-4 brief "API contract").

GET  /api/passes                    → {passes: Pass[]}
GET  /api/runners                   → {runners, thinking_levels, defaults, global_instructions}
                                      (each runner with its login: `auth`, `warning`)
POST /api/runs/preview  {pass, scope, runner?, model?, thinking?}
                                    → {prompt, scope_name, cache_hit, model, thinking, command}
POST /api/runs          {pass, scope, runner?, model?, thinking?, force?}
                                    → {run}  (queued/running, or cached at once)  400 / 409
GET  /api/runs?limit=50             → {runs}  newest first
GET  /api/runs/{id}                 → {run, findings, rejected, raw_output}
GET  /api/runs/{id}/prompt          → {prompt, command}
POST /api/runs/{id}/cancel          → {run}
POST /api/runs/paste    {pass, scope, raw}
                                    → {run}  (runner "paste", validated like a live run)
POST /api/runs/{id}/triage {runner?, model?, thinking?}
                                    → {triage}  (phase 6)  400 no open findings, 404, 409 active
GET  /api/runs/{id}/triage          → {triage|null, findings}  (the latest triage)

Request bodies are validated by hand so malformed ones are 400 with a string `detail`, like
the spec editor's endpoints. Model output only ever becomes rows in state.db.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import db, runstore
from app.api.ws import origin_allowed
from app.config import ConfigError
from app.findings import Finding, Rejected
from app.prompts import PassInfo, load_passes
from app.repo import Repo
from app.runner.auth import AuthStatus
from app.runs import PasteRequest, Preview, RunError, RunRequest
from app.runstore import Run
from app.state import RepoDep, StateDep
from app.triage import Triage, TriageDetail, TriageError, TriageRequest

router = APIRouter(prefix="/api")


def same_origin(request: Request) -> None:
    """Refuse POSTs from foreign pages (a body-less POST is a CORS "simple" request)."""
    if not origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


SameOrigin = Depends(same_origin)


class ErrorBody(BaseModel):
    detail: str


ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
}


class PassesResponse(BaseModel):
    passes: list[PassInfo]


class RunnerInfo(BaseModel):
    name: str
    available: bool
    models: list[str]
    suppress_global: bool
    path: str | None = None  # + where the CLI was found
    error: str | None = None  # + why it is unavailable (not on PATH, not logged in)
    flags: dict[str, bool] = {}  # + which optional flag groups the installed CLI accepts
    missing_flags: list[str] = []  # + flags of the base command its --help does not list
    # + how the CLI is logged in (`claude auth status` / `codex login status`); None when
    # the CLI was not found. logged_in False makes the runner unavailable.
    auth: AuthStatus | None = None
    # + set when the login is not a claude.ai / ChatGPT subscription (e.g. an API key);
    # the runner stays available
    warning: str | None = None


class LevelDefault(BaseModel):
    runner: str
    model: str | None
    thinking: str


class GlobalInstructions(BaseModel):
    runner: str
    path: str
    present: bool


class ContextLeakInfo(BaseModel):
    runner: str
    path: str  # absolute path of an instruction file or folder outside the sandbox
    suppressed: bool  # the runner's argv keeps it out of a run


class RunnersResponse(BaseModel):
    runners: list[RunnerInfo]
    thinking_levels: list[str]
    defaults: dict[str, LevelDefault]
    global_instructions: list[GlobalInstructions]
    # + instruction files above .rewrite/sandbox/ each available CLI could discover
    # (audit 2026-10-18 item 7); see app/runner/context.py
    context_leaks: list[ContextLeakInfo] = []


class RunResponse(BaseModel):
    run: Run


class RunsResponse(BaseModel):
    runs: list[Run]


class RunDetail(BaseModel):
    run: Run
    findings: list[Finding]
    rejected: list[Rejected]
    raw_output: str | None
    stderr: str | None = None  # + the CLI's last stderr lines


class PromptResponse(BaseModel):
    prompt: str
    command: list[str]


class TriageResponse(BaseModel):
    triage: Triage


_RUN_REQUEST = TypeAdapter(RunRequest)
_PASTE_REQUEST = TypeAdapter(PasteRequest)
_TRIAGE_REQUEST = TypeAdapter(TriageRequest)


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        loc = ".".join(str(part) for part in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    more = f" (and {exc.error_count() - 3} more)" if exc.error_count() > 3 else ""
    return "invalid request: " + "; ".join(parts) + more


def _error(exc: RunError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def _parse[T](adapter: TypeAdapter[T], body: Any) -> T | JSONResponse:
    try:
        return adapter.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})


# --------------------------------------------------------------------------- passes, runners


@router.get("/passes", response_model=PassesResponse)
def get_passes(repo: RepoDep) -> PassesResponse:
    return PassesResponse(passes=[item.info for item in load_passes(repo)])


@router.get("/runners", response_model=RunnersResponse, responses=ERRORS)
def get_runners(repo: RepoDep, state: StateDep) -> RunnersResponse | JSONResponse:
    try:
        config = repo.load_config()
    except ConfigError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return RunnersResponse.model_validate(state.runs.runners_info(config, repo.sandbox_dir))


# --------------------------------------------------------------------------- runs


@router.post("/runs/preview", response_model=Preview, responses=ERRORS, dependencies=[SameOrigin])
async def preview_run(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> Preview | JSONResponse:
    request = _parse(_RUN_REQUEST, body)
    if isinstance(request, JSONResponse):
        return request
    try:
        return await state.runs.preview(repo, request)
    except RunError as exc:
        return _error(exc)


@router.post("/runs/paste", response_model=RunResponse, responses=ERRORS, dependencies=[SameOrigin])
async def paste_run(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> RunResponse | JSONResponse:
    request = _parse(_PASTE_REQUEST, body)
    if isinstance(request, JSONResponse):
        return request
    try:
        return RunResponse(run=await state.runs.paste(repo, request))
    except RunError as exc:
        return _error(exc)


@router.post("/runs", response_model=RunResponse, responses=ERRORS, dependencies=[SameOrigin])
async def start_run(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> RunResponse | JSONResponse:
    request = _parse(_RUN_REQUEST, body)
    if isinstance(request, JSONResponse):
        return request
    try:
        return RunResponse(run=await state.runs.start_run(repo, request))
    except RunError as exc:
        return _error(exc)


def _with_db[T](repo: Repo, empty: T, work: Any) -> T:
    if not db.db_path(repo).exists():
        return empty
    with db.connect(repo) as conn:
        return work(conn)  # type: ignore[no-any-return]


@router.get("/runs", response_model=RunsResponse)
def list_runs(repo: RepoDep, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> RunsResponse:
    runs = _with_db(repo, [], lambda conn: runstore.list_runs(conn, limit))
    return RunsResponse(runs=runs)


@router.get("/runs/{run_id}", response_model=RunDetail, responses=ERRORS)
def get_run(run_id: str, repo: RepoDep) -> RunDetail | JSONResponse:
    def work(conn: Any) -> RunDetail | None:
        run = runstore.get_run(conn, run_id)
        text = runstore.get_run_text(conn, run_id)
        if run is None or text is None:
            return None
        return RunDetail(
            run=run,
            findings=runstore.list_findings(conn, run_id=run_id),
            rejected=runstore.list_rejected(conn, run_id),
            raw_output=text.raw_output,
            stderr=text.stderr,
        )

    detail = _with_db(repo, None, work)
    if detail is None:
        return JSONResponse(status_code=404, content={"detail": f"unknown run {run_id!r}"})
    return detail


@router.get("/runs/{run_id}/prompt", response_model=PromptResponse, responses=ERRORS)
def get_run_prompt(run_id: str, repo: RepoDep) -> PromptResponse | JSONResponse:
    text = _with_db(repo, None, lambda conn: runstore.get_run_text(conn, run_id))
    if text is None:
        return JSONResponse(status_code=404, content={"detail": f"unknown run {run_id!r}"})
    return PromptResponse(prompt=text.prompt, command=text.command)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunResponse,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def cancel_run(run_id: str, repo: RepoDep, state: StateDep) -> RunResponse | JSONResponse:
    """Cancel a queued or running run (kills the CLI's process group). Cancelling a run that
    already ended changes nothing and returns it."""
    try:
        return RunResponse(run=await state.runs.cancel(repo, run_id))
    except RunError as exc:
        return _error(exc)


# --------------------------------------------------------------------------- triage (phase 6)


@router.post(
    "/runs/{run_id}/triage",
    response_model=TriageResponse,
    responses=ERRORS,
    dependencies=[SameOrigin],
)
async def start_triage(
    run_id: str,
    repo: RepoDep,
    state: StateDep,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> TriageResponse | JSONResponse:
    """Send the run's open findings and its scoped source to a fresh call that judges each
    finding keep / wrong / trivial. Never cached."""
    request = _parse(_TRIAGE_REQUEST, body or {})
    if isinstance(request, JSONResponse):
        return request
    try:
        return TriageResponse(triage=await state.triage.start(repo, run_id, request))
    except TriageError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.get("/runs/{run_id}/triage", response_model=TriageDetail, responses=ERRORS)
async def get_triage(run_id: str, repo: RepoDep, state: StateDep) -> TriageDetail | JSONResponse:
    try:
        return await state.triage.detail(repo, run_id)
    except TriageError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
