"""Proves the stated purpose of the Context Engineering Layer migration: with
``AIOPS_CONTEXT_LAYER=on``, the reactive flow makes no MORE observability
round-trips than it did before, and — for the sections migrated in this pass —
it makes fewer.

Every other test in this migration proves *correctness* (byte-identical
output). This file is the one that proves the *point*: duplicate retrieval
actually collapses. Without it, "build once, stop duplicating" is a claim in a
docstring rather than something CI checks.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agents.alert_triage import Alert
from aiops.runtime.orchestrator import run_reactive_flow
from aiops.tools.registry import ToolResult


class _CountingRegistry:
    """A registry that answers every observability/itsm/oncall capability with
    a minimal, always-successful payload, and records every capability it was
    asked for. Not a mock of any one backend — a stand-in for "the tool
    registry", matching how ``tests/test_context_collectors.py`` is written.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, capability: str, **kwargs) -> ToolResult:
        self.calls.append(capability)
        if capability == "observability.metrics.query":
            return ToolResult(
                ok=True,
                data={"results": [{"metric": {}, "value": [1754827800.0, "1.0"]}]},
                metadata={"provider": "prometheus"},
            )
        if capability == "observability.metrics.alerts":
            return ToolResult(ok=True, data={"alerts": []}, metadata={"provider": "prometheus"})
        if capability == "observability.traces.search":
            return ToolResult(
                ok=True,
                data={"trace_count": 0, "traces": []},
                metadata={"provider": "jaeger"},
            )
        if capability == "oncall.schedule.lookup":
            return ToolResult(
                ok=True,
                data={"engineer_name": "on-call", "engineer_email": "oncall@example.com"},
                metadata={"provider": "mock"},
            )
        if capability == "itsm.cmdb.lookup":
            return ToolResult(ok=True, data={"team": "payments"}, metadata={"provider": "mock"})
        if capability == "itsm.cmdb.dependencies":
            return ToolResult(ok=True, data={"dependencies": []}, metadata={"provider": "mock"})
        if capability == "incident.resolvers.lookup":
            return ToolResult(ok=True, data={"resolvers": []}, metadata={"provider": "mock"})
        return ToolResult(ok=False, error="nope", metadata={"missing_provider": True})

    def by_capability(self, capability: str):
        return object()

    def observability_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("observability")]


def _alert() -> Alert:
    return Alert(
        alert_id="a-build-once",
        source="prometheus",
        service="payment-service",
        metric="http_error_rate",
        value=12.5,
        threshold=5.0,
        timestamp=datetime(2026, 8, 10, 12, 30, tzinfo=UTC),
        annotations={},
    )


@pytest.fixture
def counting_registry(monkeypatch):
    registry = _CountingRegistry()
    # Patch at every module that resolves its own get_registry() — the exact
    # set this migration touches: alert_triage's live fetch, notification
    # assembler's context_item fetch, and the context layer's own collectors.
    for target in (
        "agents.alert_triage.agent.get_registry",
        "agents.notification_assembler.agent.get_registry",
        "aiops.context.collectors.base.get_registry",
    ):
        monkeypatch.setattr(target, lambda registry=registry: registry)
    return registry


def test_context_on_never_makes_more_observability_calls_than_off(counting_registry, monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    run_reactive_flow(_alert())
    off_count = len(counting_registry.observability_calls())

    counting_registry.calls.clear()
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    run_reactive_flow(_alert())
    on_count = len(counting_registry.observability_calls())

    assert on_count <= off_count, (
        f"AIOPS_CONTEXT_LAYER=on made MORE observability calls ({on_count}) than off "
        f"({off_count}) — the shared context should only ever reduce round-trips"
    )


def test_metrics_query_count_is_unchanged_because_the_dialects_genuinely_differ(
    counting_registry, monkeypatch
):
    """The negative case, stated explicitly so it cannot be mistaken for a
    missed optimisation. alert_triage's three PromQL strings target
    ``http_client_duration_milliseconds_*``; notification's targets
    ``http_server_request_duration_count`` — a third, distinct metric-name
    dialect (see ``agents/notification_assembler/context_adapter.py``). They
    measure different things and must not be unified, so all four stay four
    calls under BOTH flag states. If this count ever drops to 3, someone
    collapsed two genuinely different queries into one cache entry, which
    would silently change one agent's numbers.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    run_reactive_flow(_alert())
    metrics_query_calls = [c for c in counting_registry.calls if c == "observability.metrics.query"]
    assert len(metrics_query_calls) == 4


def test_context_on_reduces_the_duplicated_traces_search_call(counting_registry, monkeypatch):
    """The concrete, measurable win for this migration's scope. Off, four
    independent live calls: alert_triage tries three service-name candidates
    for "payment-service" (all the same string here — no space, no "-api"
    suffix to strip) plus notification's own request with identical
    ``service``/``lookback``/``limit``. On, all four requests share one
    fingerprint (same source, same params) within the ONE context Phase 8
    builds for both agents, and the collector's in-flight request coalescing
    (``aiops/context/collectors/base.py``) guarantees exactly one live call
    serves all four — not "as many as happen to lose the cache-miss race",
    which is what an earlier version of this fix left as a timing accident
    (see ``test_context_collectors.py::test_concurrent_identical_requests_make_exactly_one_live_call``
    for the isolated proof of the coalescing itself).
    """
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    run_reactive_flow(_alert())
    off_count = len([c for c in counting_registry.calls if c == "observability.traces.search"])

    counting_registry.calls.clear()
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    run_reactive_flow(_alert())
    on_count = len([c for c in counting_registry.calls if c == "observability.traces.search"])

    assert off_count == 4
    assert on_count == 1
    assert on_count < off_count
