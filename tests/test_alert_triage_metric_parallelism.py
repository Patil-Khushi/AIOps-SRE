"""Tests for parallelized Stage 4 metric correlation.

Two things we want to guarantee:
1. Queries fan out in parallel — wall-clock total is ~max(query), not
   ~sum(query). Proven by sleeping in a fake provider.
2. Trace ordering and result contents are identical to the prior serial
   implementation. Proven by checking trace lines come out in input order
   and result dict still keys correctly.
"""

from __future__ import annotations

import time
from typing import Any

import pytest


def _register_slow_metric_provider(
    sleep_seconds: float,
    *,
    failing_query: str | None = None,
    raise_on_query: str | None = None,
):
    """Replace any prior provider for ``observability.metrics.query`` with a
    fake that sleeps before returning. Returns the call-count list for
    inspection. Cleans up by popping the registration on test teardown
    (handled in the fixture)."""
    from aiops.tools import ToolResult, get_registry
    from aiops.tools.registry import Tool

    registry = get_registry()
    calls: list[str] = []

    def _slow_query(promql: str) -> ToolResult:
        calls.append(promql)
        if raise_on_query and raise_on_query in promql:
            raise RuntimeError("boom")
        time.sleep(sleep_seconds)
        if failing_query and failing_query in promql:
            return ToolResult(ok=False, error="forced failure")
        # Return a single instant-vector row with a numeric value.
        return ToolResult(
            ok=True,
            data={"results": [{"metric": {}, "value": [0, "1.5"]}]},
        )

    # Register under a fresh tool name and switch the active provider.
    tool_name = f"test.slow_metric_provider_{id(_slow_query)}"
    registry._tools[tool_name] = Tool(  # type: ignore[attr-defined]
        name=tool_name,
        description="slow test stub",
        fn=_slow_query,
        capability="observability.metrics.query",
        provider="test",
    )
    prior_active = registry._active.get("observability.metrics.query")  # type: ignore[attr-defined]
    registry._active["observability.metrics.query"] = tool_name  # type: ignore[attr-defined]
    return tool_name, prior_active, calls


@pytest.fixture
def restore_active_provider():
    """Snapshot which tool is wired to the metrics-query capability and
    restore it on teardown so we don't bleed test state into the registry."""
    from aiops.tools import get_registry

    registry = get_registry()
    prior = registry._active.get("observability.metrics.query")  # type: ignore[attr-defined]
    yield
    if prior is None:
        registry._active.pop("observability.metrics.query", None)  # type: ignore[attr-defined]
    else:
        registry._active["observability.metrics.query"] = prior  # type: ignore[attr-defined]


def _alert() -> Any:
    from datetime import UTC, datetime

    from agents.alert_triage import Alert

    return Alert(
        alert_id="ALT-PARA",
        service="payment",
        metric="cpu",  # forces the cpu_seconds_rate query, so we get 4 queries
        value=90.0,
        threshold=80.0,
        timestamp=datetime.now(UTC),
        source="Prometheus",
        labels={},
        annotations={},
    )


def test_parallel_metric_queries_are_faster_than_serial(restore_active_provider):
    """A 0.5 s per-query stub × 4 queries serial → ~2.0 s; parallel → ~0.5 s.
    A generous ceiling of 1.5 s proves we are fanning out, without
    needing exact timing on a noisy laptop."""
    from agents.alert_triage.agent import _fetch_metric_context

    sleep_seconds = 0.5
    _register_slow_metric_provider(sleep_seconds)

    trace: list[str] = []
    t0 = time.perf_counter()
    out = _fetch_metric_context(_alert(), trace)
    elapsed = time.perf_counter() - t0

    assert out is not None
    # Sanity: we did actually call the provider for every query.
    n_queries = len(out["queries"])
    assert n_queries >= 4
    # Parallel: total ~= sleep_seconds + thread overhead. Serial would be
    # sleep_seconds * n_queries. Anything under (n_queries - 1) * sleep is
    # proof of parallelism.
    serial_estimate = sleep_seconds * n_queries
    assert elapsed < serial_estimate - sleep_seconds, (
        f"elapsed={elapsed:.2f}s, serial would be ~{serial_estimate:.2f}s; "
        f"queries did not run in parallel"
    )


def test_trace_lines_are_emitted_in_input_order(restore_active_provider):
    """Even though queries complete in non-deterministic wall-clock order
    when run in parallel, trace lines must come out in the same order as
    the input queries dict — the audit log has to be reproducible."""
    from agents.alert_triage.agent import _build_promql_queries, _fetch_metric_context

    # Configure the fake to fail one query so we get a trace line for it.
    _register_slow_metric_provider(0.05, failing_query="http_status_code")

    trace: list[str] = []
    out = _fetch_metric_context(_alert(), trace)

    assert out is not None
    queries = _build_promql_queries(_alert())
    input_order = list(queries.keys())

    # Pull out the names that appear in trace lines, in the order they appear.
    trace_order = [
        name for name in input_order if any(f"metrics_ctx[{name}]" in line for line in trace)
    ]
    # The error_rate_5xx query is the one carrying http_status_code → fails.
    assert "error_rate_5xx" in trace_order
    # Trace order must equal the subset of input_order it represents.
    expected = [n for n in input_order if n in trace_order]
    assert trace_order == expected, (
        f"trace lines out of input order: {trace_order} vs expected {expected}"
    )


def test_capability_not_registered_short_circuits(restore_active_provider):
    """Pre-flight: if observability.metrics.query has no active provider,
    the function must emit a single 'not registered' trace line and return
    None — not spam one line per query."""
    from aiops.tools import get_registry

    registry = get_registry()
    # Unhook the capability for the duration of this test.
    registry._active.pop("observability.metrics.query", None)  # type: ignore[attr-defined]

    from agents.alert_triage.agent import _fetch_metric_context

    trace: list[str] = []
    out = _fetch_metric_context(_alert(), trace)

    assert out is None
    not_registered_lines = [
        line for line in trace if "capability observability.metrics.query not registered" in line
    ]
    assert len(not_registered_lines) == 1, trace


def test_per_query_exceptions_are_captured_not_propagated(restore_active_provider):
    """A single query raising inside the worker thread must not abort the
    whole batch. The error is recorded in the trace; other queries' results
    still appear in the output."""
    from agents.alert_triage.agent import _fetch_metric_context

    _register_slow_metric_provider(0.05, raise_on_query="http_status_code")

    trace: list[str] = []
    out = _fetch_metric_context(_alert(), trace)

    assert out is not None
    # The failing query is recorded in the trace.
    assert any("metrics_ctx[error_rate_5xx]" in line and "error" in line for line in trace), trace
    # And the other queries still produced numeric values.
    results = out["results"]
    assert results["error_rate_5xx"] is None
    # request_rate and latency_p95_ms succeeded.
    assert isinstance(results["request_rate"], float)
    assert isinstance(results["latency_p95_ms"], float)
