"""HITL-3 (#103) — bounded ``_HITL_OUTCOMES`` + pooled demo agent threads.

The demo agent path (``POST /api/demo/auto-heal/restart``) used to leak two
ways:

1. ``_HITL_OUTCOMES`` grew unbounded — one entry per request, never evicted.
2. Each request spawned a fresh ``threading.Thread(daemon=True)``; nothing
   capped concurrency, so a fast-clicking presenter or a misbehaving client
   could rack up many in-flight threads each holding a 900s registry-wait.

These tests verify the fix: a ``_BoundedOutcomeStore`` capped at 100 entries
and a module-level ``_DaemonThreadPoolExecutor`` that the FastAPI shutdown
hook drains cleanly.
"""

from __future__ import annotations

from concurrent.futures import Future, wait
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops.policy import get_approval_registry, get_gate
from demo.ui import server as srv


@pytest.fixture
def client(monkeypatch):
    """Mirror the fixture pattern from ``tests/test_approval_web_endpoints``
    (snapshot the chatops + WS singletons so this file doesn't pollute the
    others) and additionally make sure the agent pool is healthy for each
    test."""
    monkeypatch.delenv("AIOPS_HITL_APPROVAL_TOKEN", raising=False)

    from aiops.tools.chatops import client as chat_client_mod
    from demo.ui import chatops_ws as ws_mod

    original_approver = get_gate().approver
    saved_adapters = list(chat_client_mod._CLIENT._adapters)
    saved_history = list(ws_mod._HUB._history)
    get_approval_registry()._reset_for_tests()
    try:
        with TestClient(srv.app) as c:
            yield c
    finally:
        get_gate().set_approver(original_approver)
        get_approval_registry()._reset_for_tests()
        chat_client_mod._CLIENT._adapters[:] = saved_adapters
        ws_mod._HUB._history.clear()
        ws_mod._HUB._history.extend(saved_history)


# ─── _BoundedOutcomeStore unit tests ──────────────────────────────────────


def test_bounded_outcome_store_evicts_oldest_after_max():
    store = srv._BoundedOutcomeStore()
    cap = srv._BoundedOutcomeStore._MAX_ENTRIES
    for i in range(cap + 1):
        store[f"id-{i}"] = {"status": "executed", "approval_id": f"id-{i}"}
    assert "id-0" not in store, "oldest entry should be evicted at cap+1"
    assert f"id-{cap}" in store
    assert len(store) == cap


def test_bounded_outcome_store_overwrite_refreshes_recency():
    """Re-assigning an existing key bumps it to most-recent so the next
    eviction skips it. Important when an outcome is re-published (rare
    today but cheap to guarantee)."""
    store = srv._BoundedOutcomeStore()
    cap = srv._BoundedOutcomeStore._MAX_ENTRIES
    for i in range(cap):
        store[f"id-{i}"] = {"v": i}
    store["id-0"] = {"v": "refreshed"}
    store["new"] = {"v": "new"}
    assert "id-0" in store, "overwritten key should not be the LRU victim"
    assert "id-1" not in store, "id-1 is now the oldest, should be evicted"


def test_bounded_outcome_store_is_thread_safe(tmp_path):
    """Hammering the store from many threads at once must not corrupt the
    cap or raise. Smoke-level concurrency: 8 writers × 200 entries each."""
    import threading

    store = srv._BoundedOutcomeStore()
    cap = srv._BoundedOutcomeStore._MAX_ENTRIES

    def _writer(worker_id: int) -> None:
        for j in range(200):
            store[f"w{worker_id}-{j}"] = {"v": j}

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store) == cap


# ─── end-to-end: 101 requests evict the oldest ───────────────────────────


def test_oldest_approval_id_is_evicted_after_max_plus_one_requests(client, monkeypatch):
    """Acceptance: after 101 requests, the oldest ``approval_id`` is no
    longer returned by ``get_auto_heal_outcome`` — it falls back to the
    default ``pending`` response because the LRU evicted it."""
    from agents import auto_healer_lite

    cap = srv._BoundedOutcomeStore._MAX_ENTRIES

    class _StubOutcome:
        def __init__(self, approval_id: str) -> None:
            self.approval_id = approval_id

        def model_dump(self, mode: str = "json") -> dict[str, Any]:
            return {"status": "executed", "approval_id": self.approval_id}

    def _stub_recommend_restart(rec, hitl_context):
        # The endpoint imports ``recommend_restart`` from
        # ``agents.auto_healer_lite`` inside the request — Python's import
        # cache means monkeypatching the module attribute is enough.
        return _StubOutcome(hitl_context["approval_id"])

    monkeypatch.setattr(auto_healer_lite, "recommend_restart", _stub_recommend_restart)

    # Capture the futures from each ``submit`` so we can wait deterministically
    # for the pool to drain. ``concurrent.futures.wait`` returns once every
    # future is done (or the timeout fires).
    submitted: list[Future] = []
    real_submit = srv._HITL_AGENT_POOL.submit

    def _capturing_submit(fn, *args, **kwargs):
        fut = real_submit(fn, *args, **kwargs)
        submitted.append(fut)
        return fut

    monkeypatch.setattr(srv._HITL_AGENT_POOL, "submit", _capturing_submit)

    approval_ids: list[str] = []
    for i in range(cap + 1):
        res = client.post(
            "/api/demo/auto-heal/restart",
            json={"deployment": f"d{i}", "timeout_seconds": 5},
        )
        assert res.status_code == 200
        approval_ids.append(res.json()["approval_id"])

    _done, not_done = wait(submitted, timeout=10)
    assert not not_done, f"agent pool did not drain in time: {len(not_done)} hung"

    # Oldest id was evicted → outcome endpoint falls back to the default
    # ``pending`` shape (this is what the dashboard already handles).
    res = client.get(f"/api/demo/auto-heal/outcome/{approval_ids[0]}")
    assert res.status_code == 200
    assert res.json() == {"status": "pending", "approval_id": approval_ids[0]}

    # Newest id is still present and returns the stub's executed payload.
    res = client.get(f"/api/demo/auto-heal/outcome/{approval_ids[-1]}")
    assert res.status_code == 200
    assert res.json() == {"status": "executed", "approval_id": approval_ids[-1]}

    # The store itself sits at exactly the cap.
    assert len(srv._HITL_OUTCOMES) == cap


# ─── executor shutdown / startup hooks ───────────────────────────────────


def test_shutdown_hook_shuts_down_executor():
    """``_shutdown_hitl_agent_pool`` calls ``ThreadPoolExecutor.shutdown``.
    The ``_shutdown`` flag becoming ``True`` is the public signal that no
    more work can be submitted (CPython contract)."""
    pool = srv._new_hitl_agent_pool()
    saved = srv._HITL_AGENT_POOL
    srv._HITL_AGENT_POOL = pool
    try:
        assert pool._shutdown is False
        srv._shutdown_hitl_agent_pool()
        assert pool._shutdown is True
        with pytest.raises(RuntimeError):
            pool.submit(lambda: None)
    finally:
        srv._HITL_AGENT_POOL = saved


def test_startup_hook_recreates_pool_after_shutdown():
    """A TestClient context closes the app and shuts down the pool; if the
    same process opens a *second* TestClient, the startup hook has to
    revive the executor or every subsequent test would explode."""
    saved = srv._HITL_AGENT_POOL
    closed = srv._new_hitl_agent_pool()
    closed.shutdown(wait=False, cancel_futures=True)
    srv._HITL_AGENT_POOL = closed
    try:
        srv._ensure_hitl_agent_pool()
        assert srv._HITL_AGENT_POOL is not closed
        assert srv._HITL_AGENT_POOL._shutdown is False
        # Sanity-check it actually works.
        f = srv._HITL_AGENT_POOL.submit(lambda: 7)
        assert f.result(timeout=2) == 7
    finally:
        # Restore the original pool so later tests in the same process
        # aren't surprised.
        srv._HITL_AGENT_POOL.shutdown(wait=False, cancel_futures=True)
        srv._HITL_AGENT_POOL = saved


def test_testclient_context_manager_runs_shutdown_hook():
    """The FastAPI shutdown event fires on ``TestClient`` context exit;
    after that, the recorded pool should be shut down. This validates the
    wiring rather than the helper function directly."""
    # Snapshot whatever pool is active so we can restore it.
    saved = srv._HITL_AGENT_POOL
    try:
        with TestClient(srv.app):
            during = srv._HITL_AGENT_POOL
            assert during._shutdown is False
        # After the context exits the recorded pool is closed.
        assert during._shutdown is True
    finally:
        # Bring the module back to a usable state for any later tests.
        srv._ensure_hitl_agent_pool()
        if srv._HITL_AGENT_POOL is not saved and not saved._shutdown:
            srv._HITL_AGENT_POOL.shutdown(wait=False, cancel_futures=True)
            srv._HITL_AGENT_POOL = saved


# ─── pool worker daemon-ness ─────────────────────────────────────────────


def test_pool_workers_are_daemons():
    """Workers must be daemons so a 900s HITL timeout blocked on a pool
    worker can't keep the process alive after shutdown."""
    pool = srv._new_hitl_agent_pool()
    try:
        # Force a worker to spawn.
        f = pool.submit(lambda: None)
        f.result(timeout=2)
        assert pool._threads, "pool should have at least one worker by now"
        assert all(t.daemon for t in pool._threads), "every worker must be a daemon thread"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_pool_thread_name_prefix():
    """``thread_name_prefix='hitl-demo-'`` makes thread dumps readable
    when debugging."""
    pool = srv._new_hitl_agent_pool()
    try:
        f = pool.submit(lambda: None)
        f.result(timeout=2)
        names = [t.name for t in pool._threads]
        assert all(n.startswith("hitl-demo-") for n in names), names
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
