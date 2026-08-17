"""Tests for the RCA progress channel: the hub (demo/ui/rca_progress.py) in
isolation, and the real instrumentation in agents/rca_agent/agent.py.

Mirrors tests/test_chatops_ws.py's hub-unit-test style, adapted for the one
structural difference: chatops is one long-lived global feed, this hub is
many short-lived per-run_id channels, and the property that matters most is
that two concurrent runs never cross-talk.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agents.rca_agent import agent as rca
from agents.rca_agent.progress import RcaStage, StageEvent, StageOutcome
from demo.ui.rca_progress import RcaProgressHub

TRIAGE = {
    "affected_service": "order-service",
    "severity": "Sev-1",
    "alert_summary": "EcommercePostgresDown firing: postgres_connection_status at 0.0",
    "audit_metadata": {
        "created_at": "2026-08-03T10:00:00Z",
        "source_alerts": ["ALT-order-service-postgres-down"],
    },
}

# Fixed, not datetime.now(UTC) — StageEvent.at defaults via a factory, so two
# _event() calls with identical args a microsecond apart produced unequal
# dicts and made every equality assertion below flaky.
_FIXED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _event(run_id: str, seq: int, stage: RcaStage = RcaStage.RECEIVED) -> dict:
    return StageEvent(
        run_id=run_id, seq=seq, stage=stage, outcome=StageOutcome.STARTED, label="x", at=_FIXED_AT
    ).model_dump(mode="json")


# ─── hub unit tests ─────────────────────────────────────────────────────────


def test_push_without_loop_only_buffers_to_history():
    hub = RcaProgressHub()
    hub.push("run-a", _event("run-a", 1))

    assert hub.history("run-a") == [_event("run-a", 1)]


def test_two_runs_do_not_cross_talk():
    """The property this hub exists for: a subscriber on run A never
    receives a run-B event, even though both are pushed through the same
    process-global hub."""
    hub = RcaProgressHub()
    q_a = hub.subscribe("run-a")
    q_b = hub.subscribe("run-b")

    hub.push("run-a", _event("run-a", 1))

    assert hub.history("run-a") == [_event("run-a", 1)]
    assert hub.history("run-b") == []
    assert q_a is not q_b


def test_history_after_seq_filters_to_only_newer_events():
    hub = RcaProgressHub()
    for i in range(1, 4):
        hub.push("run-a", _event("run-a", i))

    assert [r["seq"] for r in hub.history("run-a", after_seq=1)] == [2, 3]
    assert [r["seq"] for r in hub.history("run-a")] == [1, 2, 3]


def test_is_terminal_tracks_the_terminal_stage():
    hub = RcaProgressHub()
    assert hub.is_terminal("run-a") is False

    hub.push("run-a", _event("run-a", 1, RcaStage.HYPOTHESES))
    assert hub.is_terminal("run-a") is False

    hub.push("run-a", _event("run-a", 2, RcaStage.COMPLETE))
    assert hub.is_terminal("run-a") is True


def test_next_seq_continues_from_history_and_starts_at_one():
    hub = RcaProgressHub()
    assert hub.next_seq("run-a") == 1

    hub.push("run-a", _event("run-a", 1))
    hub.push("run-a", _event("run-a", 2))
    assert hub.next_seq("run-a") == 3
    # Unrelated run is unaffected.
    assert hub.next_seq("run-b") == 1


def test_history_is_capped_at_run_history_max(monkeypatch):
    from demo.ui import rca_progress as mod

    monkeypatch.setattr(mod, "RUN_HISTORY_MAX", 3)
    hub = mod.RcaProgressHub()
    for i in range(1, 6):
        hub.push("run-a", _event("run-a", i))

    assert [r["seq"] for r in hub.history("run-a")] == [3, 4, 5]


def test_lru_eviction_at_max_runs(monkeypatch):
    from demo.ui import rca_progress as mod

    monkeypatch.setattr(mod, "MAX_RUNS", 2)
    hub = mod.RcaProgressHub()
    hub.push("run-a", _event("run-a", 1))
    hub.push("run-b", _event("run-b", 1))
    hub.push("run-c", _event("run-c", 1))  # evicts run-a (least recently touched)

    assert hub.history("run-a") == []
    assert hub.history("run-b") != []
    assert hub.history("run-c") != []


@pytest.mark.asyncio
async def test_push_with_loop_fans_out_to_the_right_subscriber_only():
    hub = RcaProgressHub()
    hub.attach_loop(asyncio.get_running_loop())
    q_a = hub.subscribe("run-a")
    q_b = hub.subscribe("run-b")

    hub.push("run-a", _event("run-a", 1))
    await asyncio.sleep(0)  # call_soon_threadsafe lands on the next tick

    assert q_a.get_nowait() == _event("run-a", 1)
    assert q_b.empty()


@pytest.mark.asyncio
async def test_slow_consumer_does_not_block_others():
    hub = RcaProgressHub()
    hub.attach_loop(asyncio.get_running_loop())
    slow: asyncio.Queue = asyncio.Queue(maxsize=1)
    fast: asyncio.Queue = asyncio.Queue()
    with hub._lock:
        hub._touch("run-a").listeners.update({slow, fast})

    for i in range(1, 6):
        hub.push("run-a", _event("run-a", i))
    await asyncio.sleep(0)

    drained = []
    while not fast.empty():
        drained.append(fast.get_nowait())
    assert [r["seq"] for r in drained] == [1, 2, 3, 4, 5]
    assert slow.qsize() == 1  # accepted its cap, dropped the rest


def test_unsubscribe_stops_further_delivery():
    hub = RcaProgressHub()
    q = hub.subscribe("run-a")
    hub.unsubscribe("run-a", q)

    hub.push("run-a", _event("run-a", 1))
    assert q.empty()


def test_reset_for_tests_clears_every_run():
    hub = RcaProgressHub()
    hub.push("run-a", _event("run-a", 1))
    hub.push("run-b", _event("run-b", 1))

    hub._reset_for_tests()

    assert hub.history("run-a") == []
    assert hub.history("run-b") == []


# ─── HubSink ────────────────────────────────────────────────────────────────


def test_hub_sink_stamps_the_bound_run_id():
    from demo.ui.rca_progress import HubSink

    hub = RcaProgressHub()
    sink = HubSink(hub, "run-a")
    sink.emit(
        StageEvent(
            run_id="run-a", seq=1, stage=RcaStage.RECEIVED, outcome=StageOutcome.STARTED, label="x"
        )
    )

    assert hub.history("run-a")[0]["run_id"] == "run-a"


# ─── real instrumentation, agent.py ─────────────────────────────────────────


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    def emit(self, event: StageEvent) -> None:
        self.events.append(event)


class _RaisingSink:
    def emit(self, event: StageEvent) -> None:
        raise RuntimeError("sink is broken")


@pytest.fixture(autouse=True)
def _no_change_evidence(monkeypatch):
    # Deterministic regardless of whether `scm.commit.history` happens to be
    # registered by an earlier test module in this process — same fixture
    # shape as test_rca_deterministic_confidence.py's `_context_on`.
    monkeypatch.setattr(rca, "_fetch_change_evidence", lambda *a, **k: None)


def test_analyze_emits_stages_in_the_real_pipeline_order():
    """The instrumentation-honesty test: under the hermetic conftest env
    (stub LLM, unreachable Prometheus/Jaeger/Loki, no context layer), the
    STARTED events must appear in exactly the order the real functions in
    agent.py run in. A decorative or dropped stage fails this test."""
    sink = _RecordingSink()

    rca.analyze(TRIAGE, progress=sink, run_id="test-run")

    started_stages = [e.stage for e in sink.events if e.outcome == StageOutcome.STARTED]
    assert started_stages == [
        RcaStage.RECEIVED,
        RcaStage.CHANGE_CORRELATION,
        RcaStage.EVIDENCE,
        RcaStage.CONTEXT_PACK,
        RcaStage.MEMORY_RECALL,
        RcaStage.ACTION_VOCABULARY,
        RcaStage.HYPOTHESES,
        RcaStage.EXPLAINING,
    ]
    # Every event belongs to the run it was started with.
    assert all(e.run_id == "test-run" for e in sink.events)
    # seq is strictly increasing — an SSE client can resume from Last-Event-ID.
    seqs = [e.seq for e in sink.events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # The stub provider is the last real thing that happens; it must be
    # reported as degraded, not silently OK.
    assert sink.events[-1].stage == RcaStage.EXPLAINING
    assert sink.events[-1].outcome == StageOutcome.DEGRADED
    assert "stub" in sink.events[-1].label.lower()


def test_analyze_without_progress_is_unchanged():
    with_sink = rca.analyze(TRIAGE, progress=_RecordingSink(), run_id="test-run").model_dump(
        mode="json"
    )
    without_sink = rca.analyze(TRIAGE).model_dump(mode="json")

    # created_at is a real timestamp taken independently by each call.
    with_sink["audit_metadata"].pop("created_at", None)
    without_sink["audit_metadata"].pop("created_at", None)
    assert with_sink == without_sink


def test_a_raising_sink_cannot_break_the_verdict():
    verdict = rca.analyze(TRIAGE, progress=_RaisingSink(), run_id="test-run")

    assert verdict.affected_service == "order-service"
    assert verdict.ranked_fix_steps


def test_run_shim_passes_no_progress_and_stays_offline():
    """Protects evals/harness.py: the eval-harness contract must not gain a
    progress dependency, and must remain the zero-I/O golden path."""
    result = rca.run({"triage_verdict": TRIAGE})

    assert isinstance(result, dict)
    assert result["affected_service"] == "order-service"
