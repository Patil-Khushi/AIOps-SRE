"""Mock providers for the day-one capabilities.

These let Phase-0 smoke tests (and Phase-1 agent scaffolding) run without any
real backend. Each capability gets a real provider implementation in Phase 1+;
when that lands, agents do not change — only the registry's active provider does.
"""

from __future__ import annotations

from .registry import ToolResult, tool


@tool(
    name="mock.itsm.incident.create",
    capability="itsm.incident.create",
    provider="mock",
    description="Pretend to create an ITSM incident; returns a fake ticket id.",
)
def mock_create_incident(short_description: str, urgency: int = 3) -> ToolResult:
    return ToolResult(
        ok=True,
        data={
            "id": "INC0000001",
            "short_description": short_description,
            "urgency": urgency,
            "state": "new",
        },
        metadata={"provider": "mock"},
    )


@tool(
    name="mock.observability.metrics.query",
    capability="observability.metrics.query",
    provider="mock",
    description="Pretend to query Prometheus; returns a constant series.",
)
def mock_query_metrics(promql: str, range_minutes: int = 5) -> ToolResult:
    return ToolResult(
        ok=True,
        data={"query": promql, "range_minutes": range_minutes, "samples": [0.1, 0.2, 0.3]},
        metadata={"provider": "mock"},
    )


@tool(
    name="mock.notify.send",
    capability="notify.send",
    provider="mock",
    description="Pretend to send a chat notification.",
)
def mock_notify(channel: str, message: str) -> ToolResult:
    return ToolResult(ok=True, data={"channel": channel, "message": message[:200]})
