"""Parity: Alert Triage's context-derived metric/trace context must be
byte-identical to its legacy fetch, given the same underlying provider data.

Alert Triage is the highest-risk agent in this migration: it is the sole member
of the eval harness's truth-file-runnable set, and its ``decision_trace`` is
persisted and later compared byte-equal by
``tests/test_alert_triage_idempotency.py``. This file is the CI-enforced proof
that the context path cannot silently reword a trace line.
"""

from __future__ import annotations

import pytest

from agents.alert_triage.agent import (
    _fetch_metric_context,
    _fetch_trace_context,
    _metric_and_trace_fetchers,
    trace_context_candidates,
    triage,
)
from agents.alert_triage.context_adapter import (
    build_context_request_specs,
    context_metric_query,
    context_metrics_capability_available,
    context_trace_search,
)
from agents.alert_triage.models import Alert
from aiops.context.builder import ContextBuilder, ContextRequest
from aiops.tools.registry import ToolResult


def _alert(**overrides) -> Alert:
    base = {
        "alert_id": "a-1",
        "source": "prometheus",
        "service": "payment-service",
        "metric": "http_error_rate",
        "severity_hint": None,
        "value": 12.5,
        "threshold": 5.0,
        "timestamp": "2026-08-10T12:30:00Z",
        "annotations": {},
    }
    return Alert(**{**base, **overrides})


class _FakeRegistry:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ok = ok

    def call(self, capability: str, **kwargs) -> ToolResult:
        self.calls.append((capability, kwargs))
        if not self._ok:
            return ToolResult(ok=False, error="connection refused", metadata={})
        if capability == "observability.metrics.query":
            return ToolResult(
                ok=True,
                data={"results": [{"metric": {}, "value": [1754827800.0, "3.2"]}]},
                metadata={"provider": "prometheus"},
            )
        if capability == "observability.traces.search":
            if kwargs.get("service") == "payment-service":
                return ToolResult(
                    ok=True,
                    data={
                        "service": "payment-service",
                        "trace_count": 1,
                        "traces": [{"trace_id": "t-1", "span_count": 2}],
                    },
                    metadata={"provider": "jaeger"},
                )
            return ToolResult(ok=True, data={"trace_count": 0, "traces": []}, metadata={})
        return ToolResult(ok=False, error="nope", metadata={"missing_provider": True})

    def by_capability(self, capability: str):
        if capability == "observability.metrics.query" and not self._ok:
            raise KeyError(capability)
        return object()


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr("agents.alert_triage.agent.get_registry", lambda: registry)
    return registry


def _build_context(alert, registry):
    request = ContextRequest(
        service=alert.service,
        window_start=alert.timestamp,
        window_end=alert.timestamp,
        specs=build_context_request_specs(alert),
    )
    from unittest.mock import patch

    with patch("aiops.context.collectors.base.get_registry", lambda: registry):
        return ContextBuilder().build(request, now=alert.timestamp)


def test_metric_context_is_byte_identical_via_context(fake_registry):
    alert = _alert()
    legacy_trace: list[str] = []
    legacy = _fetch_metric_context(alert, legacy_trace)

    ctx = _build_context(alert, _FakeRegistry())
    context_trace: list[str] = []
    from_context = _fetch_metric_context(
        alert,
        context_trace,
        query_fn=context_metric_query(ctx),
        capability_available=context_metrics_capability_available(ctx),
    )

    assert from_context == legacy
    assert context_trace == legacy_trace


def test_trace_context_is_byte_identical_via_context(fake_registry):
    alert = _alert()
    legacy_trace: list[str] = []
    legacy = _fetch_trace_context(alert, legacy_trace)

    ctx = _build_context(alert, _FakeRegistry())
    context_trace: list[str] = []
    from_context = _fetch_trace_context(alert, context_trace, search_fn=context_trace_search(ctx))

    assert from_context == legacy
    assert context_trace == legacy_trace


class _UnregisteredRegistry:
    """Distinct from `_FakeRegistry(ok=False)`: this simulates a capability that
    was never *registered* (missing_provider), not one that is registered but
    currently erroring (FAILED). The two are different legacy outcomes — a
    registered-but-failing provider still passes the pre-flight `by_capability`
    probe and fails per-query instead — and the test must exercise the one the
    pre-flight probe actually short-circuits on."""

    def call(self, capability: str, **kwargs) -> ToolResult:
        return ToolResult(ok=False, error=None, metadata={"missing_provider": True})

    def by_capability(self, capability: str):
        raise KeyError(capability)


def test_an_unregistered_metrics_capability_produces_the_identical_trace_line():
    """The pre-flight probe's single fast-path line, reproduced from context
    status (UNAVAILABLE) rather than a raised KeyError."""
    alert = _alert()
    unregistered = _UnregisteredRegistry()
    legacy_trace: list[str] = []
    legacy = _fetch_metric_context(alert, legacy_trace, capability_available=lambda: False)

    ctx = _build_context(alert, unregistered)
    context_trace: list[str] = []
    from_context = _fetch_metric_context(
        alert,
        context_trace,
        query_fn=context_metric_query(ctx),
        capability_available=context_metrics_capability_available(ctx),
    )

    assert legacy is None
    assert from_context is None
    assert (
        context_trace
        == legacy_trace
        == ["metrics_ctx: capability observability.metrics.query not registered"]
    )


def test_metric_query_derivation_matches_between_agent_and_adapter(fake_registry):
    """The context request's specs come from the SAME _build_promql_queries
    call the live path uses — if they ever diverged, a query added to one
    would silently be missing from the other's context section."""
    from agents.alert_triage.agent import _build_promql_queries

    alert = _alert(metric="cpu_usage")
    specs = build_context_request_specs(alert)
    requested_promqls = {s.params["promql"] for s in specs if s.source == "metrics"}
    assert requested_promqls == set(_build_promql_queries(alert).values())


def test_trace_candidates_match_between_agent_and_adapter():
    alert = _alert()
    specs = build_context_request_specs(alert)
    requested = {s.query_id for s in specs if s.source == "traces"}
    assert requested == set(trace_context_candidates(alert))


def test_fetchers_for_returns_live_functions_when_flag_off(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    fns = _metric_and_trace_fetchers(_alert(), {"whatever": True}, [])
    assert fns == (_fetch_metric_context, _fetch_trace_context)


def test_fetchers_for_returns_live_functions_with_no_context(monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    fns = _metric_and_trace_fetchers(_alert(), None, [])
    assert fns == (_fetch_metric_context, _fetch_trace_context)


def test_context_present_but_flag_off_still_makes_live_calls(fake_registry, monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    alert = _alert()
    ctx = _build_context(alert, _FakeRegistry())
    fake_registry.calls.clear()
    triage(alert, context=ctx.model_dump(mode="json"))
    assert fake_registry.calls, "flag off must still fetch live even with a context supplied"


def test_context_present_and_flag_on_makes_zero_live_telemetry_calls(fake_registry, monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    alert = _alert()
    ctx = _build_context(alert, _FakeRegistry())
    fake_registry.calls.clear()
    triage(alert, context=ctx.model_dump(mode="json"))
    telemetry_calls = [c for c in fake_registry.calls if c[0].startswith("observability")]
    assert telemetry_calls == [], "the shared context must save the metric/trace round-trips"


def test_no_context_kwarg_reproduces_todays_behavior_exactly(fake_registry, monkeypatch):
    """The default path: no context argument at all (the eval harness's run()
    shim), which must be indistinguishable from pre-migration triage() even
    with the flag on — omitting the argument, not the flag, is what preserves
    the zero-context golden path."""
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    alert = _alert()
    verdict, _row_id = triage(alert)
    assert verdict.affected_service == "payment-service"
    assert any(
        "fetched metric bundle" in line or "queried" in line
        for line in verdict.audit_metadata.decision_trace
    )
