"""WebSocket sink for the chatops seam (D2 — primary).

Wires three things together:

1. ``WebSocketChatOpsAdapter`` — a ``ChatOpsAdapter`` that the chatops client
   fans messages into. ``send()`` is synchronous (the client calls it inline
   from any context) so the adapter only does a thread-safe handoff into the
   hub; no I/O happens here.
2. ``_ChatOpsHub`` — the async broadcaster. Keeps a bounded history ring for
   late joiners and one ``asyncio.Queue`` per connected client. Bridges the
   sync→async boundary via ``loop.call_soon_threadsafe``.
3. ``/ws/chatops`` — the FastAPI WebSocket route. On connect it replays
   recent history, then streams new messages as they arrive.

Why a hub instead of the simpler "poll-and-broadcast" pattern used by
``/ws/alerts``: chatops is event-driven, not periodic. Polling would
introduce up-to-N-seconds latency and waste cycles when nothing is
happening. A hub lets us push within milliseconds of an agent firing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from aiops.tools.chatops import (
    ChatMessage,
    to_record,
)
from aiops.tools.chatops import (
    get_client as get_chatops_client,
)

logger = logging.getLogger(__name__)

# Per-client queue cap. If a client falls this far behind we drop it rather
# than buffer indefinitely — slow consumers must not OOM the server.
CLIENT_QUEUE_MAX = 500

# History ring: most recent N messages replayed to late joiners. A new
# dashboard tab opened mid-incident must immediately see what fired before
# the page loaded.
HISTORY_MAX = 200


class _ChatOpsHub:
    """Thread-safe history ring + asyncio fan-out to all connected clients."""

    def __init__(self) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def push(self, record: dict[str, Any]) -> None:
        """Add a record to history and notify every connected client.

        Safe to call from any thread; queue puts are scheduled on the loop.
        """
        with self._lock:
            self._history.append(record)
            listeners = list(self._listeners)
            loop = self._loop
        if loop is None:
            # Server still booting; record is preserved in history for the
            # first WS client to replay once the loop is attached.
            return
        for q in listeners:
            loop.call_soon_threadsafe(_safe_put, q, record)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)

    def register(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._listeners.add(q)

    def unregister(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._listeners.discard(q)

    def _reset_for_tests(self) -> None:
        """Clear the history ring + listeners so a test starts from an empty hub.

        The hub is a process-global singleton, so a chatops message emitted by
        an earlier test (a reactive-flow / triage / chained-demo test that
        routes a notification) lingers in ``_history`` and leaks into the next
        test's replay. ``tests/conftest.py`` calls this around every test, same
        discipline as the per-test SQLite / gate / Jaeger-circuit isolation.
        """
        with self._lock:
            self._history.clear()
            self._listeners.clear()


def _safe_put(q: asyncio.Queue[dict[str, Any]], record: dict[str, Any]) -> None:
    """``Queue.put_nowait`` but never raises — drop on full so one slow client
    cannot wedge the broadcaster."""
    try:
        q.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning("chatops: client queue full; dropping message %r", record.get("title"))


class WebSocketChatOpsAdapter:
    """ChatOpsAdapter that hands every message to the hub."""

    name = "websocket"

    def __init__(self, hub: _ChatOpsHub) -> None:
        self._hub = hub

    def send(self, msg: ChatMessage) -> None:
        self._hub.push(to_record(msg))


_HUB = _ChatOpsHub()


def get_hub() -> _ChatOpsHub:
    """Test seam — production code should not call this directly."""
    return _HUB


def bootstrap_websocket_adapter() -> None:
    """Attach the running asyncio loop to the hub and register the WS adapter.

    Must be called from inside the asyncio event loop (i.e. from the FastAPI
    lifespan context manager) so ``asyncio.get_running_loop()`` resolves to the
    server's loop.

    Idempotent: the loop is re-attached on every call (each app/lifespan owns a
    different loop), but the ``WebSocketChatOpsAdapter`` is registered only
    once. Without that guard, repeated lifespans — multiple ``TestClient``
    contexts in a test session, or a hot reload — pile up duplicate adapters on
    the process-global chatops client, so every message fans into the hub N
    times and the ``/ws/chatops`` replay/stream order breaks.
    """
    _HUB.attach_loop(asyncio.get_running_loop())
    client = get_chatops_client()
    if any(isinstance(a, WebSocketChatOpsAdapter) for a in client.adapters):
        return
    client.register(WebSocketChatOpsAdapter(_HUB))
    logger.info("chatops: registered websocket adapter (/ws/chatops)")


def register_routes(app: FastAPI) -> None:
    """Wire the WebSocket route onto the given FastAPI app.

    The companion ``bootstrap_websocket_adapter()`` runs from the parent app's
    lifespan to perform the startup-time wiring (loop attach + adapter
    registration).
    """

    @app.websocket("/ws/chatops")
    async def ws_chatops(ws: WebSocket) -> None:
        await ws.accept()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        _HUB.register(q)
        try:
            # Replay buffered history so a tab opened mid-incident is not blank.
            for rec in _HUB.history():
                await ws.send_json(rec)
            while True:
                rec = await q.get()
                await ws.send_json(rec)
        except WebSocketDisconnect:
            pass
        finally:
            _HUB.unregister(q)
