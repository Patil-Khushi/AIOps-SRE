"""Auto-Healer-lite — two coexisting surfaces.

Legacy HITL-1 narrow surface (issue #77, kept untouched for the demo
that exercises the platform HITL gate end-to-end)::

    from agents.auto_healer_lite import recommend_restart, RestartRecommendation

PRS-002 generic surface (Day-1 scaffold — receives a chosen
``RemediationOption`` from PRS-001 and produces a structured
``ExecutionVerdict`` after the platform HITL gate clears; NEVER
fires the tool in Day-1)::

    from agents.auto_healer_lite import execute, ExecutionRequest, ExecutionVerdict
"""

from agents.auto_healer_lite.agent import execute, recommend_restart, reset_state, run
from agents.auto_healer_lite.models import (
    AuditMetadata,
    ExecutionRequest,
    ExecutionStatus,
    ExecutionVerdict,
    GateDecisionSummary,
    RestartOutcome,
    RestartRecommendation,
)

__all__ = [
    "AuditMetadata",
    "ExecutionRequest",
    "ExecutionStatus",
    "ExecutionVerdict",
    "GateDecisionSummary",
    "RestartOutcome",
    "RestartRecommendation",
    "execute",
    "recommend_restart",
    "reset_state",
    "run",
]
