"""Spec editor endpoints (phase-3 brief "API contract").

GET    /api/spec                      → {spec|null, raw, errors}   (never 500s on a bad file)
PUT    /api/spec            {spec}    → {spec, raw}                 422 {detail, errors}
PUT    /api/spec/raw        {raw}     → {spec, raw}                 422 {detail, errors}
POST   /api/spec/ops        {op}      → {spec, raw, new_id}         404 / 400 / 409 / 422
GET    /api/spec/status               → {nodes: {id: {status, file, line}}}
POST   /api/spec/nodes/{id}/reconcile → {id, status, file, line}
GET    /api/spec/skeleton             → {sections: [...], paragraphs: [...]}
GET    /api/spec/labels               → {labels: [{label, kind, file, line, caption}]}
GET    /api/spec/coverage             → {claims, sections, cells, unreferenced_claims,
                                         empty_sections}   (phase 5; read-only)
POST   /api/spec/adopt      {kind...} → {spec, raw, new_id, file, line}

Every write of the spec file goes through app.spec.write_spec, under AppState.spec_lock.
Adoption, which may insert a `% @p` marker in the .tex, also holds the repo's source lock
(taken after spec_lock), shared with PUT /api/file and git pull (audit 2026-10-18 item 5).
The input is always the human's edit in the spec editor; nothing here accepts model output.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app import anchors, coverage, skeleton
from app.anchors import NodeStatus, StatusValue
from app.api.ws import origin_allowed
from app.config import ConfigError
from app.events import Event, FileChanged
from app.files import NotTextError, PathEscapeError, ProtectedPathError, TooLargeError
from app.repo import Repo
from app.spec import (
    AddClaimOp,
    InsertOp,
    InvalidOpError,
    LiftToClaimOp,
    Op,
    PaperSpec,
    SpecError,
    SpecInvalidError,
    SpecOpError,
    apply_op,
    load_spec,
    parse_spec,
    parse_spec_data,
    require_spec,
    validate,
    write_spec,
)
from app.state import AppState, RepoDep, StateDep

log = logging.getLogger(__name__)


def same_origin(request: Request) -> None:
    """Refuse POSTs from foreign pages (a body without Content-Type is a simple request)."""
    if not origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


router = APIRouter(prefix="/api")
SameOrigin = Depends(same_origin)


# --------------------------------------------------------------------------- models


class SpecGet(BaseModel):
    spec: PaperSpec | None  # null when the file is missing or invalid
    raw: str  # the file text, "" when missing
    errors: list[SpecError]


class SpecResponse(BaseModel):
    spec: PaperSpec
    raw: str  # the file text as written


class SpecOpResponse(SpecResponse):
    new_id: str | None = None  # insert, lift_to_claim, add_claim (null for other ops)


class AdoptResponse(SpecOpResponse):
    file: str | None = None  # paragraph: where the marker is; section: the heading's file
    line: int | None = None  # 1-based line of the marker (or of the heading) after the write


class SpecErrorBody(BaseModel):
    detail: str
    errors: list[SpecError] | None = None


class SpecPut(BaseModel):
    spec: Any  # validated by app.spec so errors come back as SpecError paths


class RawPut(BaseModel):
    raw: str


class OpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Op


class AdoptSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["section"]
    file: str
    line: int  # the heading line
    says: str = ""


class AdoptParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["paragraph"]
    file: str
    line_start: int  # first line of the paragraph (any line of it is accepted)
    parent_id: str | None = None
    id: str | None = None  # an existing paragraph node: (re)insert its marker, set its anchor
    says: str | None = None


class AdoptRequest(BaseModel):
    request: Annotated[AdoptSection | AdoptParagraph, Field(discriminator="kind")]


class StatusResponse(BaseModel):
    nodes: dict[str, NodeStatus]


class ReconcileResponse(BaseModel):
    id: str
    status: StatusValue
    file: str | None = None
    line: int | None = None
    reread: str | None = None  # always null after a reconcile


class SkeletonSection(BaseModel):
    file: str
    line: int
    kind: Literal["section", "subsection"]
    heading: str
    label: str | None
    adopted_id: str | None


class SkeletonParagraph(BaseModel):
    file: str
    line_start: int
    line_end: int
    preview: str  # first 80 characters of the paragraph text
    section_heading: str | None
    marker_id: str | None


class SkeletonResponse(BaseModel):
    sections: list[SkeletonSection]
    paragraphs: list[SkeletonParagraph]


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": SpecErrorBody},
    404: {"model": SpecErrorBody},
    409: {"model": SpecErrorBody},
    422: {"model": SpecErrorBody},
}

_OP_REQUEST = TypeAdapter(OpRequest)
_ADOPT_REQUEST = TypeAdapter(AdoptRequest)


# --------------------------------------------------------------------------- helpers


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        loc = ".".join(str(part) for part in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    more = f" (and {exc.error_count() - 3} more)" if exc.error_count() > 3 else ""
    return "invalid request: " + "; ".join(parts) + more


def _error_response(exc: SpecOpError) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail}
    if exc.errors is not None:
        body["errors"] = [e.model_dump() for e in exc.errors]
    return JSONResponse(status_code=exc.status, content=body)


def _scan(repo: Repo) -> skeleton.DocScan:
    try:
        main = repo.main
    except ConfigError as exc:
        raise InvalidOpError(str(exc)) from exc
    return skeleton.scan_document(repo.root, main)


async def _run[T](
    state: AppState, work: Callable[[list[Event]], T], source_of: Repo | None = None
) -> T | JSONResponse:
    """Run blocking spec work in a thread under the spec lock (and, with `source_of`, that
    repo's source lock, for work that may write .tex files); publish its events after."""
    events: list[Event] = []
    try:
        async with contextlib.AsyncExitStack() as locks:
            await locks.enter_async_context(state.spec_lock)
            if source_of is not None:
                await locks.enter_async_context(state.source_lock(source_of.root))
            return await asyncio.to_thread(work, events)
    except SpecOpError as exc:
        return _error_response(exc)
    except skeleton.SourceChangedError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    except PathEscapeError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except ProtectedPathError as exc:
        return JSONResponse(status_code=403, content={"detail": str(exc)})
    except FileNotFoundError as exc:
        return JSONResponse(status_code=409, content={"detail": f"file not found: {exc}"})
    except (NotTextError, TooLargeError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    finally:
        for event in events:
            state.bus.publish(event)


# --------------------------------------------------------------------------- spec file


@router.get("/spec", response_model=SpecGet)
async def get_spec(repo: RepoDep) -> SpecGet:
    try:
        spec, raw, errors = await asyncio.to_thread(load_spec, repo)
    except Exception as exc:  # the editor must always get an answer it can show
        log.exception("loading the spec failed")
        error = SpecError(path="", message=f"cannot load the spec: {exc}")
        return SpecGet(spec=None, raw="", errors=[error])
    return SpecGet(spec=spec, raw=raw, errors=errors)


@router.put("/spec", response_model=SpecResponse, responses=ERROR_RESPONSES)
async def put_spec(body: SpecPut, repo: RepoDep, state: StateDep) -> SpecResponse | JSONResponse:
    def work(events: list[Event]) -> SpecResponse:
        spec, errors = parse_spec_data(body.spec)
        if spec is None:
            raise SpecInvalidError(errors)
        return SpecResponse(spec=spec, raw=write_spec(repo, spec, events.append))

    return await _run(state, work)


@router.put("/spec/raw", response_model=SpecResponse, responses=ERROR_RESPONSES)
async def put_spec_raw(body: RawPut, repo: RepoDep, state: StateDep) -> SpecResponse | JSONResponse:
    def work(events: list[Event]) -> SpecResponse:
        spec, errors = parse_spec(body.raw)
        if spec is None:
            raise SpecInvalidError(errors)
        return SpecResponse(spec=spec, raw=write_spec(repo, spec, events.append))

    return await _run(state, work)


@router.post(
    "/spec/ops",
    response_model=SpecOpResponse,
    responses=ERROR_RESPONSES,
    dependencies=[SameOrigin],
)
async def post_op(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> SpecOpResponse | JSONResponse:
    """Apply one op to the on-disk spec and write it. The body is `{op: {type, ...}}`."""
    try:
        op = _OP_REQUEST.validate_python(body).op
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})

    def work(events: list[Event]) -> SpecOpResponse:
        spec = require_spec(repo)
        reserved: set[str] = set()
        if isinstance(op, InsertOp | LiftToClaimOp | AddClaimOp):  # ops that make new ids
            try:
                scan: skeleton.DocScan | None = _scan(repo)
            except InvalidOpError:
                scan = None
            reserved = anchors.reserved_ids(repo, scan)
        new_spec, new_id = apply_op(spec, op, reserved)
        raw = write_spec(repo, new_spec, events.append)
        return SpecOpResponse(spec=new_spec, raw=raw, new_id=new_id)

    return await _run(state, work)


# --------------------------------------------------------------------------- status


@router.get("/spec/status", response_model=StatusResponse, responses=ERROR_RESPONSES)
async def get_status(repo: RepoDep, state: StateDep) -> StatusResponse | JSONResponse:
    def work(events: list[Event]) -> StatusResponse:
        spec = require_spec(repo)
        return StatusResponse(nodes=anchors.compute_status(repo, spec, _scan(repo)))

    return await _run(state, work)


@router.post(
    "/spec/nodes/{node_id}/reconcile",
    response_model=ReconcileResponse,
    responses=ERROR_RESPONSES,
    dependencies=[SameOrigin],
)
async def reconcile(
    node_id: str, repo: RepoDep, state: StateDep
) -> ReconcileResponse | JSONResponse:
    """ "I checked": re-baseline a stale paragraph, and clear the node's re-read mark
    (sections, paragraphs and claims)."""

    def work(events: list[Event]) -> ReconcileResponse:
        spec = require_spec(repo)
        status = anchors.reconcile(repo, spec, _scan(repo), node_id)
        return ReconcileResponse(
            id=node_id,
            status=status.status,
            file=status.file,
            line=status.line,
            reread=status.reread,
        )

    return await _run(state, work)


# --------------------------------------------------------------------------- coverage


@router.get("/spec/coverage", response_model=coverage.Coverage, responses=ERROR_RESPONSES)
async def get_coverage(repo: RepoDep, state: StateDep) -> coverage.Coverage | JSONResponse:
    """The claims x sections matrix: per claim and section, how many nodes of the section's
    subtree (the section and its paragraphs) link the claim. 409 for a broken spec file."""

    def work(events: list[Event]) -> coverage.Coverage:
        spec = require_spec(repo)
        scan = _scan(repo) if coverage.needs_scan(spec) else None
        return coverage.compute_coverage(spec, scan)

    return await _run(state, work)


# --------------------------------------------------------------------------- skeleton


@router.get("/spec/skeleton", response_model=SkeletonResponse, responses=ERROR_RESPONSES)
async def get_skeleton(repo: RepoDep, state: StateDep) -> SkeletonResponse | JSONResponse:
    """Headings and paragraphs of the include tree in document order, with adoption state.
    Works with a broken spec file too (then nothing shows as adopted)."""

    def work(events: list[Event]) -> SkeletonResponse:
        spec, _, _ = load_spec(repo)
        view = anchors.skeleton_view(spec, _scan(repo))
        return SkeletonResponse(
            sections=[
                SkeletonSection(
                    file=h.file,
                    line=h.line,
                    kind=h.kind,
                    heading=h.heading,
                    label=h.label,
                    adopted_id=adopted,
                )
                for h, adopted in view.sections
            ],
            paragraphs=[
                SkeletonParagraph(
                    file=p.file,
                    line_start=p.line_start,
                    line_end=p.line_end,
                    preview=p.preview,
                    section_heading=p.section_heading,
                    marker_id=p.marker_id,
                )
                for p in view.paragraphs
            ],
        )

    return await _run(state, work)


@router.post(
    "/spec/adopt",
    response_model=AdoptResponse,
    responses=ERROR_RESPONSES,
    dependencies=[SameOrigin],
)
async def adopt(
    body: Annotated[dict[str, Any], Body()], repo: RepoDep, state: StateDep
) -> AdoptResponse | JSONResponse:
    """Adopt a heading as a section node, or a paragraph as a paragraph node (inserting its
    `% @p <id>` marker in the .tex). Writes the spec; the frontend just takes the result."""
    try:
        request = _ADOPT_REQUEST.validate_python({"request": body}).request
    except ValidationError as exc:
        return JSONResponse(status_code=400, content={"detail": _describe(exc)})

    def work(events: list[Event]) -> AdoptResponse:
        spec = require_spec(repo)
        scan = _scan(repo)
        reserved = anchors.reserved_ids(repo, scan)
        if isinstance(request, AdoptSection):
            new_spec, new_id, heading = anchors.adopt_section(
                spec, scan, request.file, request.line, request.says, reserved
            )
            raw = write_spec(repo, new_spec, events.append)
            return AdoptResponse(
                spec=new_spec, raw=raw, new_id=new_id, file=heading.file, line=heading.line
            )

        plan = anchors.plan_paragraph_adoption(
            spec,
            scan,
            request.file,
            request.line_start,
            request.parent_id,
            request.id,
            request.says,
            reserved,
        )
        errors = validate(plan.spec)
        if errors:  # refuse before touching the .tex
            raise SpecInvalidError(errors)
        try:
            edit = skeleton.place_marker(repo.root, scan, plan.paragraph, plan.node_id)
        except skeleton.SourceChangedError as exc:
            for rel, mtime in exc.written:
                events.append(FileChanged(path=rel, change="modified", mtime=mtime))
            raise
        for rel, mtime in edit.written:
            events.append(FileChanged(path=rel, change="modified", mtime=mtime))
        raw = write_spec(repo, plan.spec, events.append, rebaseline={plan.node_id})
        return AdoptResponse(
            spec=plan.spec, raw=raw, new_id=plan.node_id, file=edit.file, line=edit.line
        )

    return await _run(state, work, source_of=repo)


class LabelOut(BaseModel):
    label: str
    kind: str
    file: str
    line: int
    caption: str | None


class LabelList(BaseModel):
    labels: list[LabelOut]


@router.get("/spec/labels", response_model=LabelList, responses=ERROR_RESPONSES)
def labels(repo: RepoDep) -> LabelList:
    r"""Every `\label{...}` in the include tree, in document order, with the kind of the
    environment it sits in and that environment's caption. The spec refers to figures,
    tables and equations by label (`fig:pipeline`), which survives renumbering."""
    scan = _scan(repo)
    return LabelList(
        labels=[
            LabelOut(label=i.label, kind=i.kind, file=i.file, line=i.line, caption=i.caption)
            for i in skeleton.scan_labels(scan)
        ]
    )
