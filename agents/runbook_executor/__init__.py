"""Runbook Executor — RA-004 (Reactive-Active).

Selects and executes the appropriate runbook for a classified incident with
step-level guardrails: a dry-run preview, a platform HITL gate on destructive
steps, autonomous execution of non-destructive steps, and automatic rollback
on failure — with an audit-grade execution log.

Public surface::

    from agents.runbook_executor import execute_runbook, Incident, RunbookExecution
"""

from agents.runbook_executor.agent import execute_runbook, run, run_plan, select
from agents.runbook_executor.events import (
    AuditEvent,
    AuditEventMetadata,
    AuditEventType,
    EventLog,
)
from agents.runbook_executor.library import ExecutableRunbook, load_runbooks
from agents.runbook_executor.models import (
    Incident,
    RunbookExecution,
    RunbookStep,
    StepRecord,
)
from agents.runbook_executor.simulation import (
    SimulationComparison,
    SimulationDetail,
    compare_simulation,
)

__all__ = [
    "AuditEvent",
    "AuditEventMetadata",
    "AuditEventType",
    "EventLog",
    "ExecutableRunbook",
    "Incident",
    "RunbookExecution",
    "RunbookStep",
    "SimulationComparison",
    "SimulationDetail",
    "StepRecord",
    "compare_simulation",
    "execute_runbook",
    "load_runbooks",
    "run",
    "run_plan",
    "select",
]
