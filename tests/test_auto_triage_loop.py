"""Unit tests for the auto-triage background loop (#130).

The loop watches ``/api/live-alerts`` and runs the triage pipeline on
new alerts so the demo doesn't need a manual ``POST /api/triage/live``.
These tests pin the dedup invariant and the lifecycle hooks.
"""

from __future__ import annotations

import asyncio

import pytest

from demo.ui import server as srv


def _alert(alert_id: str, service: str = "payment") -> dict:
    return {
        "alert_id": alert_id,
        "service": service,
        "metric": "ScenarioActive",
        "value": 1.0,
        "threshold": 0.0,
        "timestamp": "2026-05-25T10:00:00Z",
        "source": "Prometheus",
        "labels": {},
        "annotations": {},
    }


@pytest.fixture
def fast_loop(monkeypatch):
    """Build an ``_AutoTriageLoop`` with a 10ms interval for fast iteration.

    Stubs ``live_alerts`` and ``triage_alert`` so no Prometheus / LLM
    traffic is generated. Tests can append to ``poll_returns`` and read
    ``triaged`` to drive + observe the loop's behaviour.
    """
    poll_returns: list[list[dict]] = []
    triaged: list[str] = []

    def _stub_live_alerts() -> dict:
        if poll_returns:
            alerts = poll_returns.pop(0)
        else:
            alerts = []
        return {"count": len(alerts), "alerts": alerts, "raw_count": len(alerts)}

    def _stub_triage_alert(req) -> dict:
        triaged.append(req.alert["alert_id"])
        return {"verdict": {"alert_id": req.alert["alert_id"]}}

    monkeypatch.setattr(srv, "live_alerts", _stub_live_alerts)
    monkeypatch.setattr(srv, "triage_alert", _stub_triage_alert)

    loop = srv._AutoTriageLoop(interval_seconds=0.01)
    yield loop, poll_returns, triaged


@pytest.mark.asyncio
async def test_new_alert_is_triaged_on_first_poll(fast_loop):
    loop, poll_returns, triaged = fast_loop
    poll_returns.append([_alert("ALT-1")])

    loop.start()
    await asyncio.sleep(0.05)
    await loop.stop()

    assert triaged == ["ALT-1"], "first poll should run the triage pipeline"


@pytest.mark.asyncio
async def test_seen_alert_not_re_triaged_on_subsequent_poll(fast_loop):
    """Dedup invariant: the same ``alert_id`` showing up in /api/live-alerts
    on a second poll must NOT re-trigger triage. The downstream agent has
    its own idempotency window, but cheap dedup here avoids the cost."""
    loop, poll_returns, triaged = fast_loop
    poll_returns.append([_alert("ALT-A")])
    poll_returns.append([_alert("ALT-A")])  # same id on second poll
    poll_returns.append([_alert("ALT-A")])  # same id on third poll

    loop.start()
    await asyncio.sleep(0.1)
    await loop.stop()

    assert triaged == ["ALT-A"], (
        f"ALT-A should fire exactly once across multiple polls; got {triaged}"
    )


@pytest.mark.asyncio
async def test_new_alert_after_first_is_picked_up(fast_loop):
    loop, poll_returns, triaged = fast_loop
    poll_returns.append([_alert("ALT-1")])
    poll_returns.append([_alert("ALT-1"), _alert("ALT-2")])

    loop.start()
    await asyncio.sleep(0.1)
    await loop.stop()

    assert triaged == ["ALT-1", "ALT-2"], triaged


@pytest.mark.asyncio
async def test_forget_all_lets_loop_re_triage_previously_seen_alert(fast_loop):
    """When a scenario reset endpoint fires, calling ``forget_all`` lets
    the loop re-triage the same alert id after a re-inject. Without the
    forget hook, the loop would silently dedupe the re-inject."""
    loop, poll_returns, triaged = fast_loop
    poll_returns.append([_alert("ALT-X")])

    loop.start()
    await asyncio.sleep(0.03)
    loop.forget_all()
    poll_returns.append([_alert("ALT-X")])
    await asyncio.sleep(0.05)
    await loop.stop()

    assert triaged == ["ALT-X", "ALT-X"], f"forget_all should let ALT-X re-trigger; got {triaged}"


@pytest.mark.asyncio
async def test_loop_survives_live_alerts_failure(monkeypatch):
    """A transient Prometheus failure must NOT crash the loop. The next
    poll should resume normally."""
    triaged: list[str] = []
    call_count = {"n": 0}

    def _flaky_live_alerts() -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated prom outage")
        return {
            "count": 1,
            "alerts": [_alert("ALT-RECOVERED")],
            "raw_count": 1,
        }

    def _stub_triage_alert(req) -> dict:
        triaged.append(req.alert["alert_id"])
        return {"verdict": {}}

    monkeypatch.setattr(srv, "live_alerts", _flaky_live_alerts)
    monkeypatch.setattr(srv, "triage_alert", _stub_triage_alert)

    loop = srv._AutoTriageLoop(interval_seconds=0.01)
    loop.start()
    await asyncio.sleep(0.1)
    await loop.stop()

    assert call_count["n"] >= 2, "loop must keep polling after a failure"
    assert triaged == ["ALT-RECOVERED"], triaged


@pytest.mark.asyncio
async def test_start_is_idempotent(fast_loop):
    """Calling ``start`` twice must not spawn a second concurrent task."""
    loop, _poll_returns, _triaged = fast_loop
    loop.start()
    first_task = loop._task
    loop.start()
    second_task = loop._task
    assert first_task is second_task, "second start must reuse the running task"
    await loop.stop()


@pytest.mark.asyncio
async def test_stop_cancels_cleanly_with_no_task_in_progress():
    """``stop`` on a never-started loop must not raise."""
    loop = srv._AutoTriageLoop(interval_seconds=0.01)
    await loop.stop()  # no exception


@pytest.mark.asyncio
async def test_triage_exception_does_not_crash_loop(monkeypatch):
    """If ``triage_alert`` raises an unexpected exception, the loop must
    log it and keep running. Subsequent alerts must still be processed."""

    def _exploding_triage(req):
        raise RuntimeError("simulated triage crash")

    monkeypatch.setattr(srv, "triage_alert", _exploding_triage)

    counter = {"n": 0}

    def _alerts() -> dict:
        counter["n"] += 1
        if counter["n"] <= 2:
            return {
                "count": 1,
                "alerts": [_alert(f"ALT-{counter['n']}")],
                "raw_count": 1,
            }
        return {"count": 0, "alerts": [], "raw_count": 0}

    monkeypatch.setattr(srv, "live_alerts", _alerts)

    loop = srv._AutoTriageLoop(interval_seconds=0.01)
    loop.start()
    await asyncio.sleep(0.1)
    await loop.stop()

    # Both alerts were attempted (otherwise the seen set wouldn't have grown).
    assert "ALT-1" in loop._seen
    assert "ALT-2" in loop._seen
