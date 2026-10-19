"""Run groups (phase-6 brief "Decisions fixed": run groups, merge rule, fan-out).

A group owns several ordinary review runs (app/runs.py), so cache, cancel, cost, prompt
preview and the concurrency cap of 3 work for each member exactly as for a single run.

- "Run both" (`kind: both`): the same pass and scope on claude and on codex. The two plans
  carry the runner as their `scope_variant`, so they may run side by side while a plain run
  of that pass and scope still excludes both. Their findings are merged by agreement
  (app/merge.py).
- "Fan out" (`kind: fanout`, scope always the paper): one run of the pass per linked
  section (scope `section`, in document order), each prompt prefixed with the fan-out note
  (app/prompts.py FANOUT_NOTE), plus one `message-drift` run over scope `ends` (abstract,
  first and last section). Every member uses the same runner; a model or thinking level
  given in the request applies to all members, otherwise each member takes its pass's
  level defaults. Members queue behind the cap of 3 concurrent runs.

Every request is planned in full before anything starts (a bad pass or scope is 400, an
unavailable runner 400, a member scope that already has an active run 409), then the
members are launched; `force` skips the cache for each, and a cache hit counts as done.

Group status follows the members: `queued` while every member is queued, `running` while
any member is active, then `done` (every member done or cached), `partial` (some failed or
cancelled), `failed` (none succeeded) or `cancelled` (the group was cancelled, or every
member was). Status changes come from the members' run events (and each member's
completion hook, so none is missed) and are published as `group.updated`.

Model output only becomes rows in state.db here; the spec file is only read (prompts).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app import db, runstore
from app.config import RUNNER_NAMES, Config, ConfigError
from app.events import EventBus, GroupUpdated
from app.files import resolve_tex_tree
from app.merge import MergedFinding, RunFindings, agreements_count, merge_findings, singletons
from app.prompts import (
    PromptError,
    Scope,
    assemble_prompt,
    get_pass,
    linked_sections,
    load_spec_for_prompt,
)
from app.repo import Repo
from app.runs import RunError, RunManager, RunPlan
from app.runstore import ACTIVE_STATUSES, Run

log = logging.getLogger(__name__)

GroupKind = Literal["both", "fanout"]
GroupStatus = Literal["queued", "running", "done", "partial", "failed", "cancelled"]
TERMINAL: tuple[str, ...] = ("done", "partial", "failed", "cancelled")
SUCCEEDED: tuple[str, ...] = ("done", "cached")
DRIFT_PASS = "message-drift"  # the fan-out's run over the abstract, first and last section
RUN_EVENTS = ("run.started", "run.finished", "run.failed")


# --------------------------------------------------------------------------- API models


class RunnerOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None  # null: front matter / level default; "": the CLI's default
    thinking: str | None = None


class BothRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pass_: str = Field(alias="pass")
    scope: Scope
    claude: RunnerOptions | None = None
    codex: RunnerOptions | None = None
    force: bool = False


class FanoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pass_: str = Field(alias="pass")
    runner: str | None = None  # default: the pass level's runner, else [runner] default
    model: str | None = None  # for every member; null: each member's level default
    thinking: str | None = None
    force: bool = False


class RunGroup(BaseModel):
    """A group as the API returns it (phase-6 brief "API contract")."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    kind: GroupKind
    pass_: str = Field(alias="pass")
    scope: Scope
    run_ids: list[str]
    status: GroupStatus
    created_at: float
    finished_at: float | None
    cost_usd: float | None  # the members' costs summed; null when no member reports one
    findings_count: int
    agreements_count: int  # merged entries reported by more than one run (fan-out: 0)


class GroupDetail(BaseModel):
    group: RunGroup
    runs: list[Run]
    merged: list[MergedFinding]


class GroupError(Exception):
    """A group request the API refuses; `status` is the HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- status


def derive_status(stored: str, statuses: list[str]) -> GroupStatus:
    """The group status from its stored status (only `cancelled` sticks) and its members'."""
    if stored == "cancelled":
        return "cancelled"
    if not statuses:
        return "failed"
    if any(status in ACTIVE_STATUSES for status in statuses):
        return "queued" if all(status == "queued" for status in statuses) else "running"
    succeeded = sum(1 for status in statuses if status in SUCCEEDED)
    if succeeded == len(statuses):
        return "done"
    if succeeded:
        return "partial"
    return "cancelled" if all(status == "cancelled" for status in statuses) else "failed"


# --------------------------------------------------------------------------- rows


def new_id() -> str:
    return f"grp{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


def _row(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row | None:
    return _cursor(conn).execute("SELECT * FROM run_groups WHERE id = ?", (group_id,)).fetchone()


def file_order(repo: Repo) -> list[str]:
    """The paper's files in include order (empty when the main file cannot be resolved)."""
    try:
        return [f.path for f in resolve_tex_tree(repo.root, repo.main).files]
    except (ConfigError, OSError, ValueError):
        return []


def _members(conn: sqlite3.Connection, row: sqlite3.Row) -> list[Run]:
    runs = [runstore.get_run(conn, run_id) for run_id in json.loads(row["run_ids_json"])]
    return [run for run in runs if run is not None]


def _merged(
    conn: sqlite3.Connection, kind: str, runs: list[Run], order: list[str]
) -> list[MergedFinding]:
    per_run = [
        RunFindings(run.id, run.runner, runstore.list_findings(conn, run_id=run.id)) for run in runs
    ]
    return merge_findings(per_run, order) if kind == "both" else singletons(per_run, order)


def _finished_at(status: str, runs: list[Run]) -> float | None:
    """When the group ended: the last member's end, once the status is final and no member
    is active (a cancelled group ends when its last member has stopped)."""
    if status not in TERMINAL or any(run.status in ACTIVE_STATUSES for run in runs):
        return None
    ends = [run.finished_at for run in runs if run.finished_at is not None]
    return max(ends) if ends else None


def _group(
    row: sqlite3.Row, runs: list[Run], merged: list[MergedFinding]
) -> tuple[RunGroup, GroupStatus]:
    status = derive_status(row["status"], [run.status for run in runs])
    costs = [run.cost_usd for run in runs if run.cost_usd is not None]
    group = RunGroup(
        id=row["id"],
        kind=row["kind"],
        pass_=row["pass"],
        scope=Scope.model_validate_json(row["scope_json"]),
        run_ids=json.loads(row["run_ids_json"]),
        status=status,
        created_at=row["created_at"],
        finished_at=_finished_at(status, runs) or row["finished_at"],
        cost_usd=sum(costs) if costs else None,
        findings_count=sum(run.findings_count for run in runs),
        agreements_count=agreements_count(merged) if row["kind"] == "both" else 0,
    )
    return group, status


def build(
    conn: sqlite3.Connection, row: sqlite3.Row, order: list[str]
) -> tuple[RunGroup, list[Run], list[MergedFinding]]:
    runs = _members(conn, row)
    merged = _merged(conn, row["kind"], runs, order)
    group, _ = _group(row, runs, merged)
    return group, runs, merged


def get_group(conn: sqlite3.Connection, group_id: str, order: list[str]) -> RunGroup | None:
    row = _row(conn, group_id)
    return None if row is None else build(conn, row, order)[0]


def get_detail(conn: sqlite3.Connection, group_id: str, order: list[str]) -> GroupDetail | None:
    row = _row(conn, group_id)
    if row is None:
        return None
    group, runs, merged = build(conn, row, order)
    return GroupDetail(group=group, runs=runs, merged=merged)


def list_groups(conn: sqlite3.Connection, order: list[str], limit: int = 50) -> list[RunGroup]:
    """Newest first."""
    rows = _cursor(conn).execute(
        "SELECT * FROM run_groups ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    )
    return [build(conn, row, order)[0] for row in rows.fetchall()]


def settle(conn: sqlite3.Connection, group_id: str, order: list[str]) -> RunGroup | None:
    """Derive the group's status and end from its members and store them."""
    row = _row(conn, group_id)
    if row is None:
        return None
    runs = _members(conn, row)
    group, status = _group(row, runs, _merged(conn, row["kind"], runs, order))
    if (status, group.finished_at) != (row["status"], row["finished_at"]):
        conn.execute(
            "UPDATE run_groups SET status = ?, finished_at = ? WHERE id = ?",
            (status, group.finished_at, group_id),
        )
    return group


# --------------------------------------------------------------------------- manager


class GroupManager:
    """Creates groups on the shared RunManager and follows their members."""

    def __init__(self, bus: EventBus, runs: RunManager, get_repo: Callable[[], Repo | None]):
        self.bus = bus
        self.runs = runs
        self._get_repo = get_repo
        self._member_of: dict[str, str] = {}  # active member run id → group id
        self._published: dict[str, tuple[Any, ...]] = {}  # group id → last published state
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._consumer: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._consumer is None:
            self._queue = self.bus.subscribe()
            self._consumer = asyncio.create_task(self._consume(self._queue), name="groups")
        repo = self._get_repo()
        if repo is not None:
            await self.on_repo_opened(repo)

    async def shutdown(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer
            self._consumer = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
            self._queue = None
        for task in list(self._tasks):
            task.cancel()

    async def on_repo_opened(self, repo: Repo) -> None:
        """Settle the groups a previous process left active (their runs were failed as
        interrupted by the run manager)."""
        self._member_of.clear()

        def work() -> None:
            if not db.db_path(repo).exists():
                return
            order = file_order(repo)
            with db.connect(repo) as conn:
                rows = conn.execute(
                    "SELECT id FROM run_groups WHERE status IN ('queued', 'running')"
                ).fetchall()
                for (group_id,) in rows:
                    settle(conn, group_id, order)
            if rows:
                log.info("settled %d groups left active by a previous process", len(rows))

        try:
            await asyncio.to_thread(work)
        except Exception:  # start-up must not fail on state.db
            log.exception("group start-up work failed")

    # ----------------------------------------------------------------- planning

    def _config(self, repo: Repo) -> Config:
        try:
            return repo.load_config()
        except ConfigError as exc:
            raise RunError(400, str(exc)) from exc

    def plan_both(self, repo: Repo, request: BothRequest) -> tuple[Scope, list[RunPlan]]:
        """One plan per runner on the same assembled prompt (blocking)."""
        config = self._config(repo)
        try:
            assembled = assemble_prompt(repo, request.pass_, request.scope)
        except PromptError as exc:
            raise RunError(400, str(exc)) from exc
        plans = []
        for runner in RUNNER_NAMES:
            options = getattr(request, runner) or RunnerOptions()
            plan = self.runs.plan_assembled(
                repo, config, assembled, runner, options.model, options.thinking
            )
            plan.scope_variant = runner
            plans.append(plan)
        return assembled.scope, plans

    def plan_fanout(self, repo: Repo, request: FanoutRequest) -> tuple[Scope, list[RunPlan]]:
        """One plan per linked section plus the `message-drift` plan over `ends`."""
        config = self._config(repo)
        try:
            pass_def = get_pass(repo, request.pass_)
            spec, _ = load_spec_for_prompt(repo)
            sections = linked_sections(repo, spec)
        except (PromptError, ConfigError) as exc:
            raise RunError(400, str(exc)) from exc
        if not sections:
            raise RunError(
                400, "no section is linked: adopt the paper's sections in the Spec view first"
            )
        level = config.models.for_level(pass_def.info.level)
        runner = request.runner or level.runner or config.runner.default
        plans = []
        try:
            for node, _, _ in sections:
                assembled = assemble_prompt(
                    repo, request.pass_, Scope(kind="section", ref=node.id), fanout=True
                )
                plans.append(
                    self.runs.plan_assembled(
                        repo, config, assembled, runner, request.model, request.thinking
                    )
                )
            drift = assemble_prompt(repo, DRIFT_PASS, Scope(kind="ends"))
        except PromptError as exc:
            raise RunError(400, str(exc)) from exc
        plans.append(
            self.runs.plan_assembled(repo, config, drift, runner, request.model, request.thinking)
        )
        return Scope(kind="paper"), plans

    # ----------------------------------------------------------------- start

    async def start_both(self, repo: Repo, request: BothRequest) -> RunGroup:
        scope, plans = await self._plan(self.plan_both, repo, request)
        return await self._launch(repo, "both", request.pass_, scope, plans, request.force)

    async def start_fanout(self, repo: Repo, request: FanoutRequest) -> RunGroup:
        scope, plans = await self._plan(self.plan_fanout, repo, request)
        return await self._launch(repo, "fanout", request.pass_, scope, plans, request.force)

    async def _plan(self, fn: Callable[..., Any], repo: Repo, request: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, repo, request)
        except RunError as exc:
            raise GroupError(exc.status, exc.detail) from exc

    async def _launch(
        self,
        repo: Repo,
        kind: GroupKind,
        pass_name: str,
        scope: Scope,
        plans: list[RunPlan],
        force: bool,
    ) -> RunGroup:
        try:
            for plan in plans:
                self.runs.check_available(plan)
            for plan in plans:
                holder = self.runs.busy(plan)
                if holder is not None:
                    raise self.runs.busy_error(plan, holder)
        except RunError as exc:
            raise GroupError(exc.status, exc.detail) from exc
        group_id = new_id()
        for plan in plans:
            plan.on_finished = self._hook(repo, group_id)
        launched: list[Run] = []
        try:
            for plan in plans:
                launched.append(await self.runs.launch(repo, plan, use_cache=not force))
        except RunError as exc:  # a scope taken in between: undo the members started
            for run in launched:
                with contextlib.suppress(RunError):
                    await self.runs.cancel(repo, run.id)
            raise GroupError(exc.status, exc.detail) from exc
        run_ids = [run.id for run in launched]
        for run_id in run_ids:
            self._member_of[run_id] = group_id

        def create() -> None:
            with db.connect(repo) as conn:
                conn.execute(
                    "INSERT INTO run_groups (id, kind, pass, scope_json, run_ids_json, status, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        group_id,
                        kind,
                        pass_name,
                        scope.model_dump_json(),
                        json.dumps(run_ids),
                        "queued",
                        time.time(),
                    ),
                )

        await asyncio.to_thread(create)
        group = await self.refresh(repo, group_id)
        assert group is not None
        return group

    def _hook(self, repo: Repo, group_id: str) -> Callable[[Run, Any], None]:
        """A member's completion hook: refresh the group (the run events usually did)."""

        def finished(run: Run, data: Any) -> None:
            task = asyncio.get_running_loop().create_task(self._refresh_quietly(repo, group_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return finished

    # ----------------------------------------------------------------- following members

    async def refresh(self, repo: Repo, group_id: str) -> RunGroup | None:
        """Settle the group from its members' rows; publish `group.updated` when anything
        visible changed since the last publication."""
        lock = self._locks.setdefault(group_id, asyncio.Lock())
        async with lock:

            def work() -> tuple[RunGroup, tuple[str, ...]] | None:
                if not db.db_path(repo).exists():
                    return None
                order = file_order(repo)
                with db.connect(repo) as conn:
                    group = settle(conn, group_id, order)
                    if group is None:
                        return None
                    statuses = tuple(
                        run.status if run is not None else "missing"
                        for run in (runstore.get_run(conn, rid) for rid in group.run_ids)
                    )
                return group, statuses

            result = await asyncio.to_thread(work)
            if result is None:
                return None
            group, statuses = result
            state = (
                group.status,
                statuses,
                group.findings_count,
                group.agreements_count,
                group.cost_usd,
                group.finished_at,
            )
            if self._published.get(group_id) != state:
                self._published[group_id] = state
                self.bus.publish(GroupUpdated(group=group.model_dump(mode="json", by_alias=True)))
            if not any(status in ACTIVE_STATUSES for status in statuses):
                for run_id in group.run_ids:
                    self._member_of.pop(run_id, None)
            return group

    async def _refresh_quietly(self, repo: Repo, group_id: str) -> None:
        try:
            await self.refresh(repo, group_id)
        except Exception:
            log.exception("refreshing group %s failed", group_id)

    async def _consume(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Refresh a group when one of its member runs starts or ends."""
        while True:
            event = await queue.get()
            if event.get("type") not in RUN_EVENTS:
                continue
            run = event.get("run")
            group_id = self._member_of.get(run.get("id")) if isinstance(run, dict) else None
            repo = self._get_repo()
            if group_id is None or repo is None:
                continue
            await self._refresh_quietly(repo, group_id)

    # ----------------------------------------------------------------- cancel

    async def cancel(self, repo: Repo, group_id: str) -> RunGroup:
        """Cancel every active member (queued or running; their process groups are
        killed). A group with no active member is returned unchanged."""

        def members() -> list[Run] | None:
            if not db.db_path(repo).exists():
                return None
            with db.connect(repo) as conn:
                row = _row(conn, group_id)
                if row is None:
                    return None
                runs = _members(conn, row)
                if any(run.status in ACTIVE_STATUSES for run in runs):
                    conn.execute(
                        "UPDATE run_groups SET status = 'cancelled' WHERE id = ?", (group_id,)
                    )
                return runs

        runs = await asyncio.to_thread(members)
        if runs is None:
            raise GroupError(404, f"unknown group {group_id!r}")
        active = [run for run in runs if run.status in ACTIVE_STATUSES]
        if active:
            await asyncio.gather(
                *(self.runs.cancel(repo, run.id) for run in active), return_exceptions=True
            )
        group = await self.refresh(repo, group_id)
        if group is None:
            raise GroupError(404, f"unknown group {group_id!r}")
        return group
