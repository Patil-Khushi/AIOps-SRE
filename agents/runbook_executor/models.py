"""Pydantic models for the Runbook Executor (RA-004).

Input is a classified incident; output is an audit-grade :class:`RunbookExecution`
that records, per step, the dry-run preview, the executed result, whether it was
rolled back, and the final resolution status + rollback artifacts (the catalog's
declared outputs: *execution log, resolution status, rollback artifacts*).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.runbook_executor.events import AuditEvent
from agents.runbook_executor.simulation import SimulationComparison, SimulationDetail

# Final resolution of a runbook run. Mirrors the four real outcomes plus the
# "we had nothing to run" case the selector can produce.
ResolutionStatus = Literal["resolved", "rolled_back", "denied", "failed", "no_runbook"]

# Per-step execution outcome (the destructive path can be blocked at the gate).
StepStatus = Literal["executed", "denied", "failed", "rolled_back", "skipped"]


class RunbookStatus(StrEnum):
    """Review/publication lifecycle of an *executable* runbook.

    Only ``ACTIVE`` — and only with a recorded ``approved_by`` — may execute. Every
    other value is a refusal, including ``APPROVED``: approval says a human signed
    off on the content, activation says this is the version the executor should
    reach for, and a superseded-but-approved version must not run.

    Distinct from ``aiops.runbooks.models.ReviewStatus``, which is the *descriptive*
    library's lifecycle (draft → pending_review → published → rejected) for prose
    runbooks the Knowledge Synthesizer writes. That library has no executable steps,
    so the two vocabularies stay separate rather than one being widened to serve both.
    """

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class Prerequisite(BaseModel):
    """One precondition a runbook declares before it may be executed.

    ``check`` names *how* the platform evaluates it, from a closed vocabulary the
    evaluator understands (``agents/runbook_executor/applicability.py``). It is a
    plain ``str`` rather than a ``Literal`` on purpose: an unrecognised check must
    degrade to UNKNOWN (which blocks a mandatory prerequisite) instead of making the
    whole runbook unparseable and therefore invisible.

    ``mandatory=True`` is the default — a prerequisite whose author forgot to say
    how important it is gates execution rather than being waved through.
    """

    id: str
    description: str = ""
    mandatory: bool = True
    check: str = "manual"
    # For ``check="signal_present"``: the signal token that must be observed.
    signal: str = ""


class ApplicabilityScope(BaseModel):
    """What incident this runbook is *declared* to handle, and what it may touch.

    Every list is "empty means unconstrained" — a runbook that names no environments
    applies in any, and scores no environment points either way. The one field that
    is never unconstrained by omission is ``allowed_services``: parameter validation
    falls back to the runbook's own ``service`` when it is empty, so a step can never
    reach a second service just because nobody wrote the allow-list (§12).
    """

    # Incident-side matching facets.
    environments: list[str] = Field(default_factory=list)
    failure_category: str = ""
    alerts: list[str] = Field(default_factory=list)
    incident_types: list[str] = Field(default_factory=list)
    required_signals: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)
    # Execution-side scope (consumed by actions.validate_params).
    allowed_services: list[str] = Field(default_factory=list)
    allowed_namespaces: list[str] = Field(default_factory=list)


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
    reverse op invoked when a later step fails.

    The default is ``True`` (fail-closed): a runbook step that *forgets* to
    declare ``destructive`` is gated for human approval rather than silently
    running autonomously. A step is autonomous only when it explicitly opts in
    with ``destructive: false``."""

    name: str
    action: str
    destructive: bool = True
    idempotent: bool = True
    rollback_action: str | None = None
    target: str | None = None
    namespace: str = "otel-demo"
    params: dict[str, Any] = Field(default_factory=dict)


class StepRecord(BaseModel):
    """Evidence captured for a single step across the phases it went through.

    The fields below ``status`` are the v0 shape and are unchanged. Everything after
    the "execution detail" divider was added for the production upgrade (§21) and is
    optional with an inert default, so a consumer written against the v0 shape — the
    dashboard, the goldens, the audit tests — reads exactly what it read before.
    """

    name: str
    action: str
    destructive: bool
    status: StepStatus
    simulate: dict[str, Any] | None = None
    # Typed view of the dry-run prediction (predicted actions, warnings,
    # estimated duration, predicted side effects, summary). ``simulate`` above
    # is kept as the raw provider envelope; ``simulation`` is the structured
    # extraction used by the audit trail + the comparison below.
    simulation: SimulationDetail | None = None
    # Structured diff of the prediction vs. the actual execution result. Set for
    # executed / failed / rolled_back steps (a rolled-back step carries the
    # comparison computed when it executed forward). None for skipped / denied.
    comparison: SimulationComparison | None = None
    executed: dict[str, Any] | None = None
    rolled_back: bool = False
    rollback: dict[str, Any] | None = None
    error: str | None = None

    # ── execution detail (§21) ───────────────────────────────────────────────
    # ``step_id`` duplicates ``name`` today (a step name is unique within a runbook
    # version) and exists so a future stable id can be introduced without another
    # contract change.
    step_id: str = ""
    action_id: str = ""
    target: str = ""
    namespace: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    capability: str = ""
    attempts: int = 0
    # The approval that authorized THIS step. A run with two destructive steps needs two
    # approvals, so the run-level id is not enough to say what authorized what.
    approval_id: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: float | None = None
    # "not_required" | "rolled_back" | "rollback_failed" | "pending"
    rollback_status: str = "not_required"
    timed_out: bool = False


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
    # Append-only, ordered stream of what happened during the run (issue #213).
    # A tuple (immutable) so the serialized result can't be appended-to,
    # reordered, or cleared post-hoc; combined with the frozen AuditEvent /
    # AuditEventMetadata, the log is immutable end to end. The run's EventLog is
    # the authoritative append-only writer; this is its final snapshot. Kept
    # separate from ``steps`` so it never affects steps_total / steps_executed /
    # destructive_steps.
    audit_events: tuple[AuditEvent, ...] = Field(default_factory=tuple)

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
