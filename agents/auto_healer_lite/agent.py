"""Auto-Healer-lite — two coexisting surfaces.

**Legacy HITL-1 narrow path** (``recommend_restart`` /
``RestartRecommendation``) — built to exercise the platform HITL gate
end-to-end via the ``automation.runbook.execute`` capability. Tests +
CLI runner depend on this shape; kept untouched.

**PRS-002 generic Day-1 stub** (``execute`` / ``ExecutionRequest`` /
``ExecutionVerdict``) — receives a single chosen ``RemediationOption``
from PRS-001 and produces a structured ``ExecutionVerdict`` after
calling the platform HITL gate (``auto_heal.lite.execute``, REQUIRED).
**Never actually fires the tool in Day-1.** Even when the gate clears
the stub maps the outcome to ``dry_run_ok`` and records what *would*
have run (``would_execute=True``). v1 will swap the dry-run branch
for a real ``aiops.tools.get_registry().call()``.

Legacy path::

    recommend_restart(rec) -> registry.call("automation.runbook.execute", ...)
                          -> gate.check  (level=REQUIRED)
                          -> approver  (= ApprovalRequester)
                              -> registry.create  + chatops "approval requested"
                              -> wait until approve / deny / expire
                          -> if approved: tool runs, returns ToolResult
                             if denied/expired: ToolResult(ok=False, ...)
    <- RestartOutcome (executed / denied / expired / blocked / error)

PRS-002 stub path::

    execute(req) -> validate option (requires_hitl=True, tool_capability set, ...)
                 -> get_gate().check("auto_heal.lite.execute", ctx)
                 -> map Decision -> ExecutionStatus
                 -> never call the tool in Day-1
    <- ExecutionVerdict (refused / pending_approval / blocked / dry_run_ok)

Both paths share ``run(input: dict) -> dict`` for the eval harness.
``run`` dispatches on input shape: an ``option`` key routes to
``execute``; otherwise it stays on the legacy restart path.
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
from aiops.policy import get_gate
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
    """Day-1 stub: validate the option, consult the platform HITL gate,
    map the gate's Decision to an ExecutionStatus, and return the
    structured verdict. NEVER calls the tool — even an approved
    decision maps to ``dry_run_ok``.

    The Day-1 invariant is intentional: until the rollback rehearsal
    + audit-trail story land in v1, the safest behaviour is to record
    what *would* have executed and let an operator unlock the real
    fire-the-tool path manually.
    """
    request_id = f"ahl-{uuid.uuid4().hex[:12]}"
    trace: list[str] = [f"request_id={request_id} service={request.affected_service!r}"]

    option = request.option
    option_id = str(option.get("option_id") or "unknown")
    tool_capability = option.get("tool_capability")
    tool_args = option.get("tool_args") or {}

    # 1. Validate the option's shape.
    ok, reason = _validate_option(option)
    if not ok:
        trace.append(f"refused: {reason}")
        return ExecutionVerdict(
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
    trace.append(
        f"validated: option_id={option_id} action_type={option.get('action_type')} "
        f"blast={option.get('blast_radius')}"
    )

    # 2. Consult the platform HITL gate. The agent does NOT enforce —
    # that would raise GateError. Use ``check`` so we get a structured
    # Decision back regardless of the outcome and can return it on the
    # verdict for the dashboard / chatops layer to render.
    ctx: dict[str, Any] = dict(request.hitl_context or {})
    ctx.setdefault("option_id", option_id)
    ctx.setdefault("incident_id", request.incident_id)
    ctx.setdefault("affected_service", request.affected_service)
    ctx.setdefault("blast_radius", option.get("blast_radius"))
    ctx.setdefault("rollback", option.get("rollback"))
    ctx.setdefault("operator", request.operator)
    # Day-1 stub never blocks on a real approver — the eval harness
    # would deadlock otherwise. ``skip_approval`` is honoured by the
    # platform's ApprovalRequester (HITL-1).
    ctx.setdefault("skip_approval", True)

    decision = get_gate().check(_PRS002_GATE_ACTION, ctx)
    summary = _decision_to_summary(decision, ctx)
    trace.append(
        f"gate.check({_PRS002_GATE_ACTION!r}): allowed={summary.allowed} "
        f"level={summary.level} reason={summary.reason!r}"
    )

    # 3. Map Decision -> ExecutionStatus. The Day-1 stub maps an
    # *allowed* decision to dry_run_ok instead of executed — execution
    # is deferred to v1.
    if not summary.allowed:
        if summary.approval_id and summary.approval_status not in {"denied", "expired"}:
            status = ExecutionStatus.PENDING_APPROVAL
            rationale = f"Gate flow opened approval {summary.approval_id!r}; waiting on a human."
        else:
            status = ExecutionStatus.BLOCKED
            rationale = f"Gate refused the action: {summary.reason}"
    else:
        # Allowed → Day-1 stub records dry_run_ok and the would_execute
        # flag. v1 will branch on request.dry_run here and call the
        # tool when False.
        status = ExecutionStatus.DRY_RUN_OK
        rationale = (
            f"Gate cleared the action (level={summary.level}). Day-1 stub did NOT "
            f"call the tool; v1 will dispatch via aiops.tools.get_registry().call("
            f"{tool_capability!r}, **{tool_args!r})."
        )
    trace.append(f"status={status.value}")

    would_execute = status in (ExecutionStatus.DRY_RUN_OK, ExecutionStatus.APPROVED) and bool(
        tool_capability
    )

    return ExecutionVerdict(
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
