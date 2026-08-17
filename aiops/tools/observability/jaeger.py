"""Jaeger provider for the ``observability.traces.*`` capabilities."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from aiops.tools.registry import ToolResult, tool

_URL = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686")
_API_PREFIX = os.environ.get("AIOPS_JAEGER_API_PREFIX", "/jaeger/ui")
# The OTel demo's Jaeger v2 config sets ``base_path: /jaeger/ui`` so query
# endpoints live at ``/jaeger/ui/api/*``. Override to ``""`` for vanilla Jaeger.
_TIMEOUT = float(os.environ.get("AIOPS_JAEGER_TIMEOUT", "10"))
# Connect-phase cap (#113). When the kubectl port-forward to Jaeger is down
# — common during tests and CI — a long default connect timeout cascades
# across multiple Jaeger calls per triage and can stall the full pytest
# suite. Keep it short so an unreachable Jaeger degrades gracefully.
_CONNECT_TIMEOUT = float(os.environ.get("AIOPS_JAEGER_CONNECT_TIMEOUT", "2"))

# Process-local circuit breaker (#113). The connect timeout above is not
# enforced reliably on every platform (notably Windows, where a refused
# localhost connect can stall longer than ``connect=...`` suggests). After
# one failure, short-circuit subsequent calls for ``_CIRCUIT_OPEN_SECONDS``
# so a single triage that fans out to N Jaeger calls doesn't multiply the
# wall-clock cost by N. The triage agent already handles ``ToolResult.ok=False``
# gracefully, so callers see a fast "trace_ctx: error" rather than a hang.
_CIRCUIT_OPEN_SECONDS = float(os.environ.get("AIOPS_JAEGER_CIRCUIT_OPEN_SECONDS", "30"))
_circuit_open_until: float = 0.0


def _reset_circuit_for_tests() -> None:
    """Reset the circuit breaker. Test seam only (#113).

    The breaker is process-local module state, so it survives across pytest
    test boundaries — a test that trips it (e.g. on a mocked socket failure)
    would cause the next 30s of tests to short-circuit even when their own
    httpx mocks are set to succeed.  ``tests/conftest.py`` calls this in an
    autouse fixture so the breaker is fresh per-test.
    """
    global _circuit_open_until
    _circuit_open_until = 0.0


def _get(path: str, params: dict[str, str] | None = None) -> ToolResult:
    global _circuit_open_until
    now = time.monotonic()
    if now < _circuit_open_until:
        return ToolResult(ok=False, error="HTTPError: circuit open (Jaeger unreachable)")
    try:
        r = httpx.get(
            f"{_URL}{_API_PREFIX}{path}",
            params=params,
            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )
        r.raise_for_status()
        body = r.json()
    # Catch OSError as well as httpx errors so socket-level failures
    # (ConnectionRefusedError, OSError on Windows when no listener is
    # bound) also trip the breaker.  Intentionally aggressive for the
    # demo path: a transient hiccup blocks Jaeger for 30s, which is
    # cheaper than the alternative of stalling every triage on a real
    # outage.  Tune ``AIOPS_JAEGER_CIRCUIT_OPEN_SECONDS`` if a real
    # eval run needs continuous access.
    except (httpx.HTTPError, OSError) as exc:
        _circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
        return ToolResult(ok=False, error=f"HTTPError: {exc}")
    return ToolResult(ok=True, data=body, metadata={"provider": "jaeger", "url": _URL})


@tool(
    name="jaeger.observability.traces.services",
    capability="observability.traces.services",
    provider="jaeger",
    description="List services known to Jaeger.",
)
def services() -> ToolResult:
    res = _get("/api/services")
    if not res.ok:
        return res
    return ToolResult(
        ok=True,
        data={"services": (res.data or {}).get("data", [])},
        metadata=res.metadata,
    )


def _span_has_error(span: dict[str, Any]) -> bool:
    """Standard OTel span error markers: an explicit ``error`` tag, or an HTTP
    status in the 5xx range. Checked against real live spans (both shapes seen
    on this SUT's FastAPI instrumentation)."""
    for t in span.get("tags") or []:
        key = t.get("key")
        if key == "error" and t.get("value") is True:
            return True
        if key == "http.status_code" and isinstance(t.get("value"), int) and t["value"] >= 500:
            return True
    return False


def _summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    spans = trace.get("spans", [])
    root = next(
        (s for s in spans if not s.get("references")),
        spans[0] if spans else {},
    )
    return {
        "trace_id": trace.get("traceID"),
        "span_count": len(spans),
        "root_operation": root.get("operationName"),
        "duration_us": root.get("duration"),
        "start_time_us": root.get("startTime"),
        # Additive field: any span in the trace carrying an OTel error marker.
        # Existing consumers reading the other keys are unaffected.
        "has_error": any(_span_has_error(s) for s in spans),
    }


@tool(
    name="jaeger.observability.traces.search",
    capability="observability.traces.search",
    provider="jaeger",
    description="Search recent traces for a service. Returns trace summaries, not full spans.",
)
def search(service: str, lookback: str = "1h", limit: int = 20) -> ToolResult:
    res = _get(
        "/api/traces",
        params={"service": service, "lookback": lookback, "limit": str(limit)},
    )
    if not res.ok:
        return res
    traces = (res.data or {}).get("data", []) or []
    return ToolResult(
        ok=True,
        data={
            "service": service,
            "lookback": lookback,
            "trace_count": len(traces),
            "traces": [_summarize_trace(t) for t in traces],
        },
        metadata=res.metadata,
    )
