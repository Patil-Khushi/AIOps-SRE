"""Runbook Executor — RA-004 (Reactive-Active).

Answers one question: *do we have a known, approved, safe procedure for this incident?*
It finds candidate runbooks, ranks them with a deterministic explainable score, lets an
SRE choose among the applicable ones, re-validates whatever was chosen, dry-runs it,
executes it exactly once behind the platform HITL gate, rolls back on failure, and hands
the result to the Resolution Verifier.

It does **not** determine root cause (that is ``agents.rca_agent``), and it does not
decide whether the incident recovered (that is ``agents.resolution_verifier``). The
strongest thing it can report is ``EXECUTED`` / ``next_action=VERIFY``.

Two generations of entry point, both supported:

- **v0 / legacy** — ``execute_runbook(Incident)`` selects by service + tags and runs the
  plan, returning :class:`RunbookExecution`. Unchanged, still used by the CLI, the
  existing demo route and the eval harness.
- **production** — ``discover_candidates`` → ``plan_execution`` → ``execute_plan``
  (or ``execute`` for both at once), returning :class:`PlanResult` /
  :class:`ExecutorResult` with candidates, dry run, durable execution state and the
  verifier handoff.

Public surface::

    from agents.runbook_executor import discover_candidates, plan_execution, execute_plan
    from agents.runbook_executor import execute_runbook, Incident, IncidentContext
"""

from agents.runbook_executor.actions import (
    ACTION_SPECS,
    ActionSpec,
    AutonomyClass,
    BlastRadius,
    StepValidation,
    resolve_action,
    validate_runbook,
    validate_step,
)
from agents.runbook_executor.agent import (
    discover_candidates,
    execute,
    execute_plan,
    execute_runbook,
    plan_execution,
    run,
    run_plan,
    select,
)
from agents.runbook_executor.applicability import (
    ApplicabilityResult,
    ApplicabilityStatus,
    IncidentContext,
    PrerequisiteStatus,
)
from agents.runbook_executor.dryrun import (
    DryRunReport,
    DryRunStatus,
    PlannedStepView,
    dry_run,
    render_summary,
)
from agents.runbook_executor.events import (
    AuditEvent,
    AuditEventMetadata,
    AuditEventType,
    EventLog,
    redact,
)
from agents.runbook_executor.execution_state import (
    ExecutionRecord,
    ExecutionState,
    ExecutorStatus,
    NextAction,
    UiState,
    ui_state_for,
)
from agents.runbook_executor.library import ExecutableRunbook, get_runbook, load_runbooks
from agents.runbook_executor.matching import (
    DiscoveryDecision,
    DiscoveryResult,
    RunbookCandidate,
    discover,
)
from agents.runbook_executor.models import (
    ApplicabilityScope,
    Incident,
    Prerequisite,
    RunbookExecution,
    RunbookStatus,
    RunbookStep,
    StepRecord,
)
from agents.runbook_executor.results import ExecutorResult, PlanResult, VerificationHandoff
from agents.runbook_executor.risk import PlanRisk, RiskLevel, StepRisk, assess_plan, assess_step
from agents.runbook_executor.simulation import (
    SimulationComparison,
    SimulationDetail,
    compare_simulation,
)

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "ApplicabilityResult",
    "ApplicabilityScope",
    "ApplicabilityStatus",
    "AuditEvent",
    "AuditEventMetadata",
    "AuditEventType",
    "AutonomyClass",
    "BlastRadius",
    "DiscoveryDecision",
    "DiscoveryResult",
    "DryRunReport",
    "DryRunStatus",
    "EventLog",
    "ExecutableRunbook",
    "ExecutionRecord",
    "ExecutionState",
    "ExecutorResult",
    "ExecutorStatus",
    "Incident",
    "IncidentContext",
    "NextAction",
    "PlanResult",
    "PlanRisk",
    "PlannedStepView",
    "Prerequisite",
    "PrerequisiteStatus",
    "RiskLevel",
    "RunbookCandidate",
    "RunbookExecution",
    "RunbookStatus",
    "RunbookStep",
    "SimulationComparison",
    "SimulationDetail",
    "StepRecord",
    "StepRisk",
    "StepValidation",
    "UiState",
    "VerificationHandoff",
    "assess_plan",
    "assess_step",
    "compare_simulation",
    "discover",
    "discover_candidates",
    "dry_run",
    "execute",
    "execute_plan",
    "execute_runbook",
    "get_runbook",
    "load_runbooks",
    "plan_execution",
    "redact",
    "render_summary",
    "resolve_action",
    "run",
    "run_plan",
    "select",
    "ui_state_for",
    "validate_runbook",
    "validate_step",
]
