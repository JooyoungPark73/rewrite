"""WS /ws/events: pushes every event-bus event to the client as JSON."""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.state import get_state

router = APIRouter()

LOCAL_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "::1"}


def origin_allowed(origin: str | None) -> bool:
    """Browsers do not apply CORS to WebSockets, so reject pages from other origins."""
    if not origin:
        return True  # non-browser clients (curl, tests) send no Origin
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in LOCAL_ORIGIN_HOSTS


@router.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    if not origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    state = get_state(websocket)
    await websocket.accept()
    queue = state.bus.subscribe()

    async def pump() -> None:
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    async def drain() -> None:
        # Incoming messages are ignored; this only notices the client going away.
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

    tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
        state.bus.unsubscribe(queue)
