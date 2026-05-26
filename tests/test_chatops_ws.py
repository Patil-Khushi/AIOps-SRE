"""Tests for the chatops WebSocket sink (D2).

We test ``_ChatOpsHub`` and ``WebSocketChatOpsAdapter`` in isolation, plus
an end-to-end round-trip through the FastAPI ``TestClient`` to confirm
``/ws/chatops`` actually delivers messages produced via the chatops seam.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiops.tools.chatops import ChatMessage, Severity, to_record
from demo.ui.chatops_ws import (
    WebSocketChatOpsAdapter,
    _ChatOpsHub,
    bootstrap_websocket_adapter,
    register_routes,
)


def _msg(title: str = "test") -> ChatMessage:
    return ChatMessage(
        channel="ops",
        severity=Severity.P2,
        title=title,
        body="body",
        timestamp=datetime(2026, 5, 13, 11, 30, tzinfo=UTC),
    )


# ─── hub unit tests ────────────────────────────────────────────────────────


def test_push_without_loop_only_buffers_to_history():
    hub = _ChatOpsHub()
    hub.push({"title": "early"})

    assert hub.history() == [{"title": "early"}]


def test_history_is_capped_at_history_max(monkeypatch):
    # Verify the ring eventually evicts old records. Use small cap for speed.
    from demo.ui import chatops_ws as mod

    monkeypatch.setattr(mod, "HISTORY_MAX", 3)
    hub = mod._ChatOpsHub()
    for i in range(5):
        hub.push({"title": f"m{i}"})

    titles = [r["title"] for r in hub.history()]
    assert titles == ["m2", "m3", "m4"]


def test_register_and_unregister_listener():
    hub = _ChatOpsHub()
    q: asyncio.Queue = asyncio.Queue()
    hub.register(q)
    assert q in hub._listeners
    hub.unregister(q)
    assert q not in hub._listeners


@pytest.mark.asyncio
async def test_push_with_loop_fans_out_to_each_listener():
    hub = _ChatOpsHub()
    hub.attach_loop(asyncio.get_running_loop())
    qa: asyncio.Queue = asyncio.Queue()
    qb: asyncio.Queue = asyncio.Queue()
    hub.register(qa)
    hub.register(qb)

    hub.push({"title": "fan-out"})
    # call_soon_threadsafe lands on the next tick — yield once.
    await asyncio.sleep(0)

    assert qa.get_nowait() == {"title": "fan-out"}
    assert qb.get_nowait() == {"title": "fan-out"}


@pytest.mark.asyncio
async def test_slow_consumer_does_not_block_others():
    from demo.ui import chatops_ws as mod

    hub = _ChatOpsHub()
    hub.attach_loop(asyncio.get_running_loop())
    slow: asyncio.Queue = asyncio.Queue(maxsize=1)
    fast: asyncio.Queue = asyncio.Queue()
    hub.register(slow)
    hub.register(fast)

    # Fill the slow queue so further puts to it would block; verify fast
    # listener still receives everything.
    for i in range(5):
        hub.push({"title": f"m{i}"})
    await asyncio.sleep(0)

    drained = []
    while not fast.empty():
        drained.append(fast.get_nowait())
    assert [r["title"] for r in drained] == [f"m{i}" for i in range(5)]
    # Slow queue accepted exactly its cap and dropped the rest.
    assert slow.qsize() == 1
    _ = mod  # silence unused (keeps the import that proves the module loaded)


# ─── adapter unit test ─────────────────────────────────────────────────────


def test_adapter_serializes_via_shared_to_record():
    hub = _ChatOpsHub()
    adapter = WebSocketChatOpsAdapter(hub)
    msg = _msg("router-says-page")

    adapter.send(msg)

    history = hub.history()
    assert history == [to_record(msg)]


# ─── end-to-end via FastAPI TestClient ─────────────────────────────────────


def test_websocket_endpoint_replays_history_and_streams_new_messages():
    # Compose register_routes + bootstrap_websocket_adapter the same way
    # demo/ui/server.py does in its production lifespan: the route wiring
    # happens at construction, the loop-attach + adapter-register happens
    # inside the running asyncio loop via the lifespan context manager.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        bootstrap_websocket_adapter()
        yield

    app = FastAPI(lifespan=_lifespan)
    register_routes(app)

    with TestClient(app) as client:
        # The lifespan attached the loop and registered the adapter against
        # the *real* chatops client singleton. Push directly through that
        # adapter to exercise the same path agents will use.
        from aiops.tools.chatops import get_client

        chat_client = get_client()
        chat_client.send(_msg("first"))

        with client.websocket_connect("/ws/chatops") as ws:
            # History replay
            replayed = ws.receive_json()
            assert replayed["title"] == "first"

            # New message after connection
            chat_client.send(_msg("second"))
            streamed = ws.receive_json()
            assert streamed["title"] == "second"
            assert streamed["severity"] == "p2"
            assert streamed["channel"] == "ops"
