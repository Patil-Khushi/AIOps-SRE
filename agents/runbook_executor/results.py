"""The contracts RA-004 hands to the orchestrator, the API and the Resolution Verifier.

Three result shapes, each with one job:

- :class:`PlanResult` — what came back from "find me a procedure and validate it":
  the ranked candidates, the decision about who chooses, and (when one runbook is on
  the table) the dry-run report plus the execution handle that authorizes running it.
- :class:`ExecutorResult` — §27's contract. ``status`` + ``next_action`` are the two
  fields the orchestrator branches on, and neither can express "the incident is
  resolved": the strongest thing the executor may say is ``EXECUTED`` /
  ``next_action=VERIFY``.
- :class:`VerificationHandoff` — §29's payload for ``resolution_verifier``. It carries
  what *was done*, never a judgement about whether it worked, and the executor computes
  no recovery signal of its own.

These live in their own module rather than in ``models.py`` because they compose the
dry-run report and the candidate list, and ``models.py`` sits *below* both (``dryrun``
and ``matching`` import it). Keeping the composite shapes here keeps the import graph
acyclic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.runbook_executor.dryrun import DryRunReport
from agents.runbook_executor.execution_state import (
    ExecutionRecord,
    ExecutionState,
    ExecutorStatus,
    NextAction,
    UiState,
)
from agents.runbook_executor.matching import DiscoveryDecision, DiscoveryResult, RunbookCandidate
from agents.runbook_executor.models import RunbookExecution


class PlanResult(BaseModel):
    """Discovery + validation + dry run for one incident.

    ``execution_id`` is present only when a plan is genuinely runnable: a blocked dry
    run does not reserve an execution, so the executions table stays a record of things
    that were authorized rather than a log of rejected attempts (those are counted in
    metrics and reported in ``blocking_reasons``).
    """

    decision: DiscoveryDecision
    reason: str = ""
    candidates: list[RunbookCandidate] = Field(default_factory=list)
    selected_runbook_id: str | None = None
    selected_runbook_version: int | None = None
    selected_by: str = ""  # "auto" | operator identity
    dry_run: DryRunReport | None = None
    execution_id: str | None = None
    execution_state: ExecutionState | None = None
    already_executed: bool = False  # an execution for this exact plan already ran
    ui_state: UiState = UiState.DISCOVERING_RUNBOOKS
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        """May :func:`agents.runbook_executor.agent.execute_plan` be called on this?

        ``already_executed`` makes this False: the execution reserved for this exact
        plan has reached a terminal state, so the answer to "run it" is that it already
        ran (§20). Callers surface that execution instead of starting a second one.
        """
        return (
            bool(self.execution_id)
            and self.dry_run is not None
            and self.dry_run.ready
            and not self.already_executed
        )


class VerificationHandoff(BaseModel):
    """§29's payload: what the executor did, for the verifier to check against.

    Contains no verdict, no recovery signal and no "resolved" field — by construction,
    so the executor cannot accidentally become the authority on recovery. The verifier
    re-reads the *detection-time* signals itself.
    """

    execution_id: str
    incident_id: str
    service: str
    runbook_id: str
    runbook_version: int
    status: str  # ExecutionState.value at handoff — "completed"
    steps: list[dict[str, Any]] = Field(default_factory=list)
    actions_executed: list[dict[str, Any]] = Field(default_factory=list)
    rollback_status: str = "not_required"
    completed_at: str | None = None
    audit_metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutorResult(BaseModel):
    """§27's result contract.

    ``legacy`` carries the v0 :class:`RunbookExecution` verbatim when an execution
    actually ran, so every existing consumer (the dashboard's outcome poll, the eval
    harness, the audit tests) keeps reading the shape it always read while new callers
    use the fields above it.
    """

    status: ExecutorStatus
    next_action: NextAction
    reason: str = ""
    runbook_id: str | None = None
    runbook_version: int | None = None
    execution_id: str | None = None
    execution_state: ExecutionState | None = None
    ui_state: UiState = UiState.VALIDATING
    risk_level: str | None = None
    hitl_required: bool = False
    approval_id: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[RunbookCandidate] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rollback_status: str = "not_required"
    dry_run: DryRunReport | None = None
    verification_handoff: VerificationHandoff | None = None
    legacy: RunbookExecution | None = None
    duplicate_of: str | None = None  # execution_id this request collapsed onto

    @property
    def executed(self) -> bool:
        return self.status is ExecutorStatus.EXECUTED

    def to_api_dict(self) -> dict[str, Any]:
        """JSON-safe view for the HTTP layer, with the legacy body inlined.

        ``steps_total`` / ``steps_executed`` / ``destructive_steps`` are flattened the
        same way ``agent.run`` and the demo server already flatten them — they are
        computed properties, so they would otherwise vanish on serialization.
        """
        payload = self.model_dump(mode="json", exclude={"legacy"})
        if self.legacy is not None:
            payload["legacy"] = self.legacy.model_dump(mode="json")
            payload["steps_total"] = self.legacy.steps_total
            payload["steps_executed"] = self.legacy.steps_executed
            payload["destructive_steps"] = self.legacy.destructive_steps
        return payload


def from_record(record: ExecutionRecord) -> ExecutorResult:
    """Rebuild the result contract from a persisted execution.

    This is what makes a duplicate request answerable: the second caller gets the
    first execution's outcome instead of a second production run (§20). A non-terminal
    record reports ``BLOCKED`` — "in flight, not yours to start" — rather than
    inventing a status for a run that has not finished.
    """
    from agents.runbook_executor.execution_state import terminal_outcome

    outcome = terminal_outcome(record.state)
    if outcome is None:
        status, next_action = ExecutorStatus.BLOCKED, NextAction.ESCALATE
        reason = (
            f"execution {record.execution_id} for this incident and plan is already "
            f"{record.state.value} — returning its state instead of executing again"
        )
    else:
        status, next_action = outcome
        reason = (
            record.reason or f"execution {record.execution_id} finished as {record.state.value}"
        )
    return ExecutorResult(
        status=status,
        next_action=next_action,
        reason=reason,
        runbook_id=record.runbook_id or None,
        runbook_version=record.runbook_version,
        execution_id=record.execution_id,
        execution_state=record.state,
        ui_state=UiState(record.state.value.upper())
        if record.state.value.upper() in UiState.__members__
        else UiState.VALIDATING,
        risk_level=record.risk_level or None,
        hitl_required=record.hitl_required,
        approval_id=record.approval_id,
        steps=list(record.steps),
        rollback_status=record.rollback_status,
        duplicate_of=record.execution_id,
    )


def discovery_to_plan(result: DiscoveryResult) -> PlanResult:
    """Project a discovery result into a plan result with no runbook selected yet."""
    ui = {
        DiscoveryDecision.AUTO_SELECT: UiState.RUNBOOK_SELECTED,
        DiscoveryDecision.CANDIDATES: UiState.RUNBOOKS_FOUND,
        DiscoveryDecision.AMBIGUOUS: UiState.BLOCKED,
        DiscoveryDecision.BLOCKED: UiState.BLOCKED,
        DiscoveryDecision.NOT_APPLICABLE: UiState.BLOCKED,
        DiscoveryDecision.NO_RUNBOOK: UiState.NO_RUNBOOK,
    }[result.decision]
    return PlanResult(
        decision=result.decision,
        reason=result.reason,
        candidates=result.candidates,
        ui_state=ui,
        blocking_reasons=[r for c in result.candidates for r in c.blocking_reasons]
        if result.decision
        in (
            DiscoveryDecision.BLOCKED,
            DiscoveryDecision.NOT_APPLICABLE,
            DiscoveryDecision.AMBIGUOUS,
        )
        else [],
    )


# How a discovery decision that cannot proceed maps onto the §27 result contract.
DECISION_STATUS: dict[DiscoveryDecision, tuple[ExecutorStatus, NextAction]] = {
    DiscoveryDecision.NO_RUNBOOK: (ExecutorStatus.NO_RUNBOOK, NextAction.RCA),
    DiscoveryDecision.NOT_APPLICABLE: (ExecutorStatus.NOT_APPLICABLE, NextAction.RCA),
    DiscoveryDecision.AMBIGUOUS: (ExecutorStatus.AMBIGUOUS, NextAction.RCA),
    DiscoveryDecision.BLOCKED: (ExecutorStatus.BLOCKED, NextAction.RCA),
    DiscoveryDecision.CANDIDATES: (ExecutorStatus.AMBIGUOUS, NextAction.RCA),
}


__all__ = [
    "DECISION_STATUS",
    "ExecutorResult",
    "PlanResult",
    "VerificationHandoff",
    "discovery_to_plan",
    "from_record",
]
