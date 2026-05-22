"""Pydantic models for Auto-Healer-lite (HITL-1, issue #77)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RestartRecommendation(BaseModel):
    """What the agent wants to do, before the gate sees it."""

    deployment: str = Field(..., description="Deployment name, e.g. 'product-catalog'")
    namespace: str = Field("otel-demo", description="Kubernetes namespace")
    reason: str = Field(..., description="Why a restart is recommended")
    runbook: str = Field("restart-deployment", description="Runbook id for audit purposes")
    dry_run: bool = Field(True, description="Always True in the POC")


class RestartOutcome(BaseModel):
    """The full result of attempting the restart through the gate."""

    recommendation: RestartRecommendation
    status: Literal["executed", "blocked", "denied", "expired", "error"]
    approval_id: str | None = Field(
        None,
        description=(
            "Approval request id that was opened by the gate. None when the "
            "gate didn't reach the approver (e.g. the capability wasn't "
            "registered, or skip_approval was set)."
        ),
    )
    approver: str | None = Field(None, description="Resolved approver identity, if any")
    result: dict | None = Field(None, description="Tool result data on executed status")
    error: str | None = Field(None, description="Gate / tool error on non-executed status")
