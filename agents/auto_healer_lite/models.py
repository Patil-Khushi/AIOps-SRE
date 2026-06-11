"""Pydantic models for Auto-Healer-lite.

Two coexisting surfaces:

1. **Legacy HITL-1 narrow path** (``RestartRecommendation`` /
   ``RestartOutcome`` / ``recommend_restart``). Built specifically to
   exercise the platform HITL gate end-to-end (issue #77). Hardcoded to
   the ``automation.runbook.execute`` capability. Kept as-is — there are
   active tests + a CLI runner that depend on this shape.

2. **PRS-002 generic path** (``ExecutionRequest`` / ``ExecutionVerdict``
   / ``ExecutionStatus`` / ``execute``). Day-1 scaffold for the catalog
   Auto-Healer agent that consumes a chosen ``RemediationOption`` from
   PRS-001 and dispatches the right tool capability after the platform
   HITL gate clears. Day-1 stub never actually fires a tool — see
   ``agent.py:execute`` and the README for the v1 cut line.

Both surfaces share the same module so the HITL-1 demo and the
PRS-002 contract evolve together. The dispatch in
``agent.run(input)`` routes based on input shape (an ``option`` key
sends the request to ``execute``; otherwise it stays on the legacy
restart path).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── Legacy HITL-1 narrow surface (issue #77) ──────────────────────────────


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


# ─── PRS-002 generic surface (Day-1 scaffold) ──────────────────────────────


class ExecutionStatus(StrEnum):
    """Terminal status of one Auto-Healer Lite execution attempt.

    Values are narrow so audit-log consumers can branch on them without
    string-matching free-form descriptions:

    - ``refused`` — the request was malformed (missing
      ``requires_hitl=True``, missing ``tool_capability`` on a non-
      manual action). The agent never reached the gate.
    - ``pending_approval`` — gate ran, awaiting a human. The decision
      summary carries the approval id the operator must respond to.
    - ``blocked`` — gate ran, the human denied (or the window expired).
    - ``approved`` — gate cleared. In Day-1 this maps to ``dry_run_ok``;
      v1 uses it as the precursor to real execution.
    - ``dry_run_ok`` — Day-1 success: option validated, gate approved
      (or was a no-op in NONE-level configs), tool call would have
      fired but the stub skipped it.
    - ``executed`` — v1 success. Tool call returned ok.
    - ``execution_failed`` — v1 failure. Tool call raised or returned
      a not-ok envelope.
    """

    REFUSED = "refused"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED = "blocked"
    APPROVED = "approved"
    DRY_RUN_OK = "dry_run_ok"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"


class ExecutionRequest(BaseModel):
    """One request to Auto-Healer Lite — "execute this chosen option".

    ``option`` is dict-shaped (not a hard import of PRS-001's
    ``RemediationOption``) so this agent compiles and ships
    independently of PRS-001 landing in main. The validator only checks
    fields Auto-Healer cares about; extras pass through into the audit
    trail.
    """

    model_config = ConfigDict(extra="allow")

    option: dict[str, Any] = Field(
        description=(
            "Chosen RemediationOption (dict-form). Must carry option_id, "
            "action_type, tool_capability, tool_args, blast_radius, "
            "rollback, and requires_hitl=True."
        )
    )
    incident_id: str | None = None
    affected_service: str = Field(min_length=1)
    operator: str | None = Field(
        default=None,
        description=(
            "Who initiated this execution (email or slack handle). Recorded "
            "in the audit trail; the gate may use it as a hint for the "
            "approver UI but does not authorise on it."
        ),
    )
    # Day-1: forced True regardless of caller's intent.
    dry_run: bool = True
    # Forwarded into HITLGate.check() so the approver can render the
    # right context (incident link, blast radius hint).
    hitl_context: dict[str, Any] = Field(default_factory=dict)


class GateDecisionSummary(BaseModel):
    """Flat JSON-friendly view of ``aiops.policy.Decision`` for the verdict.

    Pulled out into a Pydantic model because the underlying dataclass
    isn't JSON-serialisable out of the box across pydantic versions —
    and the eval harness + dashboard need a stable wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    level: str  # AutonomyLevel.value
    reason: str
    approver: str | None = None
    approval_id: str | None = None
    approval_status: str | None = None


class AuditMetadata(BaseModel):
    """Provenance carried in every verdict. Same shape as PRS-001 / PRS-008
    so the dashboard's decision-trace renderer works against all three."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "PRS-002"
    decision_trace: list[str] = Field(default_factory=list)


class ExecutionVerdict(BaseModel):
    """Auto-Healer Lite's structured response — what the agent emits.

    ``would_execute`` is a Day-1 affordance: when ``status==dry_run_ok``
    (the stub never actually fires the tool), this flag tells the UI
    what WOULD have run so the operator can compare against expectation
    before unlocking v1's real-execute path.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    option_id: str = Field(min_length=1)
    affected_service: str = Field(min_length=1)
    status: ExecutionStatus
    dry_run: bool
    # Invariant: every Auto-Healer Lite execution is HITL-gated.
    requires_hitl: Literal[True] = True
    decision: GateDecisionSummary
    tool_capability: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    tool_result: dict[str, Any] | None = None
    would_execute: bool = Field(
        default=False,
        description=(
            "Day-1: True when status=dry_run_ok or approved AND a "
            "tool_capability is set — i.e. v1 would have fired the tool here."
        ),
    )
    error: str | None = None
    rationale: str = Field(min_length=1)
    audit_metadata: AuditMetadata


__all__ = [
    "AuditMetadata",
    "ExecutionRequest",
    "ExecutionStatus",
    "ExecutionVerdict",
    "GateDecisionSummary",
    "RestartOutcome",
    "RestartRecommendation",
]
