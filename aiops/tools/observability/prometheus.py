"""Prometheus provider for the ``observability.metrics.*`` capabilities."""

from __future__ import annotations

import os

import httpx

from aiops.tools.registry import ToolResult, tool

_URL = os.environ.get("AIOPS_PROMETHEUS_URL", "http://localhost:9090")
_TIMEOUT = float(os.environ.get("AIOPS_PROMETHEUS_TIMEOUT", "10"))


def _get(path: str, params: dict[str, str] | None = None) -> ToolResult:
    try:
        r = httpx.get(f"{_URL}{path}", params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPError as exc:
        return ToolResult(ok=False, error=f"HTTPError: {exc}")
    if body.get("status") != "success":
        return ToolResult(ok=False, error=body.get("error") or "non-success response")
    return ToolResult(ok=True, data=body["data"], metadata={"provider": "prometheus", "url": _URL})


@tool(
    name="prometheus.observability.metrics.query",
    capability="observability.metrics.query",
    provider="prometheus",
    description="Instant PromQL query against Prometheus.",
)
def query(promql: str) -> ToolResult:
    res = _get("/api/v1/query", params={"query": promql})
    if not res.ok:
        return res
    data = res.data or {}
    return ToolResult(
        ok=True,
        data={
            "query": promql,
            "result_type": data.get("resultType"),
            "results": data.get("result", []),
        },
        metadata=res.metadata,
    )


@tool(
    name="prometheus.observability.metrics.alerts",
    capability="observability.metrics.alerts",
    provider="prometheus",
    description="List currently firing or pending alerts in Prometheus.",
)
def alerts() -> ToolResult:
    res = _get("/api/v1/alerts")
    if not res.ok:
        return res
    return ToolResult(
        ok=True,
        data={"alerts": (res.data or {}).get("alerts", [])},
        metadata=res.metadata,
    )
