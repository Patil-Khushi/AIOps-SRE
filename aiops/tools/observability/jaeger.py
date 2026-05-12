"""Jaeger provider for the ``observability.traces.*`` capabilities."""

from __future__ import annotations

import os
from typing import Any

import httpx

from aiops.tools.registry import ToolResult, tool

_URL = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686")
_API_PREFIX = os.environ.get("AIOPS_JAEGER_API_PREFIX", "/jaeger/ui")
# The OTel demo's Jaeger v2 config sets ``base_path: /jaeger/ui`` so query
# endpoints live at ``/jaeger/ui/api/*``. Override to ``""`` for vanilla Jaeger.
_TIMEOUT = float(os.environ.get("AIOPS_JAEGER_TIMEOUT", "10"))


def _get(path: str, params: dict[str, str] | None = None) -> ToolResult:
    try:
        r = httpx.get(f"{_URL}{_API_PREFIX}{path}", params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPError as exc:
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
