"""Auto-Healer-lite — the HITL-1 demo agent (issue #77).

Path::

    recommend_restart(rec) -> registry.call("automation.runbook.execute", ...)
                          -> gate.check  (level=REQUIRED)
                          -> approver  (= ApprovalRequester)
                              -> registry.create  + chatops "approval requested"
                              -> wait until approve / deny / expire
                          -> if approved: tool runs, returns ToolResult
                             if denied/expired: ToolResult(ok=False, ...)
    <- RestartOutcome (executed / denied / expired / blocked / error)

The agent does *not* know whether HITL fired.  It builds an
``automation.runbook.execute`` call, hands it to the registry, and
interprets the result.  Authorization, approval prompting, audit logging
— all of that is owned by the platform.
"""

from __future__ import annotations

import logging
from typing import Any

# Side-effect import: registers the mock automation.runbook.execute provider.
import aiops.tools.mock_providers  # noqa: F401
from agents.auto_healer_lite.models import RestartOutcome, RestartRecommendation
from aiops.tools import get_registry

logger = logging.getLogger(__name__)


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


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out.

    Forces ``skip_approval=True`` so the eval harness never blocks on a
    pending HITL prompt.  Without it, every golden case would deadlock on
    the registry's wait_for() until the per-call timeout.  The HITL flow
    itself is covered by dedicated tests, not the eval harness.
    """
    rec = RestartRecommendation.model_validate(input)
    outcome = recommend_restart(rec, hitl_context={"skip_approval": True})
    return outcome.model_dump(mode="json")
