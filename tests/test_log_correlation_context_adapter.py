"""Parity: Log Correlation's context-derived fetch results must be byte-identical
to its legacy fetch results, given the same underlying provider data.

Same proof structure as ``tests/test_rca_context_adapter.py``: both paths run
through the exact same ``_fetch_logs``/``_fetch_traces``/``_fetch_metrics``
bodies, differing only in which ``fetch`` callable they were given.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.log_correlation.agent import (
    _fetch_logs,
    _fetch_metrics,
    _fetch_traces,
    _fetchers_for,
    _metrics_promql,
)
from agents.log_correlation.context_adapter import (
    build_context_request_specs,
    context_logs_fetch,
    context_metrics_fetch,
    context_traces_fetch,
)
from agents.log_correlation.models import CorrelationInput, TimeWindow
from aiops.context.builder import ContextBuilder, ContextRequest
from aiops.tools.registry import ToolResult

SERVICE = "order-service"
WINDOW_END = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
WINDOW_START = WINDOW_END - timedelta(minutes=15)


def _payload() -> CorrelationInput:
    return CorrelationInput(service=SERVICE, window=TimeWindow(start=WINDOW_START, end=WINDOW_END))


class _FakeRegistry:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[str] = []
        self._ok = ok

    def call(self, capability: str, **kwargs) -> ToolResult:
        self.calls.append(capability)
        if not self._ok:
            return ToolResult(ok=False, error="connection refused", metadata={})
        if capability == "observability.logs.query":
            return ToolResult(
                ok=True,
                data={
                    "streams": [
                        {
                            "stream": {"level": "error", "service_name": SERVICE},
                            "values": [[1754827800000000000, "mysql connection timed out"]],
                        }
                    ]
                },
                metadata={"provider": "loki"},
            )
        if capability == "observability.traces.search":
            return ToolResult(
                ok=True,
                data={
                    "service": SERVICE,
                    "trace_count": 1,
                    "traces": [
                        {
                            "trace_id": "t-1",
                            "span_count": 3,
                            "root_operation": "POST /api/orders",
                            "duration_us": 1_500_000,
                            "start_time_us": 1754827800000000,
                        }
                    ],
                },
                metadata={"provider": "jaeger"},
            )
        if capability == "observability.metrics.query":
            return ToolResult(
                ok=True,
                data={
                    "query": kwargs.get("promql"),
                    "results": [{"metric": {}, "value": [1754827800.0, "0.42"]}],
                },
                metadata={"provider": "prometheus"},
            )
        return ToolResult(ok=False, error="nope", metadata={"missing_provider": True})


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr("agents.log_correlation.agent.get_registry", lambda: registry)
    return registry


def _build_context(registry):
    payload = _payload()
    request = ContextRequest(
        service=SERVICE,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        specs=build_context_request_specs(payload),
    )
    from unittest.mock import patch

    with patch("aiops.context.collectors.base.get_registry", lambda: registry):
        return ContextBuilder().build(request, now=WINDOW_END)


@pytest.mark.parametrize(
    ("fn_name", "fetch_factory"),
    [
        ("_fetch_logs_family", context_logs_fetch),
        ("_fetch_traces_family", context_traces_fetch),
        ("_fetch_metrics_family", context_metrics_fetch),
    ],
)
def test_each_fetch_function_is_byte_identical_via_context(fake_registry, fn_name, fetch_factory):
    payload = _payload()
    fn = {
        "_fetch_logs_family": _fetch_logs,
        "_fetch_traces_family": _fetch_traces,
        "_fetch_metrics_family": _fetch_metrics,
    }[fn_name]

    legacy_trace: list[str] = []
    legacy_signals, legacy_reachable = fn(payload, legacy_trace)

    ctx = _build_context(_FakeRegistry())
    context_trace: list[str] = []
    context_signals, context_reachable = fn(payload, context_trace, fetch=fetch_factory(ctx))

    assert [s.model_dump() for s in context_signals] == [s.model_dump() for s in legacy_signals]
    assert context_reachable == legacy_reachable
    assert context_trace == legacy_trace


def test_a_provider_failure_produces_the_identical_trace_line(fake_registry):
    """The error TEXT, not just the ok/not-ok bit, must round-trip: the collector
    copies ToolResult.error verbatim into provenance.error for exactly this."""
    dead = _FakeRegistry(ok=False)
    payload = _payload()

    legacy_trace: list[str] = []
    _fetch_logs(payload, legacy_trace, fetch=lambda p: dead.call("observability.logs.query"))

    ctx = _build_context(_FakeRegistry(ok=False))
    context_trace: list[str] = []
    _fetch_logs(payload, context_trace, fetch=context_logs_fetch(ctx))

    assert context_trace == legacy_trace
    assert "loki error (connection refused)" in context_trace[0]


def test_fetchers_for_returns_live_functions_when_context_layer_is_off(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    fns = _fetchers_for(_payload(), {"whatever": True}, [])
    assert fns == (_fetch_logs, _fetch_traces, _fetch_metrics)


def test_fetchers_for_returns_live_functions_with_no_context(monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    fns = _fetchers_for(_payload(), None, [])
    assert fns == (_fetch_logs, _fetch_traces, _fetch_metrics)


def test_fetchers_for_binds_context_fetchers_when_on(monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    ctx = _build_context(_FakeRegistry())
    fns = _fetchers_for(_payload(), ctx.model_dump(mode="json"), [])
    assert fns != (_fetch_logs, _fetch_traces, _fetch_metrics)
    # Each is a functools.partial wrapping the real fetch function, so calling it
    # still runs the identical downstream parsing code.
    trace: list[str] = []
    signals, reachable = fns[0](_payload(), trace)
    assert reachable is True
    assert signals


def test_context_present_but_flag_off_still_makes_live_calls(fake_registry, monkeypatch):
    from agents.log_correlation.agent import correlate

    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    ctx = _build_context(_FakeRegistry())
    fake_registry.calls.clear()
    correlate(_payload(), context=ctx.model_dump(mode="json"))
    assert fake_registry.calls, "flag off must still fetch live even with a context supplied"


def test_context_present_and_flag_on_makes_zero_live_fetch_calls(fake_registry, monkeypatch):
    from agents.log_correlation.agent import correlate

    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    ctx = _build_context(_FakeRegistry())
    fake_registry.calls.clear()
    correlate(_payload(), context=ctx.model_dump(mode="json"))
    assert fake_registry.calls == [], "the shared context must save the three round-trips"


def test_force_synthetic_never_touches_the_context_path(fake_registry, monkeypatch):
    """The zero-I/O golden path run() relies on: force_synthetic short-circuits
    before _fetchers_for is ever consulted, regardless of the flag."""
    from agents.log_correlation.agent import correlate

    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    ctx = _build_context(_FakeRegistry())
    fake_registry.calls.clear()
    result = correlate(_payload(), force_synthetic=True, context=ctx.model_dump(mode="json"))
    assert fake_registry.calls == []
    assert any("synthetic" in line for line in result.audit_metadata.decision_trace)


def test_metrics_promql_matches_between_agent_and_adapter():
    """The context adapter derives its query_id from the same function the live
    path uses to build its PromQL — if they ever diverged, the context-sourced
    fetch would look up the wrong cache entry and silently answer as UNAVAILABLE."""
    payload = _payload()
    assert _metrics_promql(payload) in build_context_request_specs(payload)[2].params.values()
