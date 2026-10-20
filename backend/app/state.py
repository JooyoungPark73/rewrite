"""Per-process application state: the open repo, event bus, compile lock, watcher,
background Docker image pulls, the review-run manager (phase 4), the git manager and
blind A/B comparisons (phase 5), and run groups and triage calls (phase 6)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

from app.compare import CompareManager
from app.compiler import Compiler, get_compiler
from app.compiler.docker import PullPublish, docker_image, pull
from app.config import CompilerConfig
from app.drafts import DraftManager
from app.events import DockerPullFinished, EventBus, Watcher
from app.gitops import GitManager
from app.groups import GroupManager
from app.repo import Repo
from app.revise import ReviseManager
from app.runner.auth import AuthCache
from app.runs import RunManager
from app.triage import TriageManager

log = logging.getLogger(__name__)

PullFn = Callable[[int, PullPublish], Awaitable[bool]]


class AppState:
    """One paper repo per backend process (PLAN.md "Assumed defaults")."""

    def __init__(
        self,
        repo: Repo | None,
        *,
        watch: bool = True,
        compiler_factory: Callable[[CompilerConfig], Compiler] = get_compiler,
        pull_fn: PullFn = pull,
        docker_cli: str = "docker",
    ) -> None:
        self.repo = repo
        self.watch = watch
        self.compiler_factory = compiler_factory
        self.pull_fn = pull_fn
        self.docker_cli = docker_cli
        self.bus = EventBus()
        self.compile_lock = asyncio.Lock()
        self.pull_lock = asyncio.Lock()  # one `docker pull` at a time
        # Serialises spec edits (read-modify-write of the spec file and marker insertion).
        self.spec_lock = asyncio.Lock()
        # Per paper repo: serialises writes to its source files (audit 2026-10-18 item 5):
        # PUT /api/file, marker insertion and re-linking, git pull. Whoever needs both
        # takes spec_lock first, then the source lock.
        self._source_locks: dict[Path, asyncio.Lock] = {}
        self._pulls: dict[str, asyncio.Task[None]] = {}  # image → running or queued pull
        self.watcher: Watcher | None = None
        # How each LLM CLI is logged in (GET /api/runners), cached for 60 s.
        self.auth = AuthCache()
        # LLM review runs: queue, concurrency cap, cancel, cache, re-anchoring.
        self.runs = RunManager(self.bus, lambda: self.repo, auth=self.auth)
        # One git operation at a time; push/pull stream output and re-anchor after a pull.
        self.git = GitManager(
            self.bus,
            spec_lock=self.spec_lock,
            source_lock=self.source_lock,
            reanchor=self.runs.reanchor_file,
        )
        # Blind A/B comparisons run on the review-run machinery.
        self.compare = CompareManager(self.bus, self.runs)
        self.drafts = DraftManager(self.bus, self.runs)
        self.revise = ReviseManager(self.bus, self.runs)
        # "Run both" and per-section fan-out groups of ordinary runs (phase 6).
        self.groups = GroupManager(self.bus, self.runs, lambda: self.repo)
        # Triage calls: one run's findings judged by a fresh call (phase 6).
        self.triage = TriageManager(self.bus, self.runs)

    def source_lock(self, root: Path) -> asyncio.Lock:
        """The source-file lock of the paper repo at `root` (one per resolved path)."""
        key = root.resolve()
        lock = self._source_locks.get(key)
        if lock is None:
            lock = self._source_locks[key] = asyncio.Lock()
        return lock

    async def start(self) -> None:
        await self._restart_watcher()
        await self.runs.start()
        await self.groups.start()
        if self.repo is not None:
            await self.compare.on_repo_opened(self.repo)
            await self.drafts.on_repo_opened(self.repo)
            await self.revise.on_repo_opened(self.repo)
            await self.triage.on_repo_opened(self.repo)

    async def shutdown(self) -> None:
        await self.git.shutdown()
        await self.runs.shutdown()
        await self.groups.shutdown()
        await self._stop_watcher()
        tasks = list(self._pulls.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def start_pull(self, year: int) -> tuple[str, bool]:
        """Pull the TeX Live image for `year` in the background; (image, already_running).

        Pulls are serialised by `pull_lock`. A request for an image whose pull is already
        running or queued joins it instead of queueing a second one: its client sees the
        same `docker.pull` events and one `docker.pull.finished`.
        """
        image = docker_image(year)
        running = self._pulls.get(image)
        if running is not None and not running.done():
            return image, True

        async def run() -> None:
            async with self.pull_lock:
                try:
                    await self.pull_fn(year, self.bus.publish)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pull() reports its own failures; this is a bug
                    log.exception("docker pull of %s failed", image)
                    self.bus.publish(DockerPullFinished(ok=False, error=str(exc), image=image))

        task = asyncio.create_task(run(), name=f"docker-pull:{image}")
        self._pulls[image] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._pulls.get(image) is done:
                del self._pulls[image]

        task.add_done_callback(forget)
        return image, False

    @property
    def pulls_running(self) -> list[str]:
        return [image for image, task in self._pulls.items() if not task.done()]

    async def open_repo(self, repo: Repo) -> None:
        if self.repo != repo:
            await self.runs.cancel_all("another repo was opened")
        self.repo = repo
        await self._restart_watcher()
        await self.runs.on_repo_opened(repo)
        await self.compare.on_repo_opened(repo)
        await self.drafts.on_repo_opened(repo)
        await self.revise.on_repo_opened(repo)
        await self.groups.on_repo_opened(repo)
        await self.triage.on_repo_opened(repo)

    async def _stop_watcher(self) -> None:
        if self.watcher is not None:
            watcher, self.watcher = self.watcher, None
            await watcher.stop()

    async def _restart_watcher(self) -> None:
        await self._stop_watcher()
        if self.watch and self.repo is not None:
            try:
                self.watcher = Watcher(self.repo.root, self.bus.publish)
            except Exception:  # watching is optional
                log.exception("could not start the file watcher")


def get_state(conn: HTTPConnection) -> AppState:
    """FastAPI dependency (works for HTTP and WebSocket routes)."""
    return conn.app.state.pb


def require_repo(conn: HTTPConnection) -> Repo:
    repo = get_state(conn).repo
    if repo is None:
        raise HTTPException(
            status_code=409,
            detail="no paper repo is open; POST /api/repo/open or /api/repo/init first",
        )
    return repo


StateDep = Annotated[AppState, Depends(get_state)]
RepoDep = Annotated[Repo, Depends(require_repo)]
