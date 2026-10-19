"""Git panel endpoints (phase-5 brief "API contract").

GET  /api/git/status                  → GitStatus (is_repo false, empty fields, outside git)
GET  /api/git/diff?path=&staged=false → {diff}   staged=false: working tree vs HEAD, plus
                                                  untracked files as full adds;
                                                  staged=true: index vs HEAD
GET  /api/git/log?limit=50            → {commits}
GET  /api/git/show?ref=&path=         → {content}   404 when the path is absent at the ref
POST /api/git/commit {message, paths?} → {sha, subject}   400 empty message / nothing to commit
POST /api/git/push, /api/git/pull     → {started: true}   409 while a git op runs
    output: WS `git.output {op, line}` ... then `git.finished {op, ok, error}`

Errors are `{detail: str}`: 400 bad input or a git failure, 404, 409 busy, 413/415 for a file
git shows that is too large or not text, 503 when git is not installed.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from app import gitops
from app.api.runs import ERRORS, SameOrigin, _describe
from app.gitops import Commit, CommitResult, Diff, GitError, GitStatus
from app.state import RepoDep, StateDep

router = APIRouter(prefix="/api")


class LogResponse(BaseModel):
    commits: list[Commit]


class ShowResponse(BaseModel):
    content: str


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    paths: list[str] | None = None  # default: every changed or untracked path


class StartedResponse(BaseModel):
    started: bool
    op: str  # + "push" or "pull"
    command: list[str]  # + the git argv that runs


_COMMIT = TypeAdapter(CommitRequest)


def git_error(exc: GitError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.get("/git/status", response_model=GitStatus, responses=ERRORS)
def get_status(repo: RepoDep) -> GitStatus | JSONResponse:
    try:
        return gitops.status(repo.root)
    except GitError as exc:
        return git_error(exc)


@router.get("/git/diff", response_model=Diff, responses=ERRORS)
def get_diff(
    repo: RepoDep, path: Annotated[str | None, Query()] = None, staged: bool = False
) -> Diff | JSONResponse:
    if gitops.locate(repo.root) is None:
        return Diff(diff="")
    try:
        return gitops.diff(repo.root, path or None, staged)
    except GitError as exc:
        return git_error(exc)


@router.get("/git/log", response_model=LogResponse, responses=ERRORS)
def get_log(
    repo: RepoDep, limit: Annotated[int, Query(ge=1, le=1000)] = 50
) -> LogResponse | JSONResponse:
    try:
        return LogResponse(commits=gitops.log_commits(repo.root, limit))
    except GitError as exc:
        return git_error(exc)


@router.get("/git/show", response_model=ShowResponse, responses=ERRORS)
def get_show(
    repo: RepoDep, ref: Annotated[str, Query()], path: Annotated[str, Query()]
) -> ShowResponse | JSONResponse:
    try:
        return ShowResponse(content=gitops.show(repo.root, ref, path))
    except GitError as exc:
        return git_error(exc)


@router.post(
    "/git/commit", response_model=CommitResult, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_commit(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> CommitResult | JSONResponse:
    try:
        request = _COMMIT.validate_python(body)
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})
    try:
        async with state.git.exclusive("commit"):
            return await asyncio.to_thread(gitops.commit, repo.root, request.message, request.paths)
    except GitError as exc:
        return git_error(exc)


async def _start(state: StateDep, repo: RepoDep, op: gitops.RemoteOp) -> Any:
    try:
        command = await state.git.start(repo, op)
    except GitError as exc:
        return git_error(exc)
    return StartedResponse(started=True, op=op, command=command)


@router.post(
    "/git/push", response_model=StartedResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_push(repo: RepoDep, state: StateDep) -> StartedResponse | JSONResponse:
    """`git push --follow-tags` in the background (snapshot tags go along); a branch with
    no upstream is pushed with `-u <origin> HEAD`."""
    return await _start(state, repo, "push")  # type: ignore[no-any-return]


@router.post(
    "/git/pull", response_model=StartedResponse, responses=ERRORS, dependencies=[SameOrigin]
)
async def post_pull(repo: RepoDep, state: StateDep) -> StartedResponse | JSONResponse:
    """`git pull` in the background; afterwards `file.changed` per changed path and
    `spec.changed` when the spec file changed."""
    return await _start(state, repo, "pull")  # type: ignore[no-any-return]
