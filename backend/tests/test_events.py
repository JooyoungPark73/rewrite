"""Event bus and the repo file watcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

from watchfiles import Change

from app.events import (
    CompileStarted,
    EventBus,
    FileChanged,
    TexFilter,
    Watcher,
    changes_to_events,
)


async def test_bus_broadcasts_to_all_subscribers() -> None:
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish(CompileStarted(main="main.tex"))
    assert a.get_nowait() == b.get_nowait() == {"type": "compile.started", "main": "main.tex"}
    bus.unsubscribe(a)
    bus.publish({"type": "x"})
    assert a.empty() and b.get_nowait() == {"type": "x"}


async def test_slow_subscriber_drops_oldest() -> None:
    bus = EventBus(queue_size=2)
    q = bus.subscribe()
    for i in range(5):
        bus.publish({"n": i})
    assert [q.get_nowait()["n"] for _ in range(2)] == [3, 4]


def test_filter() -> None:
    f = TexFilter()
    assert f(Change.modified, "/r/sections/intro.tex")
    assert f(Change.modified, "/r/refs.bib")
    assert not f(Change.modified, "/r/.rewrite/build/main.tex")
    assert not f(Change.modified, "/r/.git/x.tex")
    assert not f(Change.modified, "/r/main.pdf")
    assert not f(Change.added, "/r/.main.tex.abc123.tmp")


def test_changes_to_events(tmp_path: Path) -> None:
    (tmp_path / "a.tex").write_text("x")
    (tmp_path / "b.tex").write_text("x")
    changes = {
        (Change.deleted, str(tmp_path / "a.tex")),  # atomic replace: file exists again
        (Change.added, str(tmp_path / "b.tex")),
        (Change.deleted, str(tmp_path / "gone.tex")),
        (Change.modified, str(tmp_path / ".rewrite" / "x.tex")),
    }
    events = {e.path: e for e in changes_to_events(tmp_path, changes)}
    assert set(events) == {"a.tex", "b.tex", "gone.tex"}
    assert events["a.tex"].change == "modified" and events["a.tex"].mtime is not None
    assert events["b.tex"].change == "added"
    assert events["gone.tex"] == FileChanged(path="gone.tex", change="deleted", mtime=None)


async def test_watcher_reports_tex_changes(tmp_path: Path) -> None:
    (tmp_path / ".rewrite").mkdir()
    (tmp_path / "sections").mkdir()
    seen: asyncio.Queue[FileChanged] = asyncio.Queue()
    watcher = Watcher(tmp_path, seen.put_nowait)
    try:
        await asyncio.sleep(0.3)  # let the watcher start
        (tmp_path / ".rewrite" / "ignored.tex").write_text("x")
        (tmp_path / "notes.txt").write_text("x")
        (tmp_path / "sections" / "intro.tex").write_text("hello")
        event = await asyncio.wait_for(seen.get(), timeout=10)
        assert event.path == "sections/intro.tex"
        assert event.change in ("added", "modified")
        await asyncio.sleep(0.4)
        assert seen.empty()
    finally:
        await asyncio.wait_for(watcher.stop(), timeout=10)


def test_file_changed_reaches_websocket_with_watcher_enabled(initialized_repo: Path) -> None:
    """Lifespan starts the watcher, /api/repo/open restarts it, and shutdown does not hang."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(initialized_repo, watch=True, frontend_dist=None)
    started = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.pb
        assert state.watcher is not None
        reopened = client.post("/api/repo/open", json={"path": str(initialized_repo)})
        assert reopened.status_code == 200 and state.watcher is not None
        with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
            time.sleep(0.5)  # let the restarted watcher settle
            (initialized_repo / "sections" / "intro.tex").write_text("changed\n")
            # if no event arrives, unblock receive_json instead of hanging the suite
            timer = threading.Timer(
                15, lambda: client.portal.call(state.bus.publish, {"type": "timeout"})
            )
            timer.start()
            try:
                event = ws.receive_json()
            finally:
                timer.cancel()
        assert event["type"] == "file.changed", event
        assert event["path"] == "sections/intro.tex"
        assert event["change"] in ("added", "modified") and event["mtime"] > 0
    assert state.watcher is None
    assert time.monotonic() - started < 20


# --------------------------------------------------------------------------- phase 5: spec file


def test_filter_watches_the_spec_file_at_the_root_only(tmp_path: Path) -> None:
    f = TexFilter(tmp_path)
    assert f(Change.modified, str(tmp_path / "paper.json"))
    assert not f(Change.modified, str(tmp_path / "sub" / "paper.json"))
    assert not f(Change.modified, str(tmp_path / ".rewrite" / "paper.json"))
    assert not f(Change.added, str(tmp_path / ".paper.json.abc.tmp"))
    assert f(Change.modified, str(tmp_path / "a.tex"))
    assert not TexFilter()(Change.modified, str(tmp_path / "paper.json"))


def test_spec_file_is_not_a_file_changed_event(tmp_path: Path) -> None:
    (tmp_path / "paper.json").write_text("{}")
    changes = {(Change.modified, str(tmp_path / "paper.json"))}
    assert changes_to_events(tmp_path, changes) == []


def test_spec_change_event_ignores_the_apps_own_writes(tmp_path: Path) -> None:
    from app import specwatch
    from app.events import SpecChanged, spec_change_event

    spec = tmp_path / "paper.json"
    batch = {(Change.modified, str(spec))}
    specwatch.remember(tmp_path, '{"own": 1}\n')
    spec.write_text('{"own": 1}\n')
    assert spec_change_event(tmp_path, batch) is None  # the editor's own save
    spec.write_text('{"external": 1}\n')
    assert spec_change_event(tmp_path, batch) == SpecChanged()
    assert spec_change_event(tmp_path, batch) is None  # announced once
    assert spec_change_event(tmp_path, {(Change.modified, str(tmp_path / "a.tex"))}) is None
    specwatch.forget(tmp_path)


async def test_watcher_announces_external_spec_edits(tmp_path: Path) -> None:
    from app import specwatch
    from app.events import SpecChanged

    (tmp_path / "paper.json").write_text('{"v": 0}\n')
    seen: asyncio.Queue[FileChanged | SpecChanged] = asyncio.Queue()
    watcher = Watcher(tmp_path, seen.put_nowait)
    try:
        await asyncio.sleep(0.3)
        (tmp_path / "paper.json").write_text('{"v": 1}\n')  # e.g. git checkout
        event = await asyncio.wait_for(seen.get(), timeout=10)
        assert event == SpecChanged()
        specwatch.remember(tmp_path, '{"v": 2}\n')  # what write_spec does before writing
        (tmp_path / "paper.json").write_text('{"v": 2}\n')
        (tmp_path / "x.tex").write_text("x")
        event = await asyncio.wait_for(seen.get(), timeout=10)
        assert isinstance(event, FileChanged) and event.path == "x.tex"
        await asyncio.sleep(0.4)
        assert seen.empty()
    finally:
        await asyncio.wait_for(watcher.stop(), timeout=10)
        specwatch.forget(tmp_path)


def test_spec_changed_reaches_websocket_for_external_edits_only(initialized_repo: Path) -> None:
    import threading
    import time

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(initialized_repo, watch=True, frontend_dist=None)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.pb
        with client.websocket_connect("ws://127.0.0.1/ws/events") as ws:
            time.sleep(0.5)
            spec = client.get("/api/spec").json()["spec"]
            spec["message"]["says"] = "edited in the app"
            assert client.put("/api/spec", json={"spec": spec}).status_code == 200
            assert ws.receive_json() == {"type": "spec.changed"}  # from the API
            time.sleep(0.8)  # the watcher sees the write but must not echo it
            client.portal.call(state.bus.publish, {"type": "marker"})
            assert ws.receive_json() == {"type": "marker"}  # nothing in between
            text = (initialized_repo / "paper.json").read_text()
            (initialized_repo / "paper.json").write_text(
                text.replace("edited in the app", "edited by git")
            )
            timer = threading.Timer(
                15, lambda: client.portal.call(state.bus.publish, {"type": "timeout"})
            )
            timer.start()
            try:
                event = ws.receive_json()
            finally:
                timer.cancel()
        assert event == {"type": "spec.changed"}
        assert client.get("/api/spec").json()["spec"]["message"]["says"] == "edited by git"
