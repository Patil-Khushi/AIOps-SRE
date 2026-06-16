"""Runbook Executor (RA-004) — selects and runs the right runbook for a
classified incident, with step-level guardrails.

Flow per run::

    select runbook (service + tags + severity)        agents.runbook_executor.selector
      └─ none?  -> RunbookExecution(status="no_runbook")
    dry-run preview every step                        automation.runbook.simulate  (NONE)
    for each step in order:
        destructive?  -> automation.runbook.execute   (REQUIRED — platform HITL gate)
        otherwise     -> automation.runbook.apply      (NONE — autonomous)
        gate blocked  -> status="denied", stop (nothing past the gate runs)
        tool failed   -> roll back prior steps in reverse, status in {rolled_back, failed}
    all ok -> status="resolved"

The agent never gate-checks itself (CLAUDE.md #3): it calls capabilities through
``aiops.tools.get_registry()`` and the platform decides whether a destructive
step needs a human. The agent owns *policy* (which runbook, which step is
destructive, the rollback order); the platform owns *mechanism* (the gate).

Public surface::

    from agents.runbook_executor import execute_runbook, Incident
"""

from __future__ import annotations

import logging
from typing import Any

# Side-effect import: registers the mock automation.runbook.* providers.
import aiops.tools.mock_providers  # noqa: F401
from agents.runbook_executor.library import ExecutableRunbook, load_runbooks
from agents.runbook_executor.models import (
    Incident,
    RunbookExecution,
    RunbookStep,
    StepRecord,
)
from agents.runbook_executor.selector import select_runbook
from aiops.tools import ToolResult, get_registry

logger = logging.getLogger(__name__)

SIMULATE_CAP = "automation.runbook.simulate"
APPLY_CAP = "automation.runbook.apply"
EXECUTE_CAP = "automation.runbook.execute"


def select(incident: Incident, *, runbooks_dir: Any = None) -> ExecutableRunbook | None:
    """Load the library and pick the runbook for ``incident`` (or None)."""
    runbooks = load_runbooks(runbooks_dir)
    return select_runbook(
        runbooks, service=incident.service, tags=incident.tags, severity=incident.severity
    )


def _call(capability: str, step: RunbookStep, incident: Incident, **extra: Any) -> ToolResult:
    """Invoke a step through the registry. HITL is enforced at this boundary."""
    return get_registry().call(
        capability,
        step=step.name,
        target=step.target or incident.service,
        namespace=step.namespace,
        **extra,
    )


def _blocked_by_gate(result: ToolResult) -> bool:
    return not result.ok and (result.metadata or {}).get("blocked_by") == "hitl_gate"


def run_plan(
    incident: Incident,
    runbook: ExecutableRunbook,
    *,
    hitl_context: dict[str, Any] | None = None,
) -> RunbookExecution:
    """Execute an already-selected runbook for ``incident``.

    ``hitl_context`` is forwarded verbatim to the REQUIRED-HITL
    ``automation.runbook.execute`` calls — callers can pre-supply an
    ``approval_id`` / ``approval_timeout_seconds`` or set ``skip_approval=True``
    (the eval path) exactly as ``auto_healer_lite`` does.
    """
    ctx: dict[str, Any] = dict(hitl_context or {})
    execution = RunbookExecution(
        incident=incident,
        selected_runbook=runbook.id,
        runbook_title=runbook.title,
        status="resolved",
        reason="all steps executed",
    )

    # ── Phase 1: dry-run preview (autonomous, zero changes) ──────────────────
    records: dict[str, StepRecord] = {}
    for step in runbook.steps:
        rec = StepRecord(
            name=step.name, action=step.action, destructive=step.destructive, status="skipped"
        )
        sim = _call(SIMULATE_CAP, step, incident, action=step.action)
        rec.simulate = sim.data if sim.ok else {"error": sim.error}
        records[step.name] = rec
        execution.steps.append(rec)

    # ── Phase 2: execute in order, gating destructive steps ──────────────────
    executed: list[RunbookStep] = []
    for step in runbook.steps:
        rec = records[step.name]
        if step.destructive:
            result = _call(
                EXECUTE_CAP,
                step,
                incident,
                runbook=runbook.id,
                action=step.action,
                dry_run=False,
                mode="execute",
                hitl_context=ctx,
            )
            execution.approval_id = ctx.get("pending_approval_id") or execution.approval_id
            if _blocked_by_gate(result):
                rec.status = "denied"
                execution.status = "denied"
                execution.reason = f"destructive step {step.name!r} blocked at HITL gate"
                return execution
        else:
            result = _call(APPLY_CAP, step, incident, action=step.action, mode="execute")

        if result.ok:
            rec.status = "executed"
            rec.executed = result.data
            executed.append(step)
        else:
            rec.status = "failed"
            rec.executed = {"error": result.error}
            rec.error = result.error
            execution.reason = f"step {step.name!r} failed: {result.error}"
            _rollback(incident, executed, records, execution, ctx)
            return execution

    return execution


def _rollback(
    incident: Incident,
    executed: list[RunbookStep],
    records: dict[str, StepRecord],
    execution: RunbookExecution,
    ctx: dict[str, Any],
) -> None:
    """Undo previously executed steps in reverse order. A step with no
    ``rollback_action`` is considered trivially reverted. The rollback of a
    destructive step is itself routed through the REQUIRED capability; a
    non-destructive step's rollback runs autonomously."""
    all_ok = True
    for step in reversed(executed):
        rec = records[step.name]
        if not step.rollback_action:
            rec.rolled_back = True
            continue
        cap = EXECUTE_CAP if step.destructive else APPLY_CAP
        extra: dict[str, Any] = {"action": step.rollback_action, "mode": "rollback"}
        if step.destructive:
            # The reverse of a destructive step re-enters the REQUIRED gate. Don't
            # re-prompt a human mid-failure (principle #5): authorize it with the
            # approval already granted for the forward action. Drop ``approval_id``
            # so the gate can't try to mint a fresh approval under the same (now
            # taken) id; the platform verifies ``pre_authorized_by`` against the
            # registry before honouring it. With no original approval (e.g. the
            # eval/skip path), this is absent and the reverse gates as before.
            rb_ctx = {k: v for k, v in ctx.items() if k != "approval_id"}
            if execution.approval_id:
                rb_ctx["pre_authorized_by"] = execution.approval_id
            extra |= {
                "runbook": execution.selected_runbook,
                "dry_run": False,
                "hitl_context": rb_ctx,
            }
        result = _call(cap, step, incident, **extra)
        rec.rollback = result.data if result.ok else {"error": result.error}
        rec.rolled_back = result.ok
        execution.rollback_artifacts.append(
            {
                "step": step.name,
                "rollback_action": step.rollback_action,
                "ok": result.ok,
                "error": result.error,
            }
        )
        if result.ok:
            if rec.status == "executed":
                rec.status = "rolled_back"
        else:
            all_ok = False
    execution.status = "rolled_back" if all_ok else "failed"
    if not all_ok:
        execution.reason += " — rollback incomplete (manual intervention required)"


def execute_runbook(
    incident: Incident,
    *,
    runbooks_dir: Any = None,
    hitl_context: dict[str, Any] | None = None,
) -> RunbookExecution:
    """Top-level entry point: select a runbook for ``incident`` and run it."""
    runbook = select(incident, runbooks_dir=runbooks_dir)
    if runbook is None:
        return RunbookExecution(
            incident=incident,
            selected_runbook=None,
            status="no_runbook",
            reason=f"no runbook matched service={incident.service!r} tags={incident.tags}",
        )
    return run_plan(incident, runbook, hitl_context=hitl_context)


# ─── eval-harness contract (dict-in, dict-out) ───────────────────────────────


def reset_state() -> None:
    """Eval-harness hook. RA-004 is stateless — every run reloads the library
    and consults the live registry — but the harness expects the symbol."""
    return None


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract. Forces ``skip_approval=True`` so goldens never
    block on a pending HITL prompt (same rationale as ``auto_healer_lite.run``):
    destructive steps deterministically resolve to ``denied`` with no approver.
    The HITL happy path is covered by dedicated tests, not the eval harness."""
    incident = Incident.model_validate(input)
    execution = execute_runbook(incident, hitl_context={"skip_approval": True})
    out = execution.model_dump(mode="json")
    # Flatten a few scalars so the suffix-grammar scorer can assert on them.
    out["steps_total"] = execution.steps_total
    out["steps_executed"] = execution.steps_executed
    out["destructive_steps"] = execution.destructive_steps
    return out
