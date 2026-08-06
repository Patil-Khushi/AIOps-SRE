"""Executor for approved RCA fix steps — the RCA → approve → remediate loop.

The RCA Agent (PRS-008) only *recommends* fix steps; it never acts. This
module is the platform-side executor that carries out an approved step
through an existing seam, gated by the REQUIRED-HITL capability
``rca.fix_step.execute`` (registered in ``aiops/policy/gate.py``). Because the
capability is REQUIRED, ``ToolRegistry.call`` runs the approval flow before the
tool body executes: it posts an interactive approve/deny prompt through the
chatops/Slack seam, blocks until a human resolves it, and only then runs the
action. This is the *same* machinery the HITL-1 auto-heal restart demo uses —
reused here, not reinvented.

The executor follows the *machine-readable action* the RCA agent annotates on
each fix step (``RankedFixStep.action_type`` + ``flag`` + ``variant``), not a
service name the UI re-derived. v0 implements one reversible action —
``set_flag``, flipping a flagd feature flag off, the real remediation for the
OTel-demo failure scenarios (``paymentFailure``, ``productCatalogFailure``,
...). ``rollback_deploy`` and ``manual`` steps are recognised but have no
automated executor yet, so they return ``ok=False`` with a clear "perform
manually" message rather than silently doing nothing — mirroring how the
restart demo wires exactly one action end-to-end (CLAUDE.md POC discipline).
"""

from __future__ import annotations

from typing import Any

from aiops.tools.registry import ToolResult, get_registry, tool

# Action verbs the agent may annotate on a fix step. Kept as plain strings so
# this module has no import dependency on the agents package (the dep arrow
# runs agents → aiops, never the reverse).
_ACTION_SET_FLAG = "set_flag"
_ACTION_ROLLBACK_DEPLOY = "rollback_deploy"
_ACTION_MANUAL = "manual"

# Human-readable "why nothing ran" for the actions v0 recognises but cannot
# execute automatically. The string is surfaced to the operator verbatim.
_NO_EXECUTOR_MESSAGE = {
    _ACTION_ROLLBACK_DEPLOY: (
        "No automated executor for 'rollback_deploy' in v0 — roll back the "
        "deploy manually (e.g. `helm rollback otel-demo <prior-revision>`)."
    ),
    _ACTION_MANUAL: (
        "This fix step is a manual action — no automated executor. Perform "
        "the step described in the RCA verdict by hand."
    ),
}


@tool(
    name="seam.rca.fix_step.execute",
    capability="rca.fix_step.execute",
    provider="seam",
    description="Execute an approved RCA fix step via an existing platform seam.",
)
def execute_rca_fix_step(
    action: str = "",
    flag: str = "",
    variant: str = "off",
    **_: Any,
) -> ToolResult:
    """Carry out one approved RCA fix step, dispatching on ``action``.

    ``action`` is the step's ``action_type`` (``set_flag`` / ``rollback_deploy``
    / ``manual``). Only reached *after* the registry's HITL gate has approved
    the call — REQUIRED-level ``rca.fix_step.execute`` blocks upstream until a
    human approves. ``**_`` swallows any extra context keys the caller forwards.
    """
    if action == _ACTION_SET_FLAG:
        if not flag:
            return ToolResult(ok=False, error="set_flag action requires a 'flag' name")
        # Delegate to whichever provider serves `automation.fault.clear`.
        #
        # This used to call `feature_flags.set_variant` directly, because the
        # OTel Demo's faults WERE flagd flags. That app is gone; ecommerce
        # faults are env vars and replica counts, cleared via kubectl.
        #
        # The capability is deliberately generic and the provider lives in the
        # demo layer (demo/ui/fault_clear.py). aiops/ must not import demo/ —
        # the dependency arrow runs demo → aiops — so the executor dispatches by
        # capability name and stays ignorant of how a fault is actually undone.
        # With no provider registered the registry returns ok=False, which is
        # the same degradation every other unconfigured seam shows.
        return get_registry().call("automation.fault.clear", fault=flag, target=variant)
    if action in _NO_EXECUTOR_MESSAGE:
        return ToolResult(
            ok=False,
            error=_NO_EXECUTOR_MESSAGE[action],
            metadata={"unsupported_action": action},
        )
    return ToolResult(ok=False, error=f"unsupported RCA fix action: {action!r}")


def request_fix_step(
    *,
    action_type: str = _ACTION_SET_FLAG,
    flag: str = "",
    variant: str = "off",
    hitl_context: dict[str, Any],
) -> dict[str, Any]:
    """Request an approved RCA fix step; return a JSON-able outcome dict.

    Blocks inside the registry call until the human approves / denies / the
    request expires — the platform HITL gate owns that wait — then maps the
    result to a status the demo UI can render: ``executed`` / ``denied`` /
    ``expired`` / ``blocked`` / ``unsupported`` / ``error``.

    ``unsupported`` is the honest outcome for a step the agent annotated as
    ``rollback_deploy`` / ``manual`` (or with no executor): the approval still
    runs, but the executor reports that there is nothing to automate.
    """
    result = get_registry().call(
        "rca.fix_step.execute",
        hitl_context=hitl_context,
        action=action_type,
        flag=flag,
        variant=variant,
    )

    approval_id = hitl_context.get("pending_approval_id")
    approver: str | None = None
    # Look the request up once so we can report who approved it (success) or
    # whether it was denied vs expired (failure). Best-effort: a missing
    # registry / unknown id just leaves these unset.
    if approval_id:
        try:
            from aiops.policy import ApprovalStatus, get_approval_registry

            req = get_approval_registry().get(approval_id)
            approver = req.approver
        except Exception:
            req = None  # type: ignore[assignment]
    else:
        req = None  # type: ignore[assignment]

    if result.ok:
        return {
            "status": "executed",
            "approval_id": approval_id,
            "approver": approver,
            "action_type": action_type,
            "flag": flag,
            "variant": variant,
            "result": result.data if isinstance(result.data, dict) else {"value": result.data},
        }

    status = "error"
    if (result.metadata or {}).get("blocked_by") == "hitl_gate":
        status = "blocked"
        if req is not None:
            if req.status is ApprovalStatus.DENIED:
                status = "denied"
            elif req.status is ApprovalStatus.EXPIRED:
                status = "expired"
    elif (result.metadata or {}).get("unsupported_action"):
        # Approved, but there is no automated executor for this action type.
        status = "unsupported"
    return {
        "status": status,
        "approval_id": approval_id,
        "approver": approver,
        "action_type": action_type,
        "flag": flag,
        "variant": variant,
        "error": result.error,
    }


def request_flag_fix(
    *,
    flag: str,
    variant: str = "off",
    hitl_context: dict[str, Any],
) -> dict[str, Any]:
    """Back-compat shim: request an approved ``set_flag`` fix step.

    Retained for callers that only ever flip flags. New callers should use
    :func:`request_fix_step` and pass the step's ``action_type`` so non-flag
    steps report ``unsupported`` instead of being forced through the flag path.
    """
    return request_fix_step(
        action_type=_ACTION_SET_FLAG,
        flag=flag,
        variant=variant,
        hitl_context=hitl_context,
    )
