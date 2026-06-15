"""Pydantic models for the Runbook Executor (RA-004).

Input is a classified incident; output is an audit-grade :class:`RunbookExecution`
that records, per step, the dry-run preview, the executed result, whether it was
rolled back, and the final resolution status + rollback artifacts (the catalog's
declared outputs: *execution log, resolution status, rollback artifacts*).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Final resolution of a runbook run. Mirrors the four real outcomes plus the
# "we had nothing to run" case the selector can produce.
ResolutionStatus = Literal["resolved", "rolled_back", "denied", "failed", "no_runbook"]

# Per-step execution outcome (the destructive path can be blocked at the gate).
StepStatus = Literal["executed", "denied", "failed", "rolled_back", "skipped"]


class Incident(BaseModel):
    """The classified incident RA-004 receives (the RA-002 hand-off, trimmed to
    what selection needs). ``tags`` carry the symptom keywords the runbook
    library is matched against."""

    incident_id: str = Field("", description="Source incident / ticket id, for audit")
    service: str = Field(..., description="Impacted service, e.g. 'payment'")
    severity: str | None = Field(None, description="e.g. 'sev1' / 'Sev-2'")
    tags: list[str] = Field(default_factory=list, description="Symptom keywords")


class RunbookStep(BaseModel):
    """One executable step parsed from a runbook's frontmatter.

    ``destructive`` is the single bit that decides routing: destructive steps go
    through the REQUIRED-HITL ``automation.runbook.execute`` capability; the rest
    run autonomously via ``automation.runbook.apply``. ``rollback_action`` is the
    reverse op invoked when a later step fails."""

    name: str
    action: str
    destructive: bool = False
    idempotent: bool = True
    rollback_action: str | None = None
    target: str | None = None
    namespace: str = "otel-demo"
    params: dict[str, Any] = Field(default_factory=dict)


class StepRecord(BaseModel):
    """Evidence captured for a single step across the phases it went through."""

    name: str
    action: str
    destructive: bool
    status: StepStatus
    simulate: dict[str, Any] | None = None
    executed: dict[str, Any] | None = None
    rolled_back: bool = False
    rollback: dict[str, Any] | None = None
    error: str | None = None


class RunbookExecution(BaseModel):
    """The full result of selecting and running a runbook for an incident."""

    incident: Incident
    selected_runbook: str | None = Field(None, description="Runbook id, or None when no match")
    runbook_title: str | None = None
    status: ResolutionStatus
    steps: list[StepRecord] = Field(default_factory=list)
    rollback_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    approval_id: str | None = Field(None, description="HITL approval id, if one was opened")
    reason: str = ""

    # ─── convenience views for callers / evals ──────────────────────────────

    @property
    def steps_total(self) -> int:
        return len(self.steps)

    @property
    def steps_executed(self) -> int:
        return sum(1 for s in self.steps if s.status == "executed")

    @property
    def destructive_steps(self) -> int:
        return sum(1 for s in self.steps if s.destructive)
