"""Real-time RCA pipeline progress — SSE, scoped per ``run_id``.

Mirrors ``demo/ui/chatops_ws.py``'s hub/lifespan/``_reset_for_tests`` idioms
(same sync→async bridge via ``loop.call_soon_threadsafe``, same
``_reset_for_tests`` discipline for hermetic tests), with one structural
difference: chatops is one long-lived global feed; this is many short-lived
per-run channels, and two concurrent RCA runs for different incidents must
never cross-talk. Every method below is keyed by ``run_id`` for that reason.

SSE over WebSocket: the stream terminates naturally when the run ends (clean,
timeout-free CI tests — push canned events including the terminal one, then
read a *finite* stream), needs no new dependency (hand-rolled over
``StreamingResponse`` — adding ``sse-starlette`` to ``pyproject.toml`` without
a committed ``uv.lock`` reddens CI, #155), and ``EventSource`` gives
auto-reconnect + ``Last-Event-ID`` resume for free. The hub itself is
transport-neutral (nothing here is SSE-specific except ``register_routes``),
so a ``/ws/rca/{run_id}`` variant is a small addition later if ever needed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from agents.rca_agent.progress import ProgressSink, StageEvent

logger = logging.getLogger(__name__)

# Per-run history ring: late subscribers (a page opened after the run started)
# replay everything so far rather than joining mid-stream blind.
RUN_HISTORY_MAX = 200

# Bounds total memory: a demo left running for days must not accumulate one
# channel per RCA run forever. Eviction is LRU by last touch (push OR
# subscribe), which in practice also acts as a soft TTL — a channel nobody
# has read from or written to in a while is exactly the one that gets evicted
# first. A hard time-based sweep is a documented follow-up if this proves
# insufficient, not built now (POC scope discipline).
MAX_RUNS = 64

# Per-client queue cap — a slow consumer drops rather than buffering
# indefinitely, same posture as the chatops hub.
CLIENT_QUEUE_MAX = 200

_TERMINAL_STAGES = frozenset({"complete", "failed"})


class _RunChannel:
    __slots__ = ("history", "listeners", "terminal")

    def __init__(self) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=RUN_HISTORY_MAX)
        self.listeners: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.terminal = False


class RcaProgressHub:
    """Thread-safe per-run history ring + asyncio fan-out."""

    def __init__(self) -> None:
        self._runs: OrderedDict[str, _RunChannel] = OrderedDict()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _touch(self, run_id: str) -> _RunChannel:
        """Get-or-create a run's channel; marks it most-recently-used and
        evicts the least-recently-used run once over ``MAX_RUNS``.

        Must be called with ``self._lock`` held.
        """
        ch = self._runs.get(run_id)
        if ch is None:
            ch = _RunChannel()
            self._runs[run_id] = ch
            while len(self._runs) > MAX_RUNS:
                self._runs.popitem(last=False)
        else:
            self._runs.move_to_end(run_id)
        return ch

    def push(self, run_id: str, record: dict[str, Any]) -> None:
        """Add a record to ``run_id``'s history and notify its subscribers.

        Safe to call from any thread — ``agent.py``'s ``analyze()`` runs
        inside ``asyncio.to_thread``, so every emit crosses this boundary.
        """
        with self._lock:
            ch = self._touch(run_id)
            ch.history.append(record)
            if record.get("stage") in _TERMINAL_STAGES:
                ch.terminal = True
            listeners = list(ch.listeners)
            loop = self._loop
        if loop is None:
            # Server still booting; the record is preserved in history for
            # the first subscriber to replay once the loop is attached.
            return
        for q in listeners:
            loop.call_soon_threadsafe(_safe_put, q, record)

    def history(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            ch = self._runs.get(run_id)
            if ch is None:
                return []
            return [r for r in ch.history if r.get("seq", 0) > after_seq]

    def is_terminal(self, run_id: str) -> bool:
        with self._lock:
            ch = self._runs.get(run_id)
            return bool(ch and ch.terminal)

    def next_seq(self, run_id: str) -> int:
        """The next sequence number for ``run_id``, for a caller (the HTTP
        layer's terminal complete/failed push) that pushes a record without
        going through a ``RunProgress``'s own counter."""
        with self._lock:
            ch = self._runs.get(run_id)
            if not ch or not ch.history:
                return 1
            return max((r.get("seq", 0) for r in ch.history), default=0) + 1

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        with self._lock:
            ch = self._touch(run_id)
            ch.listeners.add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            ch = self._runs.get(run_id)
            if ch is not None:
                ch.listeners.discard(q)

    def _reset_for_tests(self) -> None:
        """Clear every run channel around a test — same discipline as
        ``chatops_ws._ChatOpsHub._reset_for_tests``: this hub is a
        process-global singleton, so a run pushed by one test otherwise
        leaks into the next test's replay."""
        with self._lock:
            self._runs.clear()


def _safe_put(q: asyncio.Queue[dict[str, Any] | None], record: dict[str, Any] | None) -> None:
    try:
        q.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning("rca progress: client queue full; dropping event %r", record)


class HubSink:
    """``ProgressSink`` that forwards every event to one run's hub channel."""

    def __init__(self, hub: RcaProgressHub, run_id: str) -> None:
        self._hub = hub
        self._run_id = run_id

    def emit(self, event: StageEvent) -> None:
        self._hub.push(self._run_id, event.model_dump(mode="json"))


_HUB = RcaProgressHub()


def get_hub() -> RcaProgressHub:
    """Test/route seam — production emission goes through a ``HubSink``."""
    return _HUB


def bootstrap_rca_progress() -> None:
    """Attach the running asyncio loop, exactly like
    ``chatops_ws.bootstrap_websocket_adapter`` — must run from inside the
    FastAPI lifespan so ``asyncio.get_running_loop()`` resolves to the
    server's loop."""
    _HUB.attach_loop(asyncio.get_running_loop())


def new_run_id() -> str:
    return str(uuid.uuid4())


def make_sink(run_id: str | None) -> ProgressSink | None:
    """``None`` in, ``None`` out — the common case (no ``run_id`` on the
    request) costs nothing; ``analyze()`` defaults to a no-op sink either
    way, so this is purely a convenience for ``server.py``'s route body."""
    return HubSink(_HUB, run_id) if run_id else None


def register_routes(app: FastAPI) -> None:
    heartbeat_s = _env_float("AIOPS_RCA_STREAM_HEARTBEAT", 15.0)
    idle_timeout_s = _env_float("AIOPS_RCA_STREAM_IDLE_TIMEOUT", 60.0)

    @app.get("/api/rca/stream/{run_id}")
    async def rca_stream(run_id: str, request: Request) -> StreamingResponse:
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="run_id must be a UUID") from None

        after_seq = 0
        last_event_id = request.headers.get("last-event-id") or request.query_params.get("after")
        if last_event_id:
            try:
                after_seq = int(last_event_id)
            except ValueError:
                after_seq = 0

        async def frames() -> AsyncIterator[str]:
            q = _HUB.subscribe(run_id)
            try:
                for hist_record in _HUB.history(run_id, after_seq=after_seq):
                    yield _sse_frame(hist_record)
                    if hist_record.get("stage") in _TERMINAL_STAGES:
                        return
                last_activity = time.monotonic()
                while True:
                    try:
                        record: dict[str, Any] | None = await asyncio.wait_for(
                            q.get(), timeout=heartbeat_s
                        )
                    except TimeoutError:
                        if time.monotonic() - last_activity >= idle_timeout_s:
                            yield "event: timeout\ndata: {}\n\n"
                            return
                        yield ": ping\n\n"
                        continue
                    if record is None:
                        continue
                    last_activity = time.monotonic()
                    yield _sse_frame(record)
                    if record.get("stage") in _TERMINAL_STAGES:
                        return
            finally:
                _HUB.unsubscribe(run_id, q)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _sse_frame(record: dict[str, Any]) -> str:
    import json

    seq = record.get("seq", 0)
    stage = record.get("stage", "message")
    return f"id: {seq}\nevent: {stage}\ndata: {json.dumps(record)}\n\n"


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default
