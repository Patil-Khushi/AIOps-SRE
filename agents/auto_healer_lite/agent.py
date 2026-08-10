"""Auto-Healer-lite — two coexisting surfaces.

**Legacy HITL-1 narrow path** (``recommend_restart`` /
``RestartRecommendation``) — built to exercise the platform HITL gate
end-to-end via the ``automation.runbook.execute`` capability. Tests +
CLI runner depend on this shape; kept untouched.

**PRS-002 generic v1 path** (``execute`` / ``ExecutionRequest`` /
``ExecutionVerdict``) — receives a single chosen ``RemediationOption``
from PRS-001 and produces a structured ``ExecutionVerdict``. The agent
calls the platform HITL gate (``auto_heal.lite.execute``, REQUIRED).
When the gate clears AND the caller passes ``dry_run=False``, the
agent dispatches the option's ``tool_capability`` via the platform
tool registry and maps the ``ToolResult`` to ``EXECUTED`` or
``EXECUTION_FAILED``. When ``dry_run=True`` (the safer default), the
agent stops at ``DRY_RUN_OK`` and records what *would* have run via
``would_execute=True``. Every attempt — REFUSED, BLOCKED, DRY_RUN_OK,
EXECUTED, EXECUTION_FAILED — is persisted to ``aiops.state.ExecutionRow``
so the dashboard history + the future historical-effectiveness feed
to PRS-001 share one source of truth.

Legacy path::

    recommend_restart(rec) -> registry.call("automation.runbook.execute", ...)
                          -> gate.check  (level=REQUIRED)
                          -> approver  (= ApprovalRequester)
                              -> registry.create  + chatops "approval requested"
                              -> wait until approve / deny / expire
                          -> if approved: tool runs, returns ToolResult
                             if denied/expired: ToolResult(ok=False, ...)
    <- RestartOutcome (executed / denied / expired / blocked / error)

PRS-002 v1 path::

    execute(req) -> validate option (requires_hitl=True, tool_capability set, ...)
                 -> get_gate().enforce("auto_heal.lite.execute", ctx)
                 -> on GateError:      -> PENDING_APPROVAL / BLOCKED
                                          (dispatch physically unreachable)
                    if allowed + dry:  -> DRY_RUN_OK (no tool call)
                    if allowed + real: -> registry.call(tool_capability, **tool_args)
                                          -> EXECUTED / EXECUTION_FAILED
                 -> save_execution(verdict)  (best-effort; failures logged)
    <- ExecutionVerdict

Both paths share ``run(input: dict) -> dict`` for the eval harness.
``run`` dispatches on input shape: an ``option`` key routes to
``execute``; otherwise it stays on the legacy restart path.

Deferred to a follow-up:
- Pre-flight blast-radius re-validation against current CMDB / topology.
- Rollback rehearsal (confirm the rollback string maps to a registered
  reverse capability before forward fire).
- Caller-supplied ``approval_id`` for deterministic test seeding.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

# Side-effect import: registers the mock automation.runbook.execute provider.
import aiops.tools.mock_providers  # noqa: F401
from agents.auto_healer_lite.models import (
    AuditMetadata,
    ExecutionRequest,
    ExecutionStatus,
    ExecutionVerdict,
    GateDecisionSummary,
    RestartOutcome,
    RestartRecommendation,
)
from aiops.policy import GateError, get_gate
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# Gate action this agent enforces on. Declared REQUIRED in
# ``aiops/policy/gate.py:DEFAULT_LEVELS`` — the catalog rule that every
# Auto-Healer Lite execution is human-gated.
_PRS002_GATE_ACTION = "auto_heal.lite.execute"


def recommend_restart(
    rec: RestartRecommendation,
    *,
    hitl_context: dict[str, Any] | None = None,
) -> RestartOutcome:
    """Ask the platform to restart a deployment, gated on a Required HITL approval.

    ``hitl_context`` is forwarded to ``ToolRegistry.call`` so callers can:

    * pre-supply an ``approval_id`` (deterministic for tests)
    * shorten the per-call ``approval_timeout_seconds`` (tests + demo)
    * set ``skip_approval=True`` to bypass HITL in the eval harness
    """
    ctx: dict[str, Any] = {
        "deployment": rec.deployment,
        "namespace": rec.namespace,
        "reason": rec.reason,
        "runbook": rec.runbook,
        "dry_run": rec.dry_run,
    }
    if hitl_context:
        ctx.update(hitl_context)

    result = get_registry().call(
        "automation.runbook.execute",
        hitl_context=ctx,
        runbook=rec.runbook,
        target=f"deployment/{rec.deployment}",
        namespace=rec.namespace,
        dry_run=rec.dry_run,
    )

    approval_id = ctx.get("pending_approval_id")

    if result.ok:
        return RestartOutcome(
            recommendation=rec,
            status="executed",
            approval_id=approval_id,
            approver=ctx.get("approver"),
            result=result.data if isinstance(result.data, dict) else {"value": result.data},
        )

    # Distinguish gate-block from tool-failure so the demo narrative is clear.
    blocked = (result.metadata or {}).get("blocked_by") == "hitl_gate"
    status: str = "error"
    if blocked:
        # The approval registry decides the precise outcome; look it up so
        # the response carries "denied" vs "expired" instead of a generic
        # "blocked".  Failing the lookup (no registry, unknown id) falls
        # back to the original "blocked" status — fine for tests.
        status = "blocked"
        if approval_id:
            try:
                from aiops.policy import ApprovalStatus, get_approval_registry

                req = get_approval_registry().get(approval_id)
                if req.status is ApprovalStatus.DENIED:
                    status = "denied"
                elif req.status is ApprovalStatus.EXPIRED:
                    status = "expired"
            except Exception:
                logger.debug("approval status lookup failed for %r", approval_id, exc_info=True)

    return RestartOutcome(
        recommendation=rec,
        status=status,  # type: ignore[arg-type]
        approval_id=approval_id,
        approver=ctx.get("approver"),
        error=result.error,
    )


def reset_state() -> None:
    """Eval-harness hook (A11). Auto-Healer-lite is stateless — every call
    consults the live registry — but the harness expects the symbol."""
    return None


# ─── PRS-002 generic surface — Day-1 stub ──────────────────────────────────


def _validate_option(option: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)``.

    The agent refuses to execute an option that doesn't carry the
    invariants PRS-001 promises:

    - ``requires_hitl=True`` must be present and truthy. The agent does
      NOT override the requester's autonomy declaration (catalog #3:
      HITL is platform-enforced, not agent-enforced).
    - ``tool_capability`` must be a non-empty string OR ``action_type``
      must be ``"manual"``. A non-manual option without a tool capability
      is a malformed contract — the agent has no way to act on it.
    - ``option_id`` must be present so the audit trail can cross-
      reference back to PRS-001's verdict.
    """
    if not isinstance(option, dict):
        return False, f"option must be a dict, got {type(option).__name__}"
    if not option.get("requires_hitl"):
        return (
            False,
            "option.requires_hitl must be True — agent refuses to override the autonomy declaration",
        )
    if not option.get("option_id"):
        return False, "option.option_id is required for audit cross-reference"
    action_type = str(option.get("action_type", "manual")).lower()
    tool_capability = option.get("tool_capability")
    if action_type != "manual" and not tool_capability:
        return False, (
            f"option.action_type={action_type!r} but no tool_capability — non-manual "
            "actions must declare which platform tool would execute them"
        )
    return True, "ok"


def _decision_to_summary(decision: Any, ctx: dict[str, Any]) -> GateDecisionSummary:
    """Pull the fields we render off the dataclass returned by ``gate.check``.

    ``ctx['pending_approval_id']`` is written by the ApprovalRequester
    during the gate flow — that's the only legitimate write-back into
    the caller's context dict (HITL-5, see ``aiops/policy/gate.py``).
    We surface it on the verdict so the dashboard can link to the
    approval request.
    """
    approval = getattr(decision, "approval", None)
    return GateDecisionSummary(
        allowed=bool(getattr(decision, "allowed", False)),
        level=str(getattr(getattr(decision, "level", None), "value", "unknown")),
        reason=str(getattr(decision, "reason", "")),
        approver=getattr(decision, "approver", None),
        approval_id=ctx.get("pending_approval_id"),
        approval_status=getattr(approval, "status", None) if approval is not None else None,
    )


def execute(request: ExecutionRequest) -> ExecutionVerdict:
    """Validate the option, consult the platform HITL gate, map the
    Decision to an ExecutionStatus, optionally dispatch the tool, and
    return the structured verdict. Persists the verdict to
    ``aiops.state.ExecutionRow`` regardless of outcome.

    Behaviour by branch:

    - Option fails validation → ``REFUSED``, gate not consulted, no tool call.
    - Gate refuses (approver missing / denied / expired) → ``BLOCKED``
      (or ``PENDING_APPROVAL`` when an async approval is in flight).
    - Gate clears AND ``request.dry_run=True`` → ``DRY_RUN_OK``,
      ``would_execute=True`` for non-manual actions, no tool call.
    - Gate clears AND ``request.dry_run=False`` → ``execute the tool``,
      map ``ToolResult`` to ``EXECUTED`` (ok=True) or
      ``EXECUTION_FAILED`` (ok=False or capability not registered).
    """
    request_id = f"ahl-{uuid.uuid4().hex[:12]}"
    trace: list[str] = [
        f"request_id={request_id} service={request.affected_service!r} dry_run={request.dry_run}"
    ]

    option = request.option
    option_id = str(option.get("option_id") or "unknown")
    tool_capability = option.get("tool_capability")
    tool_args = option.get("tool_args") or {}

    # 1. Validate the option's shape.
    ok, reason = _validate_option(option)
    if not ok:
        trace.append(f"refused: {reason}")
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=ExecutionStatus.REFUSED,
                dry_run=request.dry_run,
                decision=GateDecisionSummary(allowed=False, level="n/a", reason=reason),
                tool_capability=tool_capability,
                tool_args=tool_args,
                rationale=f"Refused before reaching the HITL gate: {reason}",
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )
    trace.append(
        f"validated: option_id={option_id} action_type={option.get('action_type')} "
        f"blast={option.get('blast_radius')}"
    )

    # 2. Platform HITL gate — ENFORCE, don't just check.
    #
    # Principle #3: a buggy or compromised agent must not be able to reach the
    # real tool dispatch (step 3c) without the platform gate. ``enforce()``
    # raises ``GateError`` when the action isn't allowed, so the dispatch below
    # is physically unreachable on a refusal — unlike a bare ``check()`` whose
    # boolean an agent edit could ignore. We still render the structured
    # outcome on the verdict: ``enforce()`` returns the Decision on the allowed
    # path and attaches it to ``GateError.decision`` on the blocked path, so we
    # report BLOCKED / PENDING_APPROVAL without a second approver round-trip
    # (a re-``check()`` would double-prompt a human on a REQUIRED action).
    ctx: dict[str, Any] = dict(request.hitl_context or {})
    ctx.setdefault("option_id", option_id)
    ctx.setdefault("incident_id", request.incident_id)
    ctx.setdefault("affected_service", request.affected_service)
    ctx.setdefault("blast_radius", option.get("blast_radius"))
    ctx.setdefault("rollback", option.get("rollback"))
    ctx.setdefault("operator", request.operator)
    # We deliberately DO NOT default ``skip_approval`` here. The gate's
    # default approver (``_no_approver``) already fail-closes REQUIRED
    # actions to BLOCKED when no real approver is installed — so the
    # eval harness + smoke runs that haven't wired ``ApprovalRequester``
    # still get a deterministic BLOCKED outcome without needing the
    # short-circuit. In production (with ``ApprovalRequester`` installed)
    # the full approval round-trip runs unless the caller explicitly
    # opts out via ``hitl_context={"skip_approval": True}``. An earlier
    # iteration defaulted to True here and produced a silent BLOCKED on
    # every production execute call regardless of dry_run — caught in
    # self-review of PR #170.

    try:
        decision = get_gate().enforce(_PRS002_GATE_ACTION, ctx)
    except GateError as exc:
        # 3a. Gate refused → terminal (no tool dispatch). The dispatch in 3c is
        # physically unreachable from here. Rebuild the verdict from the
        # Decision the gate attached to the error (no second approver call).
        summary = _decision_to_summary(exc.decision, ctx)
        if summary.approval_id and summary.approval_status not in {"denied", "expired"}:
            status = ExecutionStatus.PENDING_APPROVAL
            rationale = f"Gate flow opened approval {summary.approval_id!r}; waiting on a human."
        else:
            status = ExecutionStatus.BLOCKED
            rationale = f"Gate refused the action: {summary.reason}"
        trace.append(
            f"gate.enforce({_PRS002_GATE_ACTION!r}) blocked: "
            f"level={summary.level} reason={summary.reason!r} -> status={status.value}"
        )
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=status,
                dry_run=request.dry_run,
                decision=summary,
                tool_capability=tool_capability,
                tool_args=tool_args,
                rationale=rationale,
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )

    # Gate cleared — the action is allowed; dispatch in 3c is now reachable.
    summary = _decision_to_summary(decision, ctx)
    trace.append(
        f"gate.enforce({_PRS002_GATE_ACTION!r}): allowed={summary.allowed} "
        f"level={summary.level} reason={summary.reason!r}"
    )

    # 3b. Gate cleared. Branch on dry_run.
    if request.dry_run:
        status = ExecutionStatus.DRY_RUN_OK
        rationale = (
            f"Gate cleared (level={summary.level}); request.dry_run=True so the tool was "
            f"not called. WOULD have dispatched aiops.tools.get_registry().call("
            f"{tool_capability!r}, **{tool_args!r})."
        )
        would_execute = bool(tool_capability)
        trace.append(f"status={status.value} (dry_run)")
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=status,
                dry_run=request.dry_run,
                decision=summary,
                tool_capability=tool_capability,
                tool_args=tool_args,
                would_execute=would_execute,
                rationale=rationale,
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )

    # 3c. Gate cleared AND dry_run=False → real tool dispatch.
    # Manual actions have no executor wired in v0 — surface that as a
    # successful dry_run_ok-style outcome (would_execute=False) instead
    # of an execution failure. v1+ can add manual-execution receipts
    # (operator marks "I did this manually") at the dashboard layer.
    if not tool_capability:
        status = ExecutionStatus.DRY_RUN_OK
        rationale = (
            f"Gate cleared (level={summary.level}). Option has no tool_capability "
            f"(action_type={option.get('action_type')!r}, manual) — no automated "
            "executor to call. Recording as DRY_RUN_OK; the operator carries it out."
        )
        trace.append(f"status={status.value} (manual, no executor)")
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=status,
                dry_run=request.dry_run,
                decision=summary,
                tool_capability=None,
                tool_args=tool_args,
                would_execute=False,
                rationale=rationale,
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )

    def _unregistered_capability_verdict() -> ExecutionVerdict:
        """Verdict for "the gate cleared but nothing can run this".

        Reachable two ways, which must stay indistinguishable to callers:
        a direct ``by_capability`` KeyError, and — since the registry started
        reporting a missing provider as a structured result rather than an
        exception — ``ToolResult(ok=False, metadata={"missing_provider": True})``.
        Note ``tool_result`` stays unset: no tool ran, so there is no result to
        report, and callers distinguish this case by ``tool_result is None``.
        """
        trace.append(f"tool_capability={tool_capability!r} NOT registered")
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=ExecutionStatus.EXECUTION_FAILED,
                dry_run=request.dry_run,
                decision=summary,
                tool_capability=tool_capability,
                tool_args=tool_args,
                error=f"tool capability {tool_capability!r} is not registered with the platform tool registry",
                rationale=(
                    "Gate cleared but the executor was unreachable — no platform tool "
                    f"matches capability {tool_capability!r}."
                ),
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )

    try:
        result = get_registry().call(tool_capability, **tool_args)
    except KeyError:
        # Retained for callers holding a registry that still raises (the test
        # fakes do), even though the real registry now returns a result instead.
        return _unregistered_capability_verdict()
    except Exception as exc:  # boundary: a buggy tool can't crash the verdict
        trace.append(f"tool dispatch raised: {type(exc).__name__}: {exc}")
        logger.exception("auto_heal.lite.execute: tool dispatch raised")
        return _finalise(
            ExecutionVerdict(
                request_id=request_id,
                option_id=option_id,
                affected_service=request.affected_service,
                status=ExecutionStatus.EXECUTION_FAILED,
                dry_run=request.dry_run,
                decision=summary,
                tool_capability=tool_capability,
                tool_args=tool_args,
                error=f"{type(exc).__name__}: {exc}",
                rationale=f"Tool dispatch raised an exception: {exc}",
                audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
            )
        )

    if (getattr(result, "metadata", None) or {}).get("missing_provider"):
        return _unregistered_capability_verdict()

    # ToolResult shape: {ok: bool, data: any, error: str | None, metadata: ...}
    tool_result_dict: dict[str, Any] = {
        "ok": bool(getattr(result, "ok", False)),
        "data": getattr(result, "data", None),
        "error": getattr(result, "error", None),
        "metadata": dict(getattr(result, "metadata", {}) or {}),
    }
    trace.append(
        f"tool.{tool_capability} -> ok={tool_result_dict['ok']} error={tool_result_dict['error']!r}"
    )

    if tool_result_dict["ok"]:
        status = ExecutionStatus.EXECUTED
        rationale = (
            f"Gate cleared and tool {tool_capability!r} succeeded. Forward action "
            f"completed; the rollback path remains {option.get('rollback')!r}."
        )
        error: str | None = None
    else:
        status = ExecutionStatus.EXECUTION_FAILED
        rationale = (
            f"Gate cleared but tool {tool_capability!r} returned ok=False. "
            f"Error: {tool_result_dict['error']!r}. No rollback was attempted; "
            "the operator should follow the option's rollback plan manually."
        )
        error = str(tool_result_dict["error"] or "tool returned ok=False")

    return _finalise(
        ExecutionVerdict(
            request_id=request_id,
            option_id=option_id,
            affected_service=request.affected_service,
            status=status,
            dry_run=request.dry_run,
            decision=summary,
            tool_capability=tool_capability,
            tool_args=tool_args,
            tool_result=tool_result_dict,
            would_execute=False,  # we actually executed; no "would" implied
            error=error,
            rationale=rationale,
            audit_metadata=AuditMetadata(created_at=datetime.now(UTC), decision_trace=trace),
        )
    )


def _finalise(verdict: ExecutionVerdict) -> ExecutionVerdict:
    """Persist the verdict (best-effort) and return it.

    A DB blip MUST NOT prevent the caller from receiving the verdict.
    Failures are logged at WARNING and swallowed — the in-memory
    response is the source of truth for the immediate caller; the row
    is for history / future learning.
    """
    try:
        # Local import keeps the agent importable in environments where
        # ``aiops.state`` isn't initialised (CI smoke without a DB).
        from aiops.state import repository as _repo

        _repo.save_execution(verdict)
    except Exception as exc:  # broad on purpose; persistence is best-effort
        logger.warning(
            "auto_heal.lite: failed to persist ExecutionVerdict (%s); "
            "in-memory verdict returned anyway",
            exc,
        )
    return verdict


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out.

    Dispatches on input shape:

    - If the input carries an ``option`` key, route to ``execute`` (the
      PRS-002 generic Day-1 path).
    - Otherwise, treat the input as a ``RestartRecommendation`` and run
      the legacy HITL-1 ``recommend_restart`` path. ``skip_approval`` is
      forced ``True`` for this branch so the eval harness never blocks
      on a pending HITL prompt — the HITL flow itself is covered by
      dedicated tests, not the eval harness.
    """
    if isinstance(input, dict) and "option" in input:
        req = ExecutionRequest.model_validate(input)
        verdict = execute(req)
        return verdict.model_dump(mode="json")

    rec = RestartRecommendation.model_validate(input)
    outcome = recommend_restart(rec, hitl_context={"skip_approval": True})
    return outcome.model_dump(mode="json")
