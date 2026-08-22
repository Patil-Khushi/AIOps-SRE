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

import contextlib
import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Side-effect import: registers the mock automation.runbook.* providers.
import aiops.tools.mock_providers  # noqa: F401
from agents.runbook_executor import actions as actions_mod
from agents.runbook_executor import matching, metrics
from agents.runbook_executor.applicability import IncidentContext
from agents.runbook_executor.dryrun import dry_run
from agents.runbook_executor.events import AuditEventType, EventLog, redact
from agents.runbook_executor.execution_state import (
    ExecutionRecord,
    ExecutionState,
    ExecutorStatus,
    NextAction,
    UiState,
    assert_transition,
    idempotency_key,
    lease_seconds,
    new_execution_id,
    resource_key,
    ui_state_for,
    utcnow,
)
from agents.runbook_executor.library import ExecutableRunbook, get_runbook, load_runbooks
from agents.runbook_executor.matching import DiscoveryDecision, DiscoveryResult
from agents.runbook_executor.models import (
    Incident,
    RunbookExecution,
    RunbookStep,
    StepRecord,
)
from agents.runbook_executor.results import (
    DECISION_STATUS,
    ExecutorResult,
    PlanResult,
    VerificationHandoff,
    discovery_to_plan,
    from_record,
)
from agents.runbook_executor.selector import select_runbook
from agents.runbook_executor.simulation import SimulationDetail, compare_simulation
from aiops.state import repository
from aiops.tools import ToolResult, get_registry
from aiops.tools.resilience import guard

logger = logging.getLogger(__name__)

# Re-exported from the action registry, which is now the single place that knows
# which capability an action dispatches through. The names stay here because tests and
# demo code import them from this module.
SIMULATE_CAP = actions_mod.SIMULATE_CAP
APPLY_CAP = actions_mod.APPLY_CAP
EXECUTE_CAP = actions_mod.EXECUTE_CAP

# A dispatcher takes (capability, step, kwargs) and returns a ToolResult. Injected so
# the new execution path can wrap each call in timeout/retry/breaker protection without
# changing how the legacy path dispatches (which is: straight at the registry).
Dispatch = Callable[[str, RunbookStep, dict[str, Any]], ToolResult]


def select(incident: Incident, *, runbooks_dir: Any = None) -> ExecutableRunbook | None:
    """Load the library and pick the runbook for ``incident`` (or None)."""
    runbooks = load_runbooks(runbooks_dir)
    return select_runbook(
        runbooks,
        service=incident.service,
        tags=incident.tags,
        severity=incident.severity,
    )


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _registry_dispatch(capability: str, _step: RunbookStep, kwargs: dict[str, Any]) -> ToolResult:
    """The default dispatcher: straight at the registry, exactly as RA-004 v0 did."""
    return get_registry().call(capability, **kwargs)


def guarded_dispatch(capability: str, step: RunbookStep, kwargs: dict[str, Any]) -> ToolResult:
    """Dispatch one step with per-step timeout, retry and circuit breaking (§23).

    Two rules make this safe on a mutation path, and both are the opposite of what a
    naive retry wrapper does:

    1. **Only retry-safe actions are retried.** ``step_policy`` zeroes the retry count
       for anything the action registry does not mark ``retry_safe``, because a timeout
       proves the call did not *answer*, not that it did not *happen* — re-issuing a
       rollout after one would restart the deployment twice.
    2. **Nothing that passes the HITL gate is ever retried, and its timeout includes the
       approval window.** ``automation.runbook.execute`` blocks inside the registry
       while a human decides; a step timeout shorter than the approval timeout would
       report failure for a call that is still waiting to be approved, and a retry would
       ask the human a second time. So the effective timeout is the step budget plus the
       caller's approval window, and retries are forced to zero.

    Caching is never enabled (``cache_ttl=0``): a cached mutation result would report
    success for a call that never ran.
    """
    from agents.runbook_executor.execution_state import step_policy

    # The action actually being dispatched — for a rollback that is the reverse action,
    # whose retry-safety and disruptiveness differ from the forward step's. Taking the
    # policy from ``step.action`` would let a non-retry-safe reverse be retried because
    # the forward action happened to be idempotent.
    dispatched_action = str(kwargs.get("action") or step.action)
    spec = actions_mod.resolve_action(dispatched_action)
    policy = step_policy(spec)
    if capability == EXECUTE_CAP:
        approval_window = float(
            (kwargs.get("hitl_context") or {}).get("approval_timeout_seconds") or 120
        )
        policy = dataclasses.replace(policy, retries=0, timeout=policy.timeout + approval_window)

    def _is_transient(result: ToolResult) -> bool:
        """A refusal is not a transient fault. Only genuine tool failures retry."""
        if result.ok:
            return False
        meta = result.metadata or {}
        return not (meta.get("blocked_by") or meta.get("missing_provider"))

    # The breaker key includes the CAPABILITY, so a read-only preview can never
    # short-circuit the mutation it previews. With one shared key, repeated
    # ``automation.runbook.simulate`` failures tripped the breaker for
    # ``runbook.<action>`` and the following approved ``automation.runbook.execute``
    # was refused with attempts=0 — a preview vetoing an approved recovery.
    breaker_key = f"runbook.{capability.rsplit('.', 1)[-1]}.{dispatched_action}"
    outcome = guard(
        breaker_key,
        lambda: get_registry().call(capability, **kwargs),
        policy=policy,
        is_transient=_is_transient,
    )
    if outcome.value is not None:
        result = outcome.value
        meta = dict(result.metadata or {})
        meta.setdefault("attempts", outcome.attempts)
        return ToolResult(ok=result.ok, data=result.data, error=result.error, metadata=meta)
    reason = (
        "timed out"
        if outcome.timed_out
        else "circuit breaker open"
        if outcome.breaker_open
        else "no worker slot"
        if outcome.starved
        else (outcome.error or "dispatch failed")
    )
    return ToolResult(
        ok=False,
        error=f"step {step.name!r} {reason} after {outcome.attempts} attempt(s)",
        metadata={
            "timed_out": outcome.timed_out,
            "breaker_open": outcome.breaker_open,
            "starved": outcome.starved,
            "attempts": outcome.attempts,
        },
    )


def _call(
    capability: str,
    step: RunbookStep,
    incident: Incident,
    *,
    dispatch: Dispatch | None = None,
    **extra: Any,
) -> ToolResult:
    """Invoke a step through the registry. HITL is enforced at this boundary."""
    kwargs: dict[str, Any] = {
        "step": step.name,
        "target": step.target or incident.service,
        "namespace": step.namespace,
        **extra,
    }
    # Validated step parameters ride along so a provider that understands them can use
    # them and the audit trail records what was passed. The registry filters kwargs by
    # provider signature, so a provider that does not accept them is unaffected.
    # ``params`` passed by the caller (already validated, possibly overridden) wins over
    # the raw frontmatter values; an explicit empty dict means "no parameters".
    if kwargs.get("params") is None:
        kwargs.pop("params", None)
        if step.params:
            kwargs["params"] = dict(step.params)
    return (dispatch or _registry_dispatch)(capability, step, kwargs)


def _step_hitl_context(ctx: dict[str, Any], step_name: str, gated_index: int) -> dict[str, Any]:
    """The HITL context for one gated step, with an approval id of its own.

    A run may need more than one approval. The caller pre-mints a single
    ``approval_id`` so a UI can start polling immediately, but reusing it for a second
    gated step makes ``ApprovalRegistry.create`` raise ``approval id collision`` — and
    that happens *after* the first destructive step has already changed production, so
    the run dies mid-way and has to roll back a change a human had approved.

    The first gated step keeps the caller's id (so the poll the UI already started keeps
    working); later ones are suffixed with the step name, which is unique within a
    runbook version. Each id is recorded on its own step in the audit trail.
    """
    step_ctx = dict(ctx)
    base = str(ctx.get("approval_id") or "")
    if base and gated_index > 0:
        step_ctx["approval_id"] = f"{base}:{step_name}"
    # Never inherit a previous step's writeback — it would be read back as this step's.
    step_ctx.pop("pending_approval_id", None)
    return step_ctx


def _finish_timing(rec: StepRecord, started: float, result: ToolResult) -> None:
    """Stamp duration / attempts / timeout onto a step record after its call returns."""
    meta = result.metadata or {}
    rec.completed_at = _utc_iso()
    rec.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    rec.attempts = int(meta.get("attempts") or 1)
    rec.timed_out = bool(meta.get("timed_out"))


def _blocked_by_gate(result: ToolResult) -> bool:
    return not result.ok and (result.metadata or {}).get("blocked_by") == "hitl_gate"


def run_plan(
    incident: Incident,
    runbook: ExecutableRunbook,
    *,
    hitl_context: dict[str, Any] | None = None,
    dispatch: Dispatch | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> RunbookExecution:
    """Execute an already-selected runbook for ``incident``.

    ``hitl_context`` is forwarded verbatim to the REQUIRED-HITL
    ``automation.runbook.execute`` calls — callers can pre-supply an
    ``approval_id`` / ``approval_timeout_seconds`` or set ``skip_approval=True``
    (the eval path) exactly as ``auto_healer_lite`` does.

    ``dispatch`` overrides how each step reaches the registry. The default is the
    direct call RA-004 has always made; the production path passes
    :func:`guarded_dispatch` for per-step timeout/retry/breaker protection (§23). The
    gate is consulted inside the registry either way — a dispatcher cannot route
    around it, it only decides how long to wait and whether a *failed* call is retried.

    ``overrides`` are per-step parameter overrides, keyed by step name, that the plan was
    authorized with. They are re-validated here against the action registry before they
    are sent: a value that fails validation refuses the step rather than being dropped
    silently, because a dropped override means the operator approved one plan and a
    different one ran.
    """
    # §9's gate, checked HERE rather than only on the discovery path, so every entry
    # point inherits it: the CLI, the legacy demo route and the eval harness all call
    # execute_runbook/run_plan directly, and none of them consulted the runbook's
    # lifecycle. A DRAFT, SUPERSEDED or ARCHIVED runbook — or one whose status was
    # fat-fingered, which the loader coerces to DRAFT — could run its steps.
    lifecycle_refusal = runbook.executability_reason()
    if lifecycle_refusal:
        return RunbookExecution(
            incident=incident,
            selected_runbook=runbook.id,
            runbook_title=runbook.title,
            status="denied",
            reason=lifecycle_refusal,
        )

    ctx: dict[str, Any] = dict(hitl_context or {})
    # Keyed by POSITION, not by name: a runbook with two steps sharing a name would
    # otherwise dispatch the first with the second's validated parameters. Names remain
    # the operator-facing key for overrides (and the library refuses a runbook whose
    # names collide), but nothing in the execution core relies on that being true.
    step_params: list[dict[str, Any]] = []
    override_errors: list[str] = []
    for step in runbook.steps:
        validation = actions_mod.validate_step(
            step, runbook, overrides=(overrides or {}).get(step.name)
        )
        step_params.append(dict(validation.parameters))
        override_errors += validation.errors
    if overrides and override_errors:
        # Only refuse over an override the caller supplied: a library runbook whose own
        # steps do not validate is the dry run's problem (and is blocked there), and
        # failing here too would change the legacy path's behaviour.
        return RunbookExecution(
            incident=incident,
            selected_runbook=runbook.id,
            runbook_title=runbook.title,
            status="denied",
            reason="parameter overrides failed validation: " + "; ".join(override_errors),
        )
    execution = RunbookExecution(
        incident=incident,
        selected_runbook=runbook.id,
        runbook_title=runbook.title,
        status="resolved",
        reason="all steps executed",
    )
    # Append-only audit trail for this run (issue #213). Emitters below only
    # *observe* — every gate-related event is reconstructed from the ToolResult
    # the registry already returned; the gate is never re-checked here.
    log = EventLog(incident_id=incident.incident_id, runbook_id=runbook.id)

    # ── Phase 1: dry-run preview (autonomous, zero changes) ──────────────────
    # A list, parallel to ``runbook.steps``. It used to be a dict keyed by step name,
    # which collapsed two steps that shared a name: the second overwrote the first, so
    # one genuinely executed step was reported as "skipped" with its result, timing and
    # rollback status lost. ``execution.steps`` and this list are the same objects in the
    # same order.
    records: list[StepRecord] = []
    for position, step in enumerate(runbook.steps):
        rec = StepRecord(
            name=step.name,
            action=step.action,
            destructive=step.destructive,
            status="skipped",
            step_id=step.name,
            action_id=step.action,
            target=step.target or incident.service,
            namespace=step.namespace,
            parameters=dict(step_params[position]),
            capability=actions_mod.capability_for(step),
        )
        sim = _call(
            SIMULATE_CAP,
            step,
            incident,
            dispatch=dispatch,
            action=step.action,
            params=step_params[position],
        )
        rec.simulate = sim.data if sim.ok else {"error": sim.error}
        rec.simulation = SimulationDetail.from_provider(sim.data if sim.ok else None)
        records.append(rec)
        execution.steps.append(rec)
        log.emit(
            AuditEventType.STEP_SIMULATED,
            step_id=step.name,
            reason=rec.simulation.summary,
            simulated_ok=sim.ok,
        )

    # ── Phase 2: execute in order, gating destructive steps ──────────────────
    # (step, its record, its position) — so a reverse updates the record of the step it
    # actually reverses, even when another step shares its name.
    executed: list[tuple[int, RunbookStep]] = []
    gated_index = 0  # how many steps have already been through the gate this run
    for position, (step, rec) in enumerate(zip(runbook.steps, records, strict=True)):
        log.emit(AuditEventType.STEP_STARTED, step_id=step.name, destructive=step.destructive)
        rec.started_at = _utc_iso()
        started = time.perf_counter()
        if step.destructive:
            step_ctx = _step_hitl_context(ctx, step.name, gated_index)
            gated_index += 1
            result = _call(
                EXECUTE_CAP,
                step,
                incident,
                dispatch=dispatch,
                runbook=runbook.id,
                action=step.action,
                params=step_params[position],
                dry_run=False,
                mode="execute",
                hitl_context=step_ctx,
            )
            _finish_timing(rec, started, result)
            approval_id = step_ctx.get("pending_approval_id") or ""
            rec.approval_id = approval_id
            execution.approval_id = approval_id or execution.approval_id
            blocked = _blocked_by_gate(result)
            log.emit(
                AuditEventType.GATE_CHECKED,
                step_id=step.name,
                gate_type="required",
                approval_id=approval_id,
                reason=(result.error or "") if blocked else "allowed",
            )
            # An approval was opened iff the gate flow wrote a pending id.
            if approval_id:
                log.emit(
                    AuditEventType.HITL_REQUESTED,
                    step_id=step.name,
                    approval_id=approval_id,
                )
            if blocked:
                rec.status = "denied"
                execution.status = "denied"
                execution.reason = f"destructive step {step.name!r} blocked at HITL gate"
                log.emit(
                    AuditEventType.STEP_BLOCKED,
                    step_id=step.name,
                    gate_type="required",
                    approval_id=approval_id,
                    reason=result.error or "blocked at HITL gate",
                )
                execution.audit_events = log.events
                return execution
            # Not blocked ⇒ the gate approved the destructive step (a human, a
            # pre-authorization, or skip in eval). Record the approval here —
            # independent of whether the subsequent tool call succeeds — so an
            # approved-but-then-failed step still shows HITL_APPROVED in the log.
            log.emit(
                AuditEventType.HITL_APPROVED,
                step_id=step.name,
                approval_id=approval_id,
                reason=str(ctx.get("approver") or "approved"),
            )
        else:
            result = _call(
                APPLY_CAP,
                step,
                incident,
                dispatch=dispatch,
                action=step.action,
                params=step_params[position],
                mode="execute",
            )
            _finish_timing(rec, started, result)
            log.emit(
                AuditEventType.GATE_CHECKED,
                step_id=step.name,
                gate_type="none",
                reason="autonomous (level=none)",
            )

        # Sim-vs-execution comparison — computed once here, from the single
        # forward result, for both executed and failed outcomes. A step later
        # rolled back keeps this forward comparison.
        rec.comparison = compare_simulation(rec.simulation, result.data if result.ok else None)

        if result.ok:
            rec.status = "executed"
            rec.executed = result.data
            executed.append((position, step))
            log.emit(AuditEventType.STEP_EXECUTED, step_id=step.name)
        else:
            rec.status = "failed"
            rec.executed = {"error": result.error}
            rec.error = result.error
            execution.reason = f"step {step.name!r} failed: {result.error}"
            log.emit(
                AuditEventType.STEP_FAILED,
                step_id=step.name,
                reason=result.error or "failed",
            )
            rec.rollback_status = "pending"
            _rollback(incident, executed, records, execution, ctx, log, dispatch=dispatch)
            execution.audit_events = log.events
            return execution

    execution.audit_events = log.events
    return execution


def _rollback(
    incident: Incident,
    executed: list[tuple[int, RunbookStep]],
    records: list[StepRecord],
    execution: RunbookExecution,
    ctx: dict[str, Any],
    log: EventLog,
    *,
    dispatch: Dispatch | None = None,
) -> None:
    """Undo previously executed steps in reverse order. A step with no
    ``rollback_action`` is considered trivially reverted. The rollback of a
    destructive step is itself routed through the REQUIRED capability; a
    non-destructive step's rollback runs autonomously.

    Emits ``STEP_ROLLED_BACK`` per successfully reverted step, and
    ``STEP_FAILED`` (reason ``rollback failed``) when a reverse op itself
    fails — the emitter observes the reverse ToolResult, it never re-gates.

    **The capability is chosen from the REVERSE action, not the forward one.** Routing a
    reverse by the forward step's ``destructive`` flag is how a disruptive reverse
    escapes the gate: a step declaring ``destructive: false`` with
    ``rollback_action: flush_cache`` would dispatch an irreversible, multi-service,
    HUMAN_APPROVAL action through the autonomous ``apply`` capability, which the policy
    gate maps to level NONE. The forward flag says nothing about what undoing it costs.
    """
    all_ok = True
    for position, step in reversed(executed):
        rec = records[position]
        if not step.rollback_action:
            rec.rolled_back = True
            rec.rollback_status = "not_required"
            log.emit(
                AuditEventType.STEP_ROLLED_BACK,
                step_id=step.name,
                reason="no rollback action; trivially reverted",
            )
            continue
        reverse_spec = actions_mod.resolve_action(step.rollback_action)
        # Gate the reverse when the reverse itself is disruptive, when the forward step
        # was (its reverse touches the same ground), or when the reverse action is
        # unknown to the registry — an unresolvable action is treated as the most
        # dangerous thing it could be, never the least.
        gated = step.destructive or reverse_spec is None or reverse_spec.disruptive
        cap = EXECUTE_CAP if gated else APPLY_CAP
        extra: dict[str, Any] = {"action": step.rollback_action, "mode": "rollback"}
        if gated:
            # The reverse of a destructive step re-enters the REQUIRED gate. Don't
            # re-prompt a human mid-failure (principle #5): authorize it with the
            # approval already granted for the forward action. Drop ``approval_id``
            # so the gate can't try to mint a fresh approval under the same (now
            # taken) id; the platform verifies ``pre_authorized_by`` against the
            # registry before honouring it. With no original approval (e.g. the
            # eval/skip path), this is absent and the reverse gates as before.
            rb_ctx = {
                k: v for k, v in ctx.items() if k not in ("approval_id", "pending_approval_id")
            }
            # This step's own approval, not the run's last one: with two gated steps the
            # run-level id belongs to whichever was approved most recently, and
            # pre-authorizing a reverse with an unrelated approval is exactly the kind of
            # cross-wiring the platform's pre_authorized_by check exists to catch.
            authorizing = rec.approval_id or execution.approval_id
            if authorizing:
                rb_ctx["pre_authorized_by"] = authorizing
            extra |= {
                "runbook": execution.selected_runbook,
                "dry_run": False,
                "hitl_context": rb_ctx,
            }
        result = _call(cap, step, incident, dispatch=dispatch, **extra)
        rec.rollback = result.data if result.ok else {"error": result.error}
        rec.rolled_back = result.ok
        rec.rollback_status = "rolled_back" if result.ok else "rollback_failed"
        metrics.incr("rollback_attempted")
        if not result.ok:
            metrics.incr("rollback_failed")
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
            log.emit(
                AuditEventType.STEP_ROLLED_BACK,
                step_id=step.name,
                # The gate the REVERSE went through, not the forward step's.
                gate_type="required" if gated else "none",
                reason=f"reverted via {step.rollback_action!r}",
            )
        else:
            all_ok = False
            log.emit(
                AuditEventType.STEP_FAILED,
                step_id=step.name,
                gate_type="required" if gated else "none",
                reason=f"rollback failed: {result.error}",
            )
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
    The HITL happy path is covered by dedicated tests, not the eval harness.

    Two halves, both scored:

    - the **legacy execution** (``execute_runbook``), whose keys are unchanged so every
      pre-existing golden case still asserts exactly what it asserted before;
    - a **read-only discovery pass**, which adds the decision, the candidate list and
      the routing verdict. Nothing here executes twice: discovery only ranks.

    The discovery half reads the *full* input, so a golden case can supply
    ``environment`` / ``alert_name`` / ``failure_category`` / ``observed_signals`` and
    score real matching, while a legacy case that supplies only service + tags still
    behaves as it always did.
    """
    incident = Incident.model_validate(input)
    execution = execute_runbook(incident, hitl_context={"skip_approval": True})
    out = execution.model_dump(mode="json")
    # Flatten a few scalars so the suffix-grammar scorer can assert on them.
    out["steps_total"] = execution.steps_total
    out["steps_executed"] = execution.steps_executed
    out["destructive_steps"] = execution.destructive_steps

    ctx = IncidentContext.model_validate(
        {k: v for k, v in input.items() if k in IncidentContext.model_fields}
    )
    discovery = discover_candidates(ctx)
    status, next_action = DECISION_STATUS.get(
        discovery.decision, (ExecutorStatus.AMBIGUOUS, NextAction.RCA)
    )
    if discovery.decision is DiscoveryDecision.AUTO_SELECT:
        # An auto-selectable candidate is not an outcome on its own — the executor
        # would go on to dry-run it — so the eval records what discovery concluded
        # rather than pretending a run happened.
        status, next_action = ExecutorStatus.EXECUTED, NextAction.VERIFY
    applicable = discovery.applicable
    out["decision"] = discovery.decision.value
    out["decision_reason"] = discovery.reason
    out["candidate_count"] = len(discovery.candidates)
    out["applicable_count"] = len(applicable)
    out["auto_selected"] = discovery.auto_selected
    out["top_candidate"] = applicable[0].runbook_id if applicable else None
    # The whole applicable set, so a golden can assert "this runbook was offered"
    # without pinning *which* of several equally-scoring runbooks sorted first. When
    # two runbooks cover one fault with different blast radii, they tie on score and
    # specificity and the order falls to the id tie-break — asserting on that would
    # test alphabetical ordering rather than matching behaviour.
    out["applicable_candidates"] = [c.runbook_id for c in applicable]
    out["top_match_score"] = applicable[0].match_score if applicable else 0.0
    out["top_match_reasons"] = list(applicable[0].match_reasons) if applicable else []
    # Joined too: the eval grammar's list containment is exact-element, so a golden that
    # wants "did the reasons mention the failure category?" needs a string to scan.
    out["top_match_reasons_text"] = " | ".join(out["top_match_reasons"])
    out["top_risk_level"] = applicable[0].risk_level.value if applicable else None
    out["top_rollback_available"] = applicable[0].rollback_available if applicable else False
    out["blocked_count"] = sum(
        1 for c in discovery.candidates if c.applicability_status.value == "BLOCKED"
    )
    out["not_applicable_count"] = sum(
        1 for c in discovery.candidates if c.applicability_status.value == "NOT_APPLICABLE"
    )
    out["executor_status"] = status.value
    out["next_action"] = next_action.value
    return out


# ─── production entry points: discover → plan → execute ─────────────────────
#
# The three functions below are additive. ``execute_runbook`` / ``run_plan`` / ``run``
# above are untouched and remain the v0 contract; these wrap the same execution core
# with the validation, dry-run gate, durable state and idempotency a production caller
# needs. Nothing here re-implements step execution: ``execute_plan`` delegates to
# ``run_plan``, so there is exactly one place that dispatches a step.


def _as_context(incident: Incident | IncidentContext) -> IncidentContext:
    """Accept either the legacy hand-off or a full context."""
    return (
        incident
        if isinstance(incident, IncidentContext)
        else IncidentContext.from_incident(incident)
    )


def _lease_resource(runbook: ExecutableRunbook, ctx: IncidentContext) -> str:
    """The §25 lease key for this runbook: the namespace its steps act in, plus the
    service it remediates. Two runbooks for the same service therefore contend, which
    is the point — one remediation at a time per service."""
    namespace = next((s.namespace for s in runbook.steps if s.namespace), "default")
    return resource_key(namespace=namespace, service=runbook.service or ctx.service)


def _advance(
    execution_id: str,
    current: ExecutionState,
    target: ExecutionState,
    **fields: Any,
) -> ExecutionState:
    """Persist a state transition, refusing illegal ones (§19)."""
    assert_transition(current, target)
    repository.update_runbook_execution(execution_id, state=target.value, **fields)
    return target


_DISCOVERY_METRIC = {
    DiscoveryDecision.AUTO_SELECT: "discovery_auto_select",
    DiscoveryDecision.CANDIDATES: "discovery_candidates",
    DiscoveryDecision.NO_RUNBOOK: "discovery_no_runbook",
    DiscoveryDecision.AMBIGUOUS: "discovery_ambiguous",
    DiscoveryDecision.NOT_APPLICABLE: "discovery_not_applicable",
    DiscoveryDecision.BLOCKED: "discovery_blocked",
}


def discover_candidates(
    incident: Incident | IncidentContext,
    *,
    runbooks_dir: Any = None,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Rank every runbook that could handle this incident, and say who chooses (§3–§6).

    Read-only: no state is written, nothing is executed, and calling it twice is free.
    """
    ctx = _as_context(incident)
    result = matching.discover(load_runbooks(runbooks_dir), ctx, now=now)
    metrics.incr("discovery_total")
    metrics.incr(_DISCOVERY_METRIC[result.decision])
    logger.info(
        "runbook discovery for %s: %s (%d candidate(s))",
        ctx.service,
        result.decision.value,
        len(result.candidates),
    )
    return result


def _retryable_key(base_key: str) -> str:
    """The idempotency key for the next *attempt* at this plan.

    A plan whose execution was refused before it dispatched anything — a stale incident,
    a lease conflict, a re-validation failure — has produced a *refusal record*, not a
    run. Reusing its key forever would mean the plan could never be executed even after
    the blocker cleared: the terminal row would answer every future request with
    "already ran". So the base key is reused only while the existing execution can still
    be acted on; once it is terminal-and-never-dispatched, the next attempt is salted.

    A terminal execution that DID dispatch something keeps its key: that is the real
    duplicate-protection case, and it must never be retried automatically.
    """
    existing = repository.find_runbook_execution_by_key(base_key)
    if existing is None:
        return base_key
    record = ExecutionRecord.from_row(existing)
    if not record.is_terminal:
        return base_key  # still live: this request must collapse onto it
    dispatched = any(
        step.get("status") in ("executed", "failed", "rolled_back") for step in record.steps
    )
    if dispatched:
        return base_key  # a real run happened; never silently retry it
    attempt = repository.count_runbook_executions_for_plan(base_key) + 1
    return f"{base_key}#retry{attempt}"


def plan_execution(
    incident: Incident | IncidentContext,
    *,
    runbook_id: str | None = None,
    selected_by: str = "",
    runbooks_dir: Any = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    simulate_call: Any = None,
    now: datetime | None = None,
) -> PlanResult:
    """Select (or accept a selection), re-validate, and dry-run — the §7 flow.

    An operator's ``runbook_id`` chooses *what to evaluate*, never what to skip: the
    chosen runbook goes through the identical applicability, prerequisite, action and
    parameter checks an auto-selected one does, and a candidate that is not APPLICABLE
    is refused here with its blocking reasons. That is the whole of "human selection
    does not bypass safety".

    An execution is reserved only for a READY dry run, and reserving is idempotent: the
    same incident + runbook + version + plan hash always maps to the same
    ``execution_id`` (§20), so a re-planned, re-approved or retried request lands on the
    execution that already exists instead of creating a second one.
    """
    ctx = _as_context(incident)
    discovery = discover_candidates(ctx, runbooks_dir=runbooks_dir, now=now)
    plan = discovery_to_plan(discovery)

    if runbook_id:
        candidate = discovery.candidate(runbook_id)
        if candidate is None:
            metrics.incr("selection_rejected")
            plan.ui_state = UiState.BLOCKED
            plan.blocking_reasons = [
                f"runbook {runbook_id!r} is not a candidate for this incident — it is not in "
                f"the library, has no executable steps, or is for a different service"
            ]
            plan.reason = "operator selected a runbook that is not a candidate"
            return plan
        if not candidate.selectable:
            metrics.incr("selection_rejected")
            plan.ui_state = UiState.BLOCKED
            plan.selected_runbook_id = candidate.runbook_id
            plan.selected_runbook_version = candidate.version
            plan.blocking_reasons = list(candidate.blocking_reasons) or [
                f"runbook {runbook_id!r} is {candidate.applicability_status.value}"
            ]
            plan.reason = (
                f"operator selection refused: {candidate.runbook_id} is "
                f"{candidate.applicability_status.value}"
            )
            return plan
        chosen_id = candidate.runbook_id
        chooser = selected_by or "operator"
        metrics.incr("selection_manual")
    elif discovery.decision is DiscoveryDecision.AUTO_SELECT and discovery.auto_selected:
        chosen_id = discovery.auto_selected
        chooser = "auto"
        metrics.incr("selection_auto")
    else:
        # CANDIDATES / NO_RUNBOOK / AMBIGUOUS / BLOCKED / NOT_APPLICABLE — nothing to
        # plan until either a human picks or the incident goes to RCA.
        return plan

    runbook = get_runbook(chosen_id, runbooks_dir)
    if runbook is None:  # pragma: no cover - the candidate came from this library
        plan.ui_state = UiState.BLOCKED
        plan.blocking_reasons = [f"runbook {chosen_id!r} disappeared from the library"]
        return plan

    plan.selected_runbook_id = runbook.id
    plan.selected_runbook_version = runbook.version
    plan.selected_by = chooser

    report = dry_run(runbook, ctx, overrides=overrides, simulate_call=simulate_call, now=now)
    plan.dry_run = report
    plan.warnings = list(report.warnings)
    metrics.incr("dry_run_total")
    if not report.ready:
        metrics.incr("dry_run_blocked")
        plan.ui_state = UiState.DRY_RUN_BLOCKED
        plan.blocking_reasons = list(report.blocking_reasons)
        plan.reason = f"dry run BLOCKED for {runbook.ref}"
        return plan
    metrics.incr("dry_run_ready")

    chosen_candidate = discovery.candidate(runbook.id)
    base_key = idempotency_key(
        incident_id=ctx.incident_id,
        runbook_id=runbook.id,
        runbook_version=runbook.version,
        plan_hash=report.plan_hash,
    )
    key = _retryable_key(base_key)
    row, created = repository.claim_runbook_execution(
        execution_id=new_execution_id(),
        idempotency_key=key,
        incident_id=ctx.incident_id or None,
        runbook_id=runbook.id,
        runbook_version=runbook.version,
        plan_hash=report.plan_hash,
        service=runbook.service,
        environment=ctx.environment,
        state=ExecutionState.PLANNED.value,
        risk_level=report.risk_level.value,
        hitl_required=report.hitl_required,
        selected_by=chooser,
        selection_reason=plan.reason or discovery.reason,
        match_score=chosen_candidate.match_score if chosen_candidate else None,
        candidates=[c.model_dump(mode="json") for c in discovery.candidates],
        dry_run=report.model_dump(mode="json"),
        overrides=dict(overrides or {}),
    )
    record = ExecutionRecord.from_row(row)
    plan.execution_id = record.execution_id
    plan.execution_state = record.state
    plan.already_executed = record.is_terminal
    plan.ui_state = ui_state_for(state=record.state) if not created else UiState.DRY_RUN_READY
    if record.is_terminal:
        plan.reason = (
            f"execution {record.execution_id} already ran this exact plan "
            f"({record.state.value}) — it will not be run again"
        )
    return plan


def _blocked_result(
    plan: PlanResult,
    *,
    status: ExecutorStatus,
    next_action: NextAction,
    reason: str,
    blocking: list[str] | None = None,
    ui_state: UiState | None = None,
) -> ExecutorResult:
    """One shape for every "this is not going to run" answer."""
    return ExecutorResult(
        status=status,
        next_action=next_action,
        reason=reason,
        runbook_id=plan.selected_runbook_id,
        runbook_version=plan.selected_runbook_version,
        execution_id=plan.execution_id,
        execution_state=plan.execution_state,
        ui_state=ui_state or plan.ui_state,
        risk_level=plan.dry_run.risk_level.value if plan.dry_run else None,
        hitl_required=plan.dry_run.hitl_required if plan.dry_run else False,
        candidates=plan.candidates,
        blocking_reasons=blocking if blocking is not None else list(plan.blocking_reasons),
        warnings=list(plan.warnings),
        dry_run=plan.dry_run,
    )


# Legacy resolution status -> (durable state, contract status, next action).
_OUTCOME: dict[str, tuple[ExecutionState, ExecutorStatus, NextAction]] = {
    "resolved": (ExecutionState.COMPLETED, ExecutorStatus.EXECUTED, NextAction.VERIFY),
    "denied": (ExecutionState.ABORTED, ExecutorStatus.BLOCKED, NextAction.RCA),
    "rolled_back": (ExecutionState.ROLLED_BACK, ExecutorStatus.ROLLED_BACK, NextAction.RCA),
    "failed": (ExecutionState.FAILED, ExecutorStatus.FAILED, NextAction.RCA),
    "no_runbook": (ExecutionState.ABORTED, ExecutorStatus.NO_RUNBOOK, NextAction.RCA),
}


def execute_plan(
    plan: PlanResult,
    incident: Incident | IncidentContext,
    *,
    hitl_context: dict[str, Any] | None = None,
    runbooks_dir: Any = None,
    dispatch: Dispatch | None = None,
    simulate_call: Any = None,
    now: datetime | None = None,
) -> ExecutorResult:
    """Execute an authorized plan exactly once, and hand off to verification (§18–§29).

    Refuses, in order: a plan with no READY dry run; an execution that already reached a
    terminal state or is in flight (returns *its* state — §20); a plan whose re-checked
    applicability no longer holds, which is where a resolved/aged-out incident is caught
    (§24); and a service that another execution is currently remediating (§25).

    The step loop itself is ``run_plan`` — the same code the v0 path runs, so the HITL
    gate, the rollback ordering and the audit events are identical. This function owns
    the state machine around it, not the mechanics inside it.

    On success the result carries a :class:`VerificationHandoff` and
    ``next_action=VERIFY``. It never reports the incident resolved: that is the
    Resolution Verifier's verdict, and ``ExecutorStatus`` has no value for it.
    """
    metrics.incr("execution_requested")
    ctx = _as_context(incident)

    if plan.dry_run is None or not plan.dry_run.ready or not plan.execution_id:
        status, next_action = DECISION_STATUS.get(
            plan.decision, (ExecutorStatus.BLOCKED, NextAction.RCA)
        )
        if plan.dry_run is not None and not plan.dry_run.ready:
            status, next_action = ExecutorStatus.BLOCKED, NextAction.RCA
            metrics.incr("execution_policy_blocked")
        return _blocked_result(
            plan,
            status=status,
            next_action=next_action,
            reason=plan.reason or "no authorized plan to execute",
        )

    row = repository.get_runbook_execution(plan.execution_id)
    if row is None:  # pragma: no cover - the row was just claimed
        return _blocked_result(
            plan,
            status=ExecutorStatus.BLOCKED,
            next_action=NextAction.ESCALATE,
            reason=f"execution {plan.execution_id} is not on record",
        )
    record = ExecutionRecord.from_row(row)
    if record.state not in (ExecutionState.PLANNED, ExecutionState.APPROVED):
        # Terminal, or someone else is already running it. Either way this request must
        # not start production actions — report what the existing execution is doing.
        # (The authoritative check is the compare-and-set below; this is the cheap,
        # early answer that avoids re-validating a plan that is already settled.)
        metrics.incr("execution_duplicate")
        existing = from_record(record)
        existing.candidates = plan.candidates
        existing.dry_run = plan.dry_run
        return existing

    runbook = get_runbook(record.runbook_id, runbooks_dir)
    if runbook is None:  # pragma: no cover - defensive
        return _blocked_result(
            plan,
            status=ExecutorStatus.BLOCKED,
            next_action=NextAction.ESCALATE,
            reason=f"runbook {record.runbook_id!r} is no longer in the library",
        )

    # Re-validate at execution time, not just at plan time. Between the two an incident
    # can close, age out, or have its alert stop firing — §24's stale-incident guard.
    #
    # The overrides the plan was authorized with come from the ROW, not the caller: they
    # are part of what was approved, and rebuilding the plan without them would produce a
    # different plan_hash and abort every overridden plan on a mismatch it caused itself.
    authorized_overrides = {
        str(step): dict(params or {}) for step, params in (record.overrides or {}).items()
    }
    recheck = dry_run(
        runbook,
        ctx,
        overrides=authorized_overrides or None,
        simulate_call=simulate_call,
        now=now,
    )
    if not recheck.ready or recheck.plan_hash != record.plan_hash:
        stale = any(
            p.id == "incident_active" and p.status.value == "failed"
            for p in (recheck.applicability.prerequisites if recheck.applicability else [])
        )
        metrics.incr("execution_stale_blocked" if stale else "execution_policy_blocked")
        blocking = list(recheck.blocking_reasons) or [
            "the plan changed since it was authorized (plan hash mismatch) — re-plan it"
        ]
        _advance(
            record.execution_id,
            record.state,
            ExecutionState.ABORTED,
            reason="; ".join(blocking)[:2000],
            status=ExecutorStatus.BLOCKED.value,
            next_action=NextAction.RCA.value,
            completed_at=utcnow(),
        )
        plan.execution_state = ExecutionState.ABORTED
        return _blocked_result(
            plan,
            status=ExecutorStatus.BLOCKED,
            next_action=NextAction.RCA,
            reason="re-validation before execution refused this plan",
            blocking=blocking,
            ui_state=UiState.BLOCKED,
        )

    lease_key = _lease_resource(runbook, ctx)
    acquired, holder = repository.acquire_runbook_lease(
        resource_key=lease_key,
        execution_id=record.execution_id,
        ttl_seconds=lease_seconds(),
        incident_id=record.incident_id or None,
        runbook_id=runbook.id,
    )
    if not acquired:
        metrics.incr("execution_lease_conflict")
        other = (holder or {}).get("execution_id", "another execution")
        # Record the refusal on the row, not just in the return value. The async execute
        # route has already answered "accepted" and pointed the client at
        # GET /executions/{id}; if nothing is written there, that row sits at PLANNED
        # forever and the operator watches a spinner for a run that was refused.
        repository.update_runbook_execution(
            record.execution_id,
            state=ExecutionState.ABORTED.value,
            status=ExecutorStatus.BLOCKED.value,
            next_action=NextAction.ESCALATE.value,
            reason=f"{lease_key} is already being remediated by {other}",
            completed_at=utcnow(),
        )
        plan.execution_state = ExecutionState.ABORTED
        return _blocked_result(
            plan,
            status=ExecutorStatus.BLOCKED,
            next_action=NextAction.ESCALATE,
            reason=f"{lease_key} is already being remediated by {other}",
            blocking=[
                f"a conflicting execution ({other}) holds the lease on {lease_key}; "
                "concurrent remediation of one service is refused"
            ],
            ui_state=UiState.BLOCKED,
        )

    ctx_hitl = dict(hitl_context or {})
    # ─── the one place a run is allowed to start ────────────────────────────
    #
    # Compare-and-set, not the read-then-check above. Two threads can both read
    # state='planned' (a double-clicked Execute button re-plans to the same execution and
    # dispatches on a pool), pass that guard, and both dispatch the production steps —
    # restarting a deployment twice mid-incident, with one run's steps and audit events
    # overwritten by the other's terminal write. The unique idempotency key does not help:
    # it guarantees one row, not one run. Only the database can arbitrate, so the state
    # move carries its own expectation and the loser is told what the winner is doing.
    first_state = (
        ExecutionState.WAITING_APPROVAL if recheck.hitl_required else ExecutionState.EXECUTING
    )
    assert_transition(record.state, first_state)
    claimed = repository.claim_runbook_execution_state(
        record.execution_id,
        expected_states=(ExecutionState.PLANNED.value, ExecutionState.APPROVED.value),
        new_state=first_state.value,
        approval_id=ctx_hitl.get("approval_id") if recheck.hitl_required else None,
        started_at=utcnow(),
    )
    if claimed is None:
        # Someone else won the CAS. Do NOT release the lease here: both callers share
        # ``record.execution_id`` (they collapsed onto the same row), so
        # ``acquire_runbook_lease``'s re-entrant same-owner rule let this caller "acquire"
        # it too — releasing now would drop the lease out from under the winner, who is
        # still executing and relying on it. Only the winner's own ``finally`` releases it.
        metrics.incr("execution_duplicate")
        current = repository.get_runbook_execution(record.execution_id)
        existing = from_record(ExecutionRecord.from_row(current) if current else record)
        existing.candidates = plan.candidates
        existing.dry_run = plan.dry_run
        existing.reason = (
            f"another request is already executing {record.execution_id}; "
            "this one did not start production actions"
        )
        return existing

    # Renew the lease while the run holds it. A gated run blocks for the whole approval
    # window (up to 900 s via the API) plus execution time, so a lease taken once at the
    # start expires mid-run — after which another execution steals it and remediates the
    # same service concurrently, which is the one thing the lease exists to prevent.
    # ``acquire_runbook_lease`` is already re-entrant for the same execution_id and simply
    # pushes ``expires_at`` forward.
    lease_stop = threading.Event()

    def _renew_lease() -> None:
        interval = max(1.0, lease_seconds() / 3)
        while not lease_stop.wait(interval):
            with contextlib.suppress(Exception):
                repository.acquire_runbook_lease(
                    resource_key=lease_key,
                    execution_id=record.execution_id,
                    ttl_seconds=lease_seconds(),
                    incident_id=record.incident_id or None,
                    runbook_id=runbook.id,
                )

    lease_keeper = threading.Thread(
        target=_renew_lease, name=f"runbook-lease-{record.execution_id}", daemon=True
    )
    lease_keeper.start()

    state: ExecutionState = first_state
    try:
        if recheck.hitl_required:
            metrics.incr("hitl_required")
        metrics.incr("execution_started")

        started = time.perf_counter()
        execution = run_plan(
            ctx.to_incident(),
            runbook,
            hitl_context=ctx_hitl,
            dispatch=dispatch or guarded_dispatch,
            overrides=authorized_overrides or None,
        )
        metrics.observe("execution_duration", (time.perf_counter() - started) * 1000)

        # The approval outcome is known only now: run_plan blocks inside the registry
        # while the gate waits for a human, so these two transitions are recorded when
        # it returns. The audit events on the execution carry the real timestamps.
        #
        # "Not denied" is NOT the same as "approved": a plan can fail or roll back on an
        # earlier autonomous step and never reach the gate at all. Recording an approval
        # then would put a human's name on a decision nobody made, and inflate the HITL
        # approval rate with approvals that never happened. So the bookkeeping keys off
        # whether a gated step actually went through the gate.
        gate_reached = any(
            event.status is AuditEventType.HITL_APPROVED for event in execution.audit_events
        )
        if state is ExecutionState.WAITING_APPROVAL:
            if execution.status == "denied":
                metrics.incr("hitl_rejected")
            elif not gate_reached:
                # The run ended before any gated step — leave the row in
                # WAITING_APPROVAL's successor states alone and let the terminal
                # transition below record what actually happened.
                logger.info(
                    "execution %s ended before reaching the gate (%s); no approval recorded",
                    record.execution_id,
                    execution.status,
                )
            else:
                metrics.incr("hitl_approved")
                state = _advance(
                    record.execution_id,
                    state,
                    ExecutionState.APPROVED,
                    approval_id=execution.approval_id or ctx_hitl.get("approval_id"),
                    approver=str(ctx_hitl.get("approver") or "") or None,
                )
                state = _advance(record.execution_id, state, ExecutionState.EXECUTING)

        target, status, next_action = _OUTCOME.get(
            execution.status, (ExecutionState.FAILED, ExecutorStatus.FAILED, NextAction.RCA)
        )
        if state is ExecutionState.WAITING_APPROVAL and target is not ExecutionState.ABORTED:
            # Still WAITING_APPROVAL means no gated step ran: either the gate refused, or
            # the run ended earlier (a failed autonomous step). Either way nothing was
            # approved, and ABORTED is the only honest terminal state from here — the
            # reason field carries which of the two it was.
            target = ExecutionState.ABORTED
            status, next_action = ExecutorStatus.BLOCKED, NextAction.RCA
            if execution.status in ("failed", "rolled_back"):
                status, next_action = (
                    ExecutorStatus.FAILED
                    if execution.status == "failed"
                    else ExecutorStatus.ROLLED_BACK,
                    NextAction.RCA,
                )
        if target is ExecutionState.ROLLED_BACK:
            # ROLLING_BACK is a real state the run passed through; record it so the
            # history shows a rollback happened rather than a silent state jump.
            state = _advance(record.execution_id, state, ExecutionState.ROLLING_BACK)

        rollback_status = "not_required"
        if any(s.rollback_status == "rollback_failed" for s in execution.steps):
            rollback_status = "rollback_failed"
        elif any(s.rollback_status == "rolled_back" for s in execution.steps):
            rollback_status = "rolled_back"

        metrics.incr(
            {
                ExecutorStatus.EXECUTED: "execution_completed",
                ExecutorStatus.FAILED: "execution_failed",
                ExecutorStatus.ROLLED_BACK: "execution_rolled_back",
                ExecutorStatus.BLOCKED: "execution_blocked",
            }.get(status, "execution_blocked")
        )

        steps_json = [s.model_dump(mode="json") for s in execution.steps]
        _advance(
            record.execution_id,
            state,
            target,
            status=status.value,
            next_action=next_action.value,
            reason=execution.reason[:2000],
            steps=steps_json,
            audit_events=[e.model_dump(mode="json") for e in execution.audit_events],
            rollback_status=rollback_status,
            approval_id=execution.approval_id or ctx_hitl.get("approval_id"),
            completed_at=utcnow(),
        )
    except Exception as exc:
        # An unexpected exception here (a provider that raises past the registry's own
        # guard, a persistence failure mid-run) must not leave the execution parked in a
        # non-terminal state: the idempotency key would then refuse every future attempt
        # for this plan with "already executing", and no human could tell whether any
        # step had run. Record it as FAILED with the reason, escalate, and re-raise so
        # the caller still sees the error rather than a silent BLOCKED.
        logger.exception("runbook execution %s crashed", record.execution_id)
        metrics.incr("execution_failed")
        with contextlib.suppress(Exception):
            repository.update_runbook_execution(
                record.execution_id,
                state=ExecutionState.FAILED.value,
                status=ExecutorStatus.FAILED.value,
                next_action=NextAction.ESCALATE.value,
                reason=(
                    f"executor raised {type(exc).__name__}: {exc} — whether any step ran is "
                    "unknown; inspect the audit log before retrying"
                )[:2000],
                error=f"{type(exc).__name__}: {exc}",
                completed_at=utcnow(),
            )
        raise
    finally:
        lease_stop.set()
        repository.release_runbook_lease(resource_key=lease_key, execution_id=record.execution_id)

    handoff: VerificationHandoff | None = None
    if status is ExecutorStatus.EXECUTED:
        handoff = VerificationHandoff(
            execution_id=record.execution_id,
            incident_id=record.incident_id,
            service=runbook.service,
            runbook_id=runbook.id,
            runbook_version=runbook.version,
            status=target.value,
            steps=steps_json,
            actions_executed=[
                {
                    "step_id": s.step_id or s.name,
                    "action_id": s.action_id or s.action,
                    "target": s.target,
                    "namespace": s.namespace,
                    "parameters": redact(s.parameters),
                    "destructive": s.destructive,
                    "status": s.status,
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                }
                for s in execution.steps
                if s.status in ("executed", "rolled_back")
            ],
            rollback_status=rollback_status,
            completed_at=_utc_iso(),
            audit_metadata={
                "selected_by": record.selected_by,
                "selection_reason": record.selection_reason,
                "match_score": record.match_score,
                "risk_level": recheck.risk_level.value,
                "hitl_required": recheck.hitl_required,
                "approval_id": execution.approval_id,
                "plan_hash": record.plan_hash,
                "event_count": len(execution.audit_events),
            },
        )

    return ExecutorResult(
        status=status,
        next_action=next_action,
        reason=execution.reason,
        runbook_id=runbook.id,
        runbook_version=runbook.version,
        execution_id=record.execution_id,
        execution_state=target,
        ui_state=ui_state_for(state=target),
        risk_level=recheck.risk_level.value,
        hitl_required=recheck.hitl_required,
        approval_id=execution.approval_id,
        steps=steps_json,
        candidates=plan.candidates,
        warnings=list(plan.warnings),
        rollback_status=rollback_status,
        dry_run=plan.dry_run,
        verification_handoff=handoff,
        legacy=execution,
    )


def execute(
    incident: Incident | IncidentContext,
    *,
    runbook_id: str | None = None,
    selected_by: str = "",
    hitl_context: dict[str, Any] | None = None,
    runbooks_dir: Any = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    dispatch: Dispatch | None = None,
    simulate_call: Any = None,
    now: datetime | None = None,
) -> ExecutorResult:
    """Plan and execute in one call — the orchestrator-facing entry point.

    With no ``runbook_id`` this runs only when exactly one candidate is applicable
    (§6 CASE 1). Several applicable candidates return ``AMBIGUOUS`` with the ranked
    list rather than guessing, and the SRE's choice comes back as ``runbook_id``.
    """
    ctx = _as_context(incident)
    plan = plan_execution(
        ctx,
        runbook_id=runbook_id,
        selected_by=selected_by,
        runbooks_dir=runbooks_dir,
        overrides=overrides,
        simulate_call=simulate_call,
        now=now,
    )
    if not plan.ready:
        status, next_action = DECISION_STATUS.get(
            plan.decision, (ExecutorStatus.BLOCKED, NextAction.RCA)
        )
        if plan.decision is DiscoveryDecision.CANDIDATES and not runbook_id:
            reason = plan.reason or "several applicable runbooks — an SRE must choose"
        elif plan.blocking_reasons:
            status, next_action = ExecutorStatus.BLOCKED, NextAction.RCA
            reason = plan.reason or "planning refused this runbook"
        else:
            reason = plan.reason
        if plan.already_executed and plan.execution_id:
            row = repository.get_runbook_execution(plan.execution_id)
            if row is not None:
                metrics.incr("execution_duplicate")
                return from_record(ExecutionRecord.from_row(row))
        return _blocked_result(plan, status=status, next_action=next_action, reason=reason)
    return execute_plan(
        plan,
        ctx,
        hitl_context=hitl_context,
        runbooks_dir=runbooks_dir,
        dispatch=dispatch,
        simulate_call=simulate_call,
        now=now,
    )
