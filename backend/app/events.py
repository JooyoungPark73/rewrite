"""In-process event bus (broadcast to every WebSocket client) and the repo file watcher.

Events are pydantic models serialised to JSON. Phase 1 emits:
- `compile.started`  {type, main}
- `compile.finished` {type, result: CompileResult}
- `file.changed`     {type, path, change, mtime}  (one event per changed .tex/.bib file)
Phase 2 adds, for POST /api/compile/docker/pull:
- `docker.pull`          {type, line, image}  (one per line of `docker pull` output)
- `docker.pull.finished` {type, ok, error, image}
Phase 3 adds:
- `spec.changed`         {type}  (after every write of the spec file by app/spec.py)
Phase 4 adds (app/runs.py; `run` is the Run object of GET /api/runs):
- `run.started`          {type, run}  (the CLI process is starting; status "running")
- `run.event`            {type, run_id, kind: "text"|"status", text}  (≤ 10 per second per run)
- `run.finished`         {type, run}  (status "done" or "cached")
- `run.failed`           {type, run}  (status "failed" or "cancelled")
- `findings.updated`     {type, run_id, file}  (new findings, a state change, or re-anchoring)
Phase 5 adds:
- `spec.changed`         also when the spec file changes outside the app (git pull, another
                         editor): the watcher compares its content hash with the app's last
                         write (app/specwatch.py)
- `git.output`           {type, op: "push"|"pull", line}  (one per output line of git)
- `git.finished`         {type, op, ok, error, changed}  (`changed`: paths a pull changed)
- `compare.finished`     {type, comparison}  (a blind A/B comparison ended; mapping is null)
Phase 6 adds:
- `group.updated`        {type, group}  (a "run both" / fan-out group: a member run started,
                         finished, failed or was cancelled, or the group was created)
- `triage.finished`      {type, triage}  (a triage call ended: done or failed)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from watchfiles import Change, DefaultFilter, awatch

from app import specwatch
from app.compiler import CompileResult
from app.repo import PAPER_JSON_NAME, REWRITE_DIR

log = logging.getLogger(__name__)

WATCHED_SUFFIXES = (".tex", ".bib")


class CompileStarted(BaseModel):
    type: Literal["compile.started"] = "compile.started"
    main: str


class CompileFinished(BaseModel):
    type: Literal["compile.finished"] = "compile.finished"
    result: CompileResult


class FileChanged(BaseModel):
    type: Literal["file.changed"] = "file.changed"
    path: str  # repo-relative
    change: Literal["added", "modified", "deleted"]
    mtime: float | None  # null when deleted; compare with the mtime a save returned


class DockerPull(BaseModel):
    type: Literal["docker.pull"] = "docker.pull"
    line: str
    image: str = ""  # the image being pulled, e.g. texlive/texlive:TL2024-historic


class DockerPullFinished(BaseModel):
    type: Literal["docker.pull.finished"] = "docker.pull.finished"
    ok: bool
    error: str | None = None  # last output line (or a reason) when ok is false
    image: str = ""


class SpecChanged(BaseModel):
    """The spec file was rewritten by the spec editor; clients refetch GET /api/spec."""

    type: Literal["spec.changed"] = "spec.changed"


class RunStarted(BaseModel):
    type: Literal["run.started"] = "run.started"
    run: dict[str, Any]


class RunEventMessage(BaseModel):
    type: Literal["run.event"] = "run.event"
    run_id: str
    kind: Literal["text", "status"]
    text: str


class RunFinished(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run: dict[str, Any]


class RunFailed(BaseModel):
    type: Literal["run.failed"] = "run.failed"
    run: dict[str, Any]


class FindingsUpdated(BaseModel):
    type: Literal["findings.updated"] = "findings.updated"
    run_id: str | None = None  # the run whose findings changed, or null for several
    file: str | None = None  # the file whose findings were re-anchored, or null


GitOp = Literal["push", "pull"]


class GitOutput(BaseModel):
    type: Literal["git.output"] = "git.output"
    op: GitOp
    line: str


class GitFinished(BaseModel):
    type: Literal["git.finished"] = "git.finished"
    op: GitOp
    ok: bool
    error: str | None = None  # git's last error line (or a reason) when ok is false
    changed: list[str] = []  # + pull: repo-relative paths the pull changed


class DraftFinished(BaseModel):
    """A draft (phase 7) reached done or failed; `draft` is the Draft row."""

    type: Literal["draft.finished"] = "draft.finished"
    draft: dict[str, Any]


class RevisionFinished(BaseModel):
    """A rewrite proposal (phase 8, Revise) reached done or failed; `revision` is the row."""

    type: Literal["revision.finished"] = "revision.finished"
    revision: dict[str, Any]


class CompareFinished(BaseModel):
    """A blind A/B comparison reached done or failed. `comparison.mapping` is always null
    here: the mapping is revealed only by GET /api/compare/{id}?reveal=true."""

    type: Literal["compare.finished"] = "compare.finished"
    comparison: dict[str, Any]


class GroupUpdated(BaseModel):
    """A run group changed (app/groups.py): `group` is the RunGroup of GET /api/groups."""

    type: Literal["group.updated"] = "group.updated"
    group: dict[str, Any]


class TriageFinished(BaseModel):
    """A triage call reached done or failed (app/triage.py); `triage` is the Triage of
    GET /api/runs/{id}/triage. The judged run also gets a `findings.updated`."""

    type: Literal["triage.finished"] = "triage.finished"
    triage: dict[str, Any]


Event = (
    CompileStarted
    | CompileFinished
    | FileChanged
    | DockerPull
    | DockerPullFinished
    | SpecChanged
    | RunStarted
    | RunEventMessage
    | RunFinished
    | RunFailed
    | FindingsUpdated
    | GitOutput
    | GitFinished
    | CompareFinished
    | DraftFinished
    | RevisionFinished
    | GroupUpdated
    | TriageFinished
)


class EventBus:
    """Fan-out of events to subscriber queues. Slow subscribers lose their oldest events."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def publish(self, event: Event | dict[str, Any]) -> None:
        """Deliver to every subscriber. Must be called from the event loop thread."""
        payload = event.model_dump(mode="json") if isinstance(event, BaseModel) else event
        for q in list(self._queues):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(payload)


class TexFilter(DefaultFilter):
    """Only .tex and .bib files, never anything under .rewrite/ or .git/; with `root`,
    also the spec file at the repo root (phase 5)."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(ignore_dirs=(*DefaultFilter.ignore_dirs, REWRITE_DIR, ".git"))
        self.spec_path = (root / PAPER_JSON_NAME) if root is not None else None

    def is_spec(self, path: str) -> bool:
        if self.spec_path is None or Path(path).name != PAPER_JSON_NAME:
            return False
        return is_spec_file(self.spec_path.parent, path)

    def __call__(self, change: Change, path: str) -> bool:
        if self.is_spec(path):
            return True
        return path.endswith(WATCHED_SUFFIXES) and super().__call__(change, path)


def is_spec_file(root: Path, path: str | Path) -> bool:
    """`path` is the spec file at the root of the repo at `root`."""
    candidate = Path(path)
    if candidate.name != PAPER_JSON_NAME:
        return False
    try:
        return candidate.parent.resolve() == root.resolve()
    except OSError:
        return False


def changes_to_events(root: Path, changes: set[tuple[Change, str]]) -> list[FileChanged]:
    """Collapse one watchfiles batch into one event per path, using the file's final state."""
    root_resolved = root.resolve()
    by_path: dict[str, Change] = {}
    for change, raw in sorted(changes, key=lambda c: c[1]):
        by_path[raw] = change
    events: list[FileChanged] = []
    for raw, change in by_path.items():
        path = Path(raw)
        if is_spec_file(root, path):
            continue  # announced as spec.changed by spec_change_event
        try:
            rel = path.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
        if rel.split("/", 1)[0] in (REWRITE_DIR, ".git"):
            continue
        if path.is_file():
            kind: Literal["added", "modified", "deleted"] = (
                "added" if change == Change.added else "modified"
            )
            events.append(FileChanged(path=rel, change=kind, mtime=path.stat().st_mtime))
        else:
            events.append(FileChanged(path=rel, change="deleted", mtime=None))
    return events


def spec_change_event(root: Path, changes: set[tuple[Change, str]]) -> SpecChanged | None:
    """`spec.changed` when the batch touches the spec file and its content differs from what
    the app wrote or announced last (so the editor's own saves are not echoed)."""
    if not any(is_spec_file(root, raw) for _, raw in changes):
        return None
    return SpecChanged() if specwatch.changed_externally(root) else None


async def watch_repo(
    root: Path,
    publish: Callable[[FileChanged | SpecChanged], None],
    stop_event: asyncio.Event,
    *,
    debounce_ms: int = 200,
) -> None:
    """Publish `file.changed` events for the repo, and `spec.changed` for external edits of
    the spec file, until `stop_event` is set."""
    try:
        specwatch.prime(root)
    except Exception:  # an unreadable spec file is announced on its first change
        log.exception("could not read the spec file of %s", root)
    try:
        async for changes in awatch(
            root,
            watch_filter=TexFilter(root),
            stop_event=stop_event,
            debounce=debounce_ms,
            recursive=True,
        ):
            for event in changes_to_events(root, changes):
                publish(event)
            spec_event = spec_change_event(root, changes)
            if spec_event is not None:
                publish(spec_event)
    except asyncio.CancelledError:
        raise
    except Exception:  # watching is best-effort; the editor works without it
        log.exception("file watcher for %s stopped", root)


class Watcher:
    """A running watch_repo task that can be stopped cleanly."""

    def __init__(self, root: Path, publish: Callable[[FileChanged | SpecChanged], None]) -> None:
        self.root = root
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] = asyncio.create_task(
            watch_repo(root, publish, self._stop), name=f"watch:{root}"
        )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
