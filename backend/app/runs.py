"""Review-run orchestration (PLAN.md "Review pass", "LLM interaction extras" phase 4).

`RunManager` (one per backend process, on AppState) turns a request into a run:

1. plan: assemble the prompt (app/prompts.py), choose runner/model/thinking, build the argv
   and the cache key;
2. one active run per scope key (`<pass>:<kind>:<ref or file>`; a second request is 409);
3. cache: unless `force`, a finished run with the same cache key is copied into a new
   `cached` run at once (its findings copied and re-anchored, so states are independent);
4. otherwise the run is queued behind a global limit of `[runner] concurrency` (3) runs,
   then the CLI runs in the sandbox; progress is forwarded as `run.event` (≤ 10/s);
5. the answer is parsed and validated (app/findings.py); accepted findings are anchored in
   the current source; everything (prompt, argv, raw answer, events, usage) is stored.

It also re-anchors open findings when a source file changes (`file.changed` from the
watcher, or a save through PUT /api/file) and publishes `findings.updated`.

Other features reuse the same machinery through `plan_assembled` + `launch` (phase 5: the
blind A/B compare in app/compare.py; phase 6: the triage call in app/triage.py): a plan may
carry its own answer schema, an `interpret` function that replaces findings parsing, and an
`on_finished` hook that runs once the run row reached its final state (done, failed or
cancelled). Such runs never touch the cache.

Phase 6 run groups (app/groups.py) launch ordinary review runs. The two members of a "run
both" group share a pass and a scope, so their plans carry a `scope_variant` (the runner):
runs with different variants of one scope key may run side by side, while a plain run of
that scope key still excludes (and is excluded by) every variant.

Model output only ever becomes rows in state.db. Nothing here writes the spec file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from app import db, runstore
from app.config import (
    PASS_LEVELS,
    RUNNER_NAMES,
    THINKING_LEVELS,
    Config,
    ConfigError,
    RunnerCliConfig,
)
from app.events import (
    EventBus,
    FindingsUpdated,
    RunEventMessage,
    RunFailed,
    RunFinished,
    RunStarted,
)
from app.files import NotTextError, PathError, TooLargeError, read_text_file
from app.findings import (
    Finding,
    FindingIn,
    OutputParseError,
    Rejected,
    anchor_new,
    findings_schema,
    parse_output,
    reanchor,
    resolve_file,
    validate_findings,
)
from app.prompts import (
    AssembledPrompt,
    PromptError,
    Scope,
    assemble_prompt,
    ensure_sandbox,
    source_lines,
)
from app.repo import Repo
from app.runner import CliProbe, ProcessHandle, RunEvent, RunResult, make_runner
from app.runner.auth import AuthCache, auth_error, auth_warning
from app.runner.base import KILL_GRACE_S, help_argv, probe_cli
from app.runner.context import context_leaks, context_warning
from app.runner.paste import NAME as PASTE
from app.runner.paste import paste_result
from app.runstore import Run

log = logging.getLogger(__name__)

EVENT_INTERVAL_S = 0.1  # run.event throttle: at most 10 per second per run
VARIANT_SEP = "\x1f"  # between a scope key and its variant in the active-scope registry
CANCEL_WAIT_S = KILL_GRACE_S + 3.0


class RunError(Exception):
    """A request the run API refuses; `status` is the HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- requests


class RunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pass_: str = Field(alias="pass")
    scope: Scope
    runner: str | None = None  # default: the pass level's runner, else [runner] default
    model: str | None = None  # null: front matter / config default; "": the CLI's default
    thinking: str | None = None  # low | medium | high
    force: bool = False  # skip the cache


class PasteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pass_: str = Field(alias="pass")
    scope: Scope
    raw: str


class Preview(BaseModel):
    prompt: str
    scope_name: str
    cache_hit: bool
    model: str | None
    thinking: str | None
    command: list[str]
    runner: str  # +
    scope: Scope  # + the normalized scope
    cache_key: str  # +


def scope_key(pass_name: str, scope: Scope) -> str:
    return f"{pass_name}:{scope.kind}:{scope.ref or scope.file or ''}"


@dataclass
class RunOutcome:
    """What a finished CLI result means: the run's final status and error, findings for a
    review run, or `data` from a plan's own `interpret` function."""

    status: str
    error: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    data: Any = None


@dataclass
class RunPlan:
    assembled: AssembledPrompt
    runner: str
    model: str | None
    thinking: str | None
    command: list[str]
    cache_key: str  # "" = never cached and never a cache source
    scope_key: str
    probe: CliProbe | None
    config: Config
    # Phase 5 extension points (None = a review run: findings schema, findings parsing).
    schema: dict[str, Any] | None = None  # the answer schema passed to the CLI
    interpret: Callable[[RunResult], RunOutcome] | None = None  # parse an "ok" result
    free_text: bool = False  # no structured-output schema; `interpret` reads the text
    # Called once (blocking, on the event loop) with the final Run and RunOutcome.data.
    on_finished: Callable[[Run, Any], None] | None = None
    # Phase 6: runs whose plans name different variants of one scope key may be active at
    # once (the two members of a "run both" group); None = the plain scope key.
    scope_variant: str | None = None

    @property
    def lock_key(self) -> str:
        """The key held in the active-scope registry while the run is active."""
        if self.scope_variant is None:
            return self.scope_key
        return f"{self.scope_key}{VARIANT_SEP}{self.scope_variant}"

    @property
    def answer_schema(self) -> dict[str, Any] | None:
        """The structured-output schema sent to the CLI: the plan's own, else the findings
        schema; None for a free-text plan (phase 7 drafts answer in LaTeX, not JSON, because
        JSON string escapes mangle `\\ref`, `\\textsc`, `\\newline`)."""
        if self.free_text:
            return None
        return self.schema if self.schema is not None else findings_schema()

    @property
    def pass_name(self) -> str:
        return self.assembled.pass_def.info.name

    @property
    def level(self) -> str:
        return self.assembled.pass_def.info.level

    @property
    def allow_rewrites(self) -> bool:
        return self.assembled.pass_def.info.allow_rewrites


# --------------------------------------------------------------------------- helpers


def git_head(root: Path) -> str | None:
    """`git rev-parse HEAD` in the repo, or None outside git / before the first commit."""
    if shutil.which("git") is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
        return None
    return sha


def global_instruction_files() -> list[dict[str, Any]]:
    """The user-level instruction files each CLI injects (only checked, never written)."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    out = []
    for runner, path in (("claude", claude_dir / "CLAUDE.md"), ("codex", codex_dir / "AGENTS.md")):
        out.append({"runner": runner, "path": str(path), "present": path.is_file()})
    return out


_PROBES: dict[tuple[str, ...], CliProbe] = {}
_PROBE_LOCK = threading.Lock()


def cached_probe(command: list[str]) -> CliProbe:
    """`probe_cli` once per help command line for the life of the process. The result says
    which optional flags the installed CLI accepts; restart the server after upgrading a
    CLI. A CLI that was not found is probed again next time (it may have been installed)."""
    key = tuple(help_argv(command))
    with _PROBE_LOCK:
        cached = _PROBES.get(key)
    if cached is not None:
        return cached
    result = probe_cli(command)
    if result.available:
        with _PROBE_LOCK:
            result = _PROBES.setdefault(key, result)
    return result


def clear_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBES.clear()


def _load_config(repo: Repo) -> Config:
    try:
        return repo.load_config()
    except ConfigError as exc:
        raise RunError(400, str(exc)) from exc


def _dump(run: Run) -> dict[str, Any]:
    return run.model_dump(mode="json", by_alias=True)


class EventThrottle:
    """Coalesces run progress into at most one `run.event` per EVENT_INTERVAL_S.

    Consecutive events of the same kind are merged (text concatenated, status lines
    joined). Must be used from the event loop thread."""

    def __init__(self, bus: EventBus, run_id: str, interval: float = EVENT_INTERVAL_S) -> None:
        self.bus = bus
        self.run_id = run_id
        self.interval = interval
        self.pending: deque[list[str]] = deque()  # [kind, text]
        self.last = 0.0
        self.timer: asyncio.TimerHandle | None = None

    def push(self, event: RunEvent) -> None:
        if self.pending and self.pending[-1][0] == event.kind:
            sep = "" if event.kind == "text" else "\n"
            self.pending[-1][1] += sep + event.text
        else:
            self.pending.append([event.kind, event.text])
        if self.timer is None:
            delay = max(0.0, self.last + self.interval - time.monotonic())
            self.timer = asyncio.get_running_loop().call_later(delay, self._flush)

    def _publish(self, kind: str, text: str) -> None:
        self.bus.publish(
            RunEventMessage(run_id=self.run_id, kind=cast(Any, kind), text=text)  # text|status
        )

    def _flush(self) -> None:
        self.timer = None
        if not self.pending:
            return
        kind, text = self.pending.popleft()
        self._publish(kind, text)
        self.last = time.monotonic()
        if self.pending:
            self.timer = asyncio.get_running_loop().call_later(self.interval, self._flush)

    def close(self) -> None:
        """Flush what is left (merged per kind) and stop."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        merged: dict[str, str] = {}
        for kind, text in self.pending:
            sep = "" if kind == "text" else "\n"
            merged[kind] = merged[kind] + sep + text if kind in merged else text
        self.pending.clear()
        for kind, text in merged.items():
            self._publish(kind, text)


@dataclass
class ActiveRun:
    run_id: str
    scope_key: str
    handle: ProcessHandle
    task: asyncio.Task[None] | None = None
    started: bool = False  # holds a slot and the CLI is (about to be) running


# --------------------------------------------------------------------------- manager


class RunManager:
    def __init__(
        self,
        bus: EventBus,
        get_repo: Callable[[], Repo | None],
        *,
        auth: AuthCache | None = None,
    ) -> None:
        self.bus = bus
        self._get_repo = get_repo
        # `claude auth status` / `codex login status` answers for GET /api/runners (60 s).
        self.auth = auth if auth is not None else AuthCache()
        self._active: dict[str, ActiveRun] = {}
        self._scope_keys: dict[str, str] = {}
        self._running = 0
        self._slots = asyncio.Condition()
        self._consumer: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None

    # ----------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._consumer is None:
            self._queue = self.bus.subscribe()
            self._consumer = asyncio.create_task(self._consume(self._queue), name="reanchor")
        repo = self._get_repo()
        if repo is not None:
            await self.on_repo_opened(repo)

    async def shutdown(self) -> None:
        await self.cancel_all("server shutting down")
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer
            self._consumer = None
        if self._queue is not None:
            self.bus.unsubscribe(self._queue)
            self._queue = None

    async def on_repo_opened(self, repo: Repo) -> None:
        """Fail runs a previous process left behind, and probe the CLIs in the background."""
        if self._active:
            await self.cancel_all("another repo was opened")

        def work() -> None:
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    count = runstore.mark_interrupted(conn)
                if count:
                    log.info("marked %d interrupted runs as failed", count)
            try:
                config = repo.load_config()
            except ConfigError:
                return
            for name in RUNNER_NAMES:
                self.probe(config.runner.cli(name))

        try:
            await asyncio.to_thread(work)
        except Exception:  # startup must not fail on state.db or a probe
            log.exception("run manager start-up work failed")

    async def cancel_all(self, reason: str) -> None:
        active = list(self._active.values())
        for item in active:
            item.handle.cancel("cancelled")
            if not item.started and item.task is not None:
                item.task.cancel()
        tasks = [item.task for item in active if item.task is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=CANCEL_WAIT_S)
        if active:
            log.info("cancelled %d runs: %s", len(active), reason)

    # ----------------------------------------------------------------- probes

    def probe(self, cli: RunnerCliConfig) -> CliProbe:
        """`--help` of the CLI, parsed once per process and command (thread-safe)."""
        return cached_probe(cli.command)

    def runners_info(self, config: Config, sandbox: Path | None = None) -> dict[str, Any]:
        """GET /api/runners (blocking: may probe). With the repo's `sandbox`, also the
        instruction files outside it each available CLI could read (`context_leaks`).

        Each CLI found on PATH is also asked how it is logged in (`auth`, cached 60 s, same
        environment as a run): not logged in makes it unavailable with an `error`; a login
        that is not a claude.ai / ChatGPT subscription only adds a `warning`."""
        runners = []
        leaks: list[dict[str, Any]] = []
        for name in RUNNER_NAMES:
            cli = config.runner.cli(name)
            probe = self.probe(cli)
            auth = (
                self.auth.get(name, cli, allow_api_keys=config.runner.allow_api_keys, cwd=sandbox)
                if probe.available
                else None
            )
            login_error = auth_error(name, auth)
            isolation = bool(cli.isolation_args) and probe.accepts(cli.isolation_args)
            optional = {
                "model": bool(cli.model_flag) and probe.accepts([cli.model_flag]),
                "thinking": bool(cli.thinking_args) and probe.accepts(cli.thinking_args),
                "schema": bool(cli.schema_args) and probe.accepts(cli.schema_args),
                "isolation": isolation,
                "stream": bool(cli.stream_args) and probe.accepts(cli.stream_args),
            }
            runners.append(
                {
                    "name": name,
                    "available": probe.available and login_error is None,
                    "models": list(cli.models),
                    "suppress_global": probe.available and isolation and cli.suppress_global,
                    "path": probe.path,
                    "error": login_error if probe.available else probe.error,
                    "auth": auth.model_dump() if auth is not None else None,
                    "warning": auth_warning(name, auth),
                    "flags": optional,
                    "missing_flags": probe.missing([*cli.command, *cli.no_tools_args])
                    if probe.available and probe.flags
                    else [],
                }
            )
            if sandbox is not None and probe.available:
                argv = make_runner(
                    name,
                    cli,
                    probe,
                    sandbox,
                    config.runner.timeout_s,
                    allow_api_keys=config.runner.allow_api_keys,
                ).command(None, None)
                leaks += [leak.model_dump() for leak in context_leaks(sandbox, name, argv)]
        defaults = {}
        for level in PASS_LEVELS:
            entry = config.models.for_level(level)
            defaults[level] = {
                "runner": entry.runner or config.runner.default,
                "model": entry.model or None,
                "thinking": entry.thinking,
            }
        return {
            "runners": runners,
            "thinking_levels": list(THINKING_LEVELS),
            "defaults": defaults,
            "global_instructions": global_instruction_files(),
            "context_leaks": leaks,
        }

    # ----------------------------------------------------------------- planning

    def plan(
        self,
        repo: Repo,
        config: Config,
        pass_name: str,
        scope: Scope,
        runner: str | None,
        model: str | None,
        thinking: str | None,
    ) -> RunPlan:
        """Assemble the prompt and settle runner, model, thinking, argv and cache key.

        Model: an explicit request value wins ("" = the CLI's default, no flag). Otherwise
        the pass's front matter, then `[models.<level>]`, but only when the run uses that
        level's runner (a claude model name means nothing to codex): with another runner the
        CLI's default model is used. Thinking: request, front matter, `[models.<level>]`."""
        try:
            assembled = assemble_prompt(repo, pass_name, scope)
        except PromptError as exc:
            raise RunError(400, str(exc)) from exc
        return self.plan_assembled(repo, config, assembled, runner, model, thinking)

    def plan_assembled(
        self,
        repo: Repo,
        config: Config,
        assembled: AssembledPrompt,
        runner: str | None,
        model: str | None,
        thinking: str | None,
        *,
        schema: dict[str, Any] | None = None,
        scope_key_override: str | None = None,
        cacheable: bool = True,
        free_text: bool = False,
    ) -> RunPlan:
        """Settle runner, model, thinking, argv and cache key for an assembled prompt (the
        rules of `plan`). `schema` replaces the findings schema; `cacheable=False` gives the
        plan no cache key, so it is never served from or stored into the cache."""
        info = assembled.pass_def.info
        level_default = config.models.for_level(info.level)
        default_runner = level_default.runner or config.runner.default
        chosen = runner or default_runner
        if chosen not in RUNNER_NAMES:
            raise RunError(400, f"unknown runner {chosen!r} (use {', '.join(RUNNER_NAMES)})")
        if model is not None:
            chosen_model = model.strip() or None
        elif chosen == default_runner:
            chosen_model = info.model or level_default.model or None
        else:
            chosen_model = None
        chosen_thinking = thinking or info.thinking or level_default.thinking
        if chosen_thinking not in THINKING_LEVELS:
            raise RunError(
                400, f"thinking must be one of {', '.join(THINKING_LEVELS)}; got {thinking!r}"
            )
        cli = config.runner.cli(chosen)
        probe = self.probe(cli)
        runner_obj = make_runner(
            chosen,
            cli,
            probe,
            repo.sandbox_dir,
            config.runner.timeout_s,
            allow_api_keys=config.runner.allow_api_keys,
        )
        answer_schema = None if free_text else (schema if schema is not None else findings_schema())
        command = runner_obj.command(chosen_model, chosen_thinking, schema=answer_schema)
        return RunPlan(
            assembled=assembled,
            runner=chosen,
            model=chosen_model,
            thinking=chosen_thinking,
            command=command,
            cache_key=assembled.cache_key(chosen, chosen_model, chosen_thinking)
            if cacheable
            else "",
            scope_key=scope_key_override or scope_key(info.name, assembled.scope),
            probe=probe,
            config=config,
            schema=schema,
            free_text=free_text,
        )

    def _plan_request(self, repo: Repo, request: RunRequest) -> RunPlan:
        config = _load_config(repo)
        return self.plan(
            repo,
            config,
            request.pass_,
            request.scope,
            request.runner,
            request.model,
            request.thinking,
        )

    async def preview(self, repo: Repo, request: RunRequest) -> Preview:
        def work() -> Preview:
            plan = self._plan_request(repo, request)
            hit = False
            if db.db_path(repo).exists():
                with db.connect(repo) as conn:
                    hit = runstore.find_cached(conn, plan.cache_key) is not None
            return Preview(
                prompt=plan.assembled.prompt,
                scope_name=plan.assembled.scope_name,
                cache_hit=hit,
                model=plan.model,
                thinking=plan.thinking,
                command=plan.command,
                runner=plan.runner,
                scope=plan.assembled.scope,
                cache_key=plan.cache_key,
            )

        return await asyncio.to_thread(work)

    # ----------------------------------------------------------------- start

    def _base_row(self, plan: RunPlan, run_id: str, status: str, git_sha: str | None) -> dict:
        scope = plan.assembled.scope
        return {
            "id": run_id,
            "pass": plan.pass_name,
            "level": plan.level,
            "scope_kind": scope.kind,
            "scope_ref": scope.ref or scope.file,
            "scope_json": scope.model_dump_json(),
            "scope_name": plan.assembled.scope_name,
            "scope_key": plan.scope_key,
            "runner": plan.runner,
            "model": plan.model,
            "thinking": plan.thinking,
            "allow_rewrites": int(plan.allow_rewrites),
            "git_sha": git_sha,
            "prompt_hash": plan.assembled.prompt_hash,
            "scope_hash": plan.assembled.scope_hash,
            "spec_hash": plan.assembled.spec_hash,
            "cache_key": plan.cache_key or None,
            "prompt": plan.assembled.prompt,
            "command_json": _json(plan.command),
            "status": status,
            "created_at": time.time(),
        }

    async def start_run(self, repo: Repo, request: RunRequest) -> Run:
        plan = await asyncio.to_thread(self._plan_request, repo, request)
        return await self.launch(repo, plan, use_cache=not request.force)

    @staticmethod
    def check_available(plan: RunPlan) -> None:
        if plan.probe is None or not plan.probe.available:
            reason = plan.probe.error if plan.probe is not None else "not configured"
            raise RunError(400, f"runner {plan.runner} is not available: {reason}")

    def busy(self, plan: RunPlan) -> str | None:
        """The id of an active run whose scope key conflicts with the plan's (None when the
        scope is free): the same key and variant, or the same key where either side is the
        plain key."""
        key = plan.lock_key
        for held, holder in self._scope_keys.items():
            base = held.split(VARIANT_SEP, 1)[0]
            if base != plan.scope_key:
                continue
            if held == key or VARIANT_SEP not in held or plan.scope_variant is None:
                return holder
        return None

    def busy_error(self, plan: RunPlan, holder: str) -> RunError:
        return RunError(
            409,
            f"a run of {plan.pass_name} on {plan.assembled.scope_name} is already active "
            f"({holder}); cancel it or wait",
        )

    async def launch(
        self,
        repo: Repo,
        plan: RunPlan,
        *,
        use_cache: bool = True,
        on_created: Callable[[sqlite3.Connection, Run], None] | None = None,
    ) -> Run:
        """Start a planned run: scope lock, cache (when the plan has a cache key and
        `use_cache`), the `queued` row, then the background task. `on_created` runs in the
        same database connection right after the run row is inserted and before the task
        starts, so rows that refer to the run exist before it can finish."""
        self.check_available(plan)
        key = plan.lock_key
        holder = self.busy(plan)
        if holder is not None:
            raise self.busy_error(plan, holder)
        self._scope_keys[key] = "starting"  # reserved before the first await below
        try:
            if use_cache and plan.cache_key:
                cached = await asyncio.to_thread(self._copy_if_cached, repo, plan)
                if cached is not None:
                    self._scope_keys.pop(key, None)  # nothing stays active for a cache hit
                    self.bus.publish(RunFinished(run=_dump(cached)))
                    self.bus.publish(FindingsUpdated(run_id=cached.id))
                    return cached
            run_id = runstore.new_run_id()

            def create() -> Run:
                git_sha = git_head(repo.root)
                with db.connect(repo) as conn:
                    runstore.insert_run(conn, self._base_row(plan, run_id, "queued", git_sha))
                    run = runstore.get_run(conn, run_id)
                    assert run is not None
                    if on_created is not None:
                        on_created(conn, run)
                return run

            run = await asyncio.to_thread(create)
        except BaseException:
            self._scope_keys.pop(key, None)
            raise
        self._scope_keys[key] = run_id
        active = ActiveRun(run_id=run_id, scope_key=key, handle=ProcessHandle())
        self._active[run_id] = active
        active.task = asyncio.create_task(self._execute(repo, plan, active), name=f"run:{run_id}")
        return run

    def _copy_if_cached(self, repo: Repo, plan: RunPlan) -> Run | None:
        """A new `cached` run copying the newest finished run with the same cache key."""
        with db.connect(repo) as conn:
            source_id = runstore.find_cached(conn, plan.cache_key)
            if source_id is None:
                return None
            source = runstore.get_run(conn, source_id)
            text = runstore.get_run_text(conn, source_id)
            if source is None or text is None:
                return None
            findings = runstore.list_findings(conn, run_id=source_id)
            rejected = runstore.list_rejected(conn, source_id)
            run_id = runstore.new_run_id()
            now = time.time()
            row = self._base_row(plan, run_id, "cached", git_head(repo.root))
            row.update(
                cached_from=source_id,
                command_json=_json(text.command),
                raw_output=text.raw_output,
                raw_events=text.raw_events,
                stderr=text.stderr,
                model_used=source.model_used,
                context_warning=source.context_warning,  # the answer is that run's
                started_at=now,
                finished_at=now,
                duration_ms=0,
                input_tokens=None,
                output_tokens=None,
                cost_usd=0.0,
            )
            lines_cache: dict[str, list[str] | None] = {}
            copies = []
            for index, finding in enumerate(findings):
                lines = self._current_lines(repo, finding.file, lines_cache)
                anchor = anchor_new(
                    finding.quote, lines or [], finding.line_start, finding.line_end
                )
                copies.append(
                    {
                        **finding.model_dump(exclude={"id", "run_id", "state", "created_at"}),
                        "id": runstore.finding_id(run_id, index),
                        "run_id": run_id,
                        "idx": index,
                        "state": "open",
                        "anchor_state": anchor.state,
                        "anchor_line_start": anchor.line_start,
                        "anchor_line_end": anchor.line_end,
                    }
                )
            with db.transaction(conn):
                runstore.insert_run(conn, row)
                runstore.insert_findings(conn, copies)
                runstore.insert_rejected(conn, run_id, rejected)
            return runstore.get_run(conn, run_id)

    @staticmethod
    def _current_lines(
        repo: Repo, rel: str, cache: dict[str, list[str] | None]
    ) -> list[str] | None:
        if rel not in cache:
            try:
                text, _ = read_text_file(repo.root, rel)
                cache[rel] = source_lines(text)
            except (OSError, PathError, NotTextError, TooLargeError):
                cache[rel] = None
        return cache[rel]

    # ----------------------------------------------------------------- execution

    @contextlib.asynccontextmanager
    async def _slot(self, limit: int) -> AsyncIterator[None]:
        async with self._slots:
            while self._running >= limit:
                await self._slots.wait()
            self._running += 1
        try:
            yield
        finally:
            async with self._slots:
                self._running -= 1
                self._slots.notify_all()

    @property
    def running_count(self) -> int:
        return self._running

    async def _execute(self, repo: Repo, plan: RunPlan, active: ActiveRun) -> None:
        run_id = active.run_id
        throttle = EventThrottle(self.bus, run_id)
        limit = plan.config.runner.concurrency
        final: Run | None = None
        data: Any = None
        try:
            if self._running >= limit:
                throttle.push(
                    RunEvent(kind="status", text=f"queued: {self._running} runs are active")
                )
            async with self._slot(limit):
                if active.handle.cancelled:
                    raise asyncio.CancelledError
                active.started = True
                started_at = time.time()

                def mark_running() -> Run:
                    ensure_sandbox(repo, plan.assembled.system_text)
                    # Instruction files outside the sandbox this argv lets the CLI read
                    # (audit 2026-10-18 item 7): recorded on the run, never silently.
                    warning = context_warning(
                        context_leaks(repo.sandbox_dir, plan.runner, plan.command)
                    )
                    with db.connect(repo) as conn:
                        runstore.update_run(
                            conn,
                            run_id,
                            status="running",
                            started_at=started_at,
                            context_warning=warning,
                        )
                        run = runstore.get_run(conn, run_id)
                    assert run is not None
                    return run

                run = await asyncio.to_thread(mark_running)
                if run.context_warning:
                    throttle.push(RunEvent(kind="status", text=f"warning: {run.context_warning}"))
                self.bus.publish(RunStarted(run=_dump(run)))
                cli = plan.config.runner.cli(plan.runner)
                assert plan.probe is not None
                runner = make_runner(
                    plan.runner,
                    cli,
                    plan.probe,
                    repo.sandbox_dir,
                    plan.config.runner.timeout_s,
                    allow_api_keys=plan.config.runner.allow_api_keys,
                )
                result = await runner.run(
                    plan.assembled.prompt,
                    plan.answer_schema,
                    plan.model,
                    plan.thinking,
                    on_event=throttle.push,
                    handle=active.handle,
                )
                # Complete (store findings, publish run.finished) while still holding
                # the slot, so a queued run never announces run.started before this
                # run's run.finished: the UI's "3 at once" count then matches reality.
                throttle.close()
                final, data = await self._complete(repo, plan, run_id, result, started_at)
        except asyncio.CancelledError:
            throttle.close()
            final = self._finish_without_result(repo, run_id, "cancelled", "cancelled")
        except Exception as exc:
            log.exception("run %s failed", run_id)
            throttle.close()
            final = self._finish_without_result(repo, run_id, "failed", f"internal error: {exc}")
        finally:
            self._active.pop(run_id, None)
            if self._scope_keys.get(active.scope_key) == run_id:
                del self._scope_keys[active.scope_key]
        if plan.on_finished is not None:
            self._notify_finished(repo, plan, run_id, final, data)

    def _notify_finished(
        self, repo: Repo, plan: RunPlan, run_id: str, final: Run | None, data: Any
    ) -> None:
        assert plan.on_finished is not None
        try:
            run = final if final is not None else self.get_run(repo, run_id)
            if run is not None:
                plan.on_finished(run, data)
        except Exception:
            log.exception("the completion hook of run %s failed", run_id)

    def _finish_without_result(
        self, repo: Repo, run_id: str, status: str, error: str
    ) -> Run | None:
        """Mark a run cancelled/failed without awaiting (safe inside a cancelled task)."""
        now = time.time()
        try:
            with db.connect(repo) as conn:
                current = runstore.get_run(conn, run_id)
                if current is None or current.status not in runstore.ACTIVE_STATUSES:
                    return current
                duration = int((now - current.started_at) * 1000) if current.started_at else None
                runstore.update_run(
                    conn, run_id, status=status, error=error, finished_at=now, duration_ms=duration
                )
                run = runstore.get_run(conn, run_id)
        except Exception:
            log.exception("could not record the end of run %s", run_id)
            return None
        if run is not None:
            self.bus.publish(RunFailed(run=_dump(run)))
        return run

    def _outcome(self, repo: Repo, plan: RunPlan, result: RunResult) -> RunOutcome:
        """Parse, validate and anchor a finished CLI result (blocking)."""
        if result.status == "cancelled":
            return RunOutcome(status="cancelled", error=result.error or "cancelled")
        if result.status != "ok":
            return RunOutcome(status="failed", error=result.error or result.status)
        if plan.interpret is not None:
            return plan.interpret(result)
        try:
            parsed = parse_output(result.text, result.structured)
        except OutputParseError as exc:
            return RunOutcome(status="failed", error=f"could not parse findings: {exc}")
        accepted, rejected = validate_findings(parsed.items, plan.allow_rewrites)
        rejected = [*parsed.rejected, *rejected]
        files: dict[str, list[str]] = {}
        cache: dict[str, list[str] | None] = {}
        for path, prompt_lines in plan.assembled.resolved.files.items():
            files[path] = self._current_lines(repo, path, cache) or prompt_lines
        return RunOutcome(status="done", **self._anchor_all(accepted, files, rejected))

    @staticmethod
    def _anchor_all(
        accepted: list[tuple[int, FindingIn]],
        files: dict[str, list[str]],
        rejected: list[Rejected],
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for index, finding in accepted:
            path = resolve_file(finding.file, finding.quote, finding.line_start, files)
            if path is None:
                rejected.append(
                    Rejected(index=index, reason="invalid:file", raw=finding.model_dump())
                )
                continue
            anchor = anchor_new(finding.quote, files[path], finding.line_start, finding.line_end)
            rows.append(
                {
                    **finding.model_dump(),
                    "file": path,
                    "idx": index,
                    "anchor_state": anchor.state,
                    "anchor_line_start": anchor.line_start,
                    "anchor_line_end": anchor.line_end,
                }
            )
        rejected.sort(key=lambda r: r.index)
        return {"findings": rows, "rejected": rejected}

    def _store_outcome(
        self,
        repo: Repo,
        run_id: str,
        result: RunResult,
        outcome: RunOutcome,
        started_at: float | None,
    ) -> Run:
        now = time.time()
        raw_output = result.text
        if not raw_output and result.structured is not None:
            raw_output = _json(result.structured)
        values: dict[str, Any] = {
            "status": outcome.status,
            "error": outcome.error,
            "raw_output": raw_output,
            "raw_events": result.raw_events or None,
            "stderr": result.stderr or None,
            "model_used": result.model,
            "finished_at": now,
            "duration_ms": result.usage.wall_ms
            if result.usage.wall_ms is not None
            else (int((now - started_at) * 1000) if started_at else 0),
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
        }
        if result.command:
            values["command_json"] = _json(result.command)
        rows = [
            {**row, "id": runstore.finding_id(run_id, row["idx"]), "run_id": run_id}
            for row in outcome.findings
        ]
        with db.connect(repo) as conn, db.transaction(conn):
            runstore.update_run(conn, run_id, **values)
            runstore.insert_findings(conn, rows)
            runstore.insert_rejected(conn, run_id, outcome.rejected)
        with db.connect(repo) as conn:
            run = runstore.get_run(conn, run_id)
        assert run is not None
        return run

    async def _complete(
        self, repo: Repo, plan: RunPlan, run_id: str, result: RunResult, started_at: float
    ) -> tuple[Run, Any]:
        def work() -> tuple[Run, Any]:
            outcome = self._outcome(repo, plan, result)
            run = self._store_outcome(repo, run_id, result, outcome, started_at)
            return run, outcome.data

        run, data = await asyncio.to_thread(work)
        if run.status == "done":
            self.bus.publish(RunFinished(run=_dump(run)))
        else:
            self.bus.publish(RunFailed(run=_dump(run)))
        if plan.interpret is None:  # a review run: its findings changed
            self.bus.publish(FindingsUpdated(run_id=run_id))
        return run, data

    # ----------------------------------------------------------------- cancel / paste

    async def cancel(self, repo: Repo, run_id: str) -> Run:
        active = self._active.get(run_id)
        if active is not None:
            active.handle.cancel("cancelled")
            if not active.started and active.task is not None:
                active.task.cancel()
            if active.task is not None:
                await asyncio.wait({active.task}, timeout=CANCEL_WAIT_S)
        run = await asyncio.to_thread(self.get_run, repo, run_id)
        if run is None:
            raise RunError(404, f"unknown run {run_id!r}")
        return run

    def active_pid(self, run_id: str) -> int | None:
        active = self._active.get(run_id)
        return active.handle.pid if active is not None else None

    async def paste(self, repo: Repo, request: PasteRequest) -> Run:
        """A run from pasted JSON (runner "paste"): validated exactly like a live run."""

        def work() -> Run:
            config = _load_config(repo)
            try:
                assembled = assemble_prompt(repo, request.pass_, request.scope)
            except PromptError as exc:
                raise RunError(400, str(exc)) from exc
            plan = RunPlan(
                assembled=assembled,
                runner=PASTE,
                model=None,
                thinking=None,
                command=[],
                cache_key="",
                scope_key=scope_key(assembled.pass_def.info.name, assembled.scope),
                probe=None,
                config=config,
            )
            run_id = runstore.new_run_id()
            now = time.time()
            row = self._base_row(plan, run_id, "running", git_head(repo.root))
            row.update(cache_key=None, started_at=now)
            with db.connect(repo) as conn:
                runstore.insert_run(conn, row)
            result = paste_result(request.raw)
            outcome = self._outcome(repo, plan, result)
            return self._store_outcome(repo, run_id, result, outcome, now)

        run = await asyncio.to_thread(work)
        self.bus.publish((RunFinished if run.status == "done" else RunFailed)(run=_dump(run)))
        self.bus.publish(FindingsUpdated(run_id=run.id))
        return run

    # ----------------------------------------------------------------- reads

    @staticmethod
    def get_run(repo: Repo, run_id: str) -> Run | None:
        if not db.db_path(repo).exists():
            return None
        with db.connect(repo) as conn:
            return runstore.get_run(conn, run_id)

    # ----------------------------------------------------------------- findings

    async def set_finding_state(self, repo: Repo, finding_id: str, state: str) -> Finding:
        def work() -> Finding | None:
            if not db.db_path(repo).exists():
                return None
            with db.connect(repo) as conn:
                if runstore.get_finding(conn, finding_id) is None:
                    return None
                runstore.set_finding_state(conn, finding_id, state)
                return runstore.get_finding(conn, finding_id)

        finding = await asyncio.to_thread(work)
        if finding is None:
            raise RunError(404, f"unknown finding {finding_id!r}")
        self.bus.publish(FindingsUpdated(run_id=finding.run_id, file=finding.file))
        return finding

    async def reanchor_file(self, repo: Repo, rel: str) -> int:
        """Re-anchor the open findings in one file; publishes `findings.updated` when any
        anchor changed. Returns how many changed."""
        changed = await asyncio.to_thread(self._reanchor_sync, repo, rel)
        if changed:
            self.bus.publish(FindingsUpdated(run_id=None, file=rel))
        return changed

    def _reanchor_sync(self, repo: Repo, rel: str) -> int:
        if not db.db_path(repo).exists():
            return 0
        with db.connect(repo) as conn:
            findings = runstore.open_findings_for_file(conn, rel)
            if not findings:
                return 0
            lines = self._current_lines(repo, rel, {})
            changed = 0
            with db.transaction(conn):
                for finding in findings:
                    result = reanchor(
                        finding.quote,
                        lines,
                        finding.anchor_state,
                        finding.anchor_line_start,
                        finding.anchor_line_end,
                        runstore.finding_origin(conn, finding.id),
                    )
                    before = (
                        finding.anchor_state,
                        finding.anchor_line_start,
                        finding.anchor_line_end,
                    )
                    if (result.state, result.line_start, result.line_end) != before:
                        runstore.update_anchor(
                            conn, finding.id, result.state, result.line_start, result.line_end
                        )
                        changed += 1
            return changed

    async def _consume(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Re-anchor on every `file.changed` from the watcher (or a spec adoption)."""
        while True:
            event = await queue.get()
            if event.get("type") != "file.changed" or not isinstance(event.get("path"), str):
                continue
            repo = self._get_repo()
            if repo is None:
                continue
            try:
                await self.reanchor_file(repo, event["path"])
            except Exception:
                log.exception("re-anchoring %s failed", event.get("path"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
