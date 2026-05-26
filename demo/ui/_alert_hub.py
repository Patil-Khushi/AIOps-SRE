"""WebSocket broadcaster for ``/ws/alerts`` (DEMO-16 / #68).

A single broadcaster task polls a caller-supplied ``frame_fn`` every
``AIOPS_ALERT_BROADCAST_INTERVAL`` seconds and fans the result out to
every connected client.  Cheaper than each browser tab hammering
Prometheus directly.

Why a hub instead of per-client polling: the polled work — currently
``live_alerts()``, which hits Prometheus via the registry — is
identical across clients.  Doing it once per interval and broadcasting
the JSON payload keeps the cost flat regardless of how many tabs the
demo audience opens.

Why this lives next to ``chatops_ws.py`` and not under ``aiops/``:
both files own the WebSocket plumbing for one of the dashboard's
two real-time feeds.  Putting them side-by-side makes the layering
obvious — the dashboard talks to ``demo/ui/`` only.  The hub itself
is generic (no Prometheus / alert-specific code), so when the next
real-time panel needs the same pattern it lifts this class as-is.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


# Default seconds between broadcaster ticks. Lower for snappier demo UX;
# higher to reduce load on the Prometheus port-forward. Override via
# AIOPS_ALERT_BROADCAST_INTERVAL.
_DEFAULT_INTERVAL_SECONDS = 5.0


FrameFn = Callable[[], dict[str, Any]]


class _AlertHub:
    """Periodic-poll WebSocket broadcaster.

    Keeps a set of connected clients and a single broadcaster task.
    When the first client connects the task spins up; when the last
    client disconnects the task returns.  ``frame_fn`` is called once
    per tick on a worker thread (it's allowed to block on I/O — e.g.
    a synchronous Prometheus call).
    """

    def __init__(self, frame_fn: FrameFn, *, interval_seconds: float | None = None) -> None:
        self._frame_fn = frame_fn
        if interval_seconds is None:
            interval_seconds = float(
                os.environ.get("AIOPS_ALERT_BROADCAST_INTERVAL", _DEFAULT_INTERVAL_SECONDS)
            )
        self._interval = interval_seconds
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._broadcast_loop())

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def collect_frame(self) -> dict[str, Any]:
        """Run ``frame_fn`` on a worker thread and return the payload.

        Exposed so the WS route can send an initial frame on connect
        rather than waiting up to ``interval_seconds`` for the first tick.
        """
        return await asyncio.to_thread(self._frame_fn)

    async def _broadcast_loop(self) -> None:
        while True:
            async with self._lock:
                if not self._clients:
                    return  # last client gone; stop the task
                clients = list(self._clients)
            payload = await self.collect_frame()
            stale: list[WebSocket] = []
            for ws in clients:
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            if stale:
                async with self._lock:
                    for ws in stale:
                        self._clients.discard(ws)
            await asyncio.sleep(self._interval)


def register_routes(app: FastAPI, frame_fn: FrameFn) -> _AlertHub:
    """Wire ``/ws/alerts`` onto the given FastAPI app.

    Returns the hub instance so callers (tests, lifespan teardown) can
    introspect it.  Each call constructs a fresh hub; production code
    should call this exactly once per app.
    """
    hub = _AlertHub(frame_fn)

    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            # Send one frame immediately so the UI doesn't sit empty for
            # ``interval`` seconds before the first broadcaster tick.
            await ws.send_json(await hub.collect_frame())
            while True:
                await ws.receive_text()  # client keepalive pings; ignore content
        except WebSocketDisconnect:
            pass
        finally:
            await hub.disconnect(ws)

    return hub
