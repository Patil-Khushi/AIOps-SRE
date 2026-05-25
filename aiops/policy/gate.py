"""HITL gate.

Three autonomy levels per Solution Design slide 10:

- ``NONE``     — fully autonomous. Read-only / low-risk actions.
- ``OPTIONAL`` — agent acts by default; tenant can switch on a human gate.
- ``REQUIRED`` — human approval mandatory. Destructive / irreversible /
                 policy-critical / financial. Includes every RCA Agent fix step.

The mapping of action -> level is owned by ``policies/hitl.rego`` (OPA). Phase 0
hard-codes a default mapping in ``DEFAULT_LEVELS`` so we can ship the seam
before OPA is wired in.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class AutonomyLevel(enum.StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass
class Decision:
    allowed: bool
    level: AutonomyLevel
    reason: str
    approver: str | None = None


class GateError(RuntimeError):
    """Raised when an action is blocked by HITL policy."""


# Phase 0 defaults. Phase 1+ replaces this dict with an OPA query.
# Keys are tool capabilities (see aiops/tools); values are autonomy levels
# from the catalog.
DEFAULT_LEVELS: dict[str, AutonomyLevel] = {
    # Reactive-Active phase
    "itsm.incident.create": AutonomyLevel.OPTIONAL,
    "itsm.incident.update": AutonomyLevel.OPTIONAL,
    "automation.runbook.execute": AutonomyLevel.REQUIRED,
    "notify.send": AutonomyLevel.NONE,
    "observability.metrics.query": AutonomyLevel.NONE,
    "observability.metrics.alerts": AutonomyLevel.NONE,
    "observability.logs.query": AutonomyLevel.NONE,
    "observability.traces.query": AutonomyLevel.NONE,
    "observability.traces.search": AutonomyLevel.NONE,
    "observability.traces.services": AutonomyLevel.NONE,
    "itsm.cmdb.lookup": AutonomyLevel.NONE,
    "oncall.schedule.lookup": AutonomyLevel.NONE,
    # Prescriptive-Adaptive phase
    "remediation.recommend": AutonomyLevel.REQUIRED,
    "auto_heal.execute": AutonomyLevel.OPTIONAL,
    "policy.optimize": AutonomyLevel.REQUIRED,
    "feedback.promote_model": AutonomyLevel.REQUIRED,
    "knowledge.publish": AutonomyLevel.REQUIRED,
    "chaos.experiment.run": AutonomyLevel.REQUIRED,
    "rca.fix_step.execute": AutonomyLevel.REQUIRED,
    # Predictive phase (where action implications are big)
    "capacity.recommend": AutonomyLevel.REQUIRED,
    "slo.freeze_changes": AutonomyLevel.REQUIRED,
    "change.predict_risk": AutonomyLevel.REQUIRED,
}


ApproverFn = Callable[[str, dict[str, Any]], str | None]
"""(action, context) -> approver id if approved, else None.

In Phase 0 this is always ``_no_approver`` so REQUIRED actions block. Phase 1
wires a real UI (Slack interaction, web approve screen) into here.
"""


def _no_approver(_action: str, _ctx: dict[str, Any]) -> str | None:
    return None


class HITLGate:
    def __init__(
        self,
        levels: dict[str, AutonomyLevel] | None = None,
        approver: ApproverFn = _no_approver,
    ) -> None:
        self._levels = dict(DEFAULT_LEVELS, **(levels or {}))
        self._approver = approver

    # ─── public approver accessors (HITL-4, #104) ───────────────────────
    #
    # Phase 1 wired the chatops-bridging approver into the gate by poking
    # ``gate._approver = ...`` from a module-level installer.  Pragmatic
    # at the time, but it tied the installer to private state and made
    # the gate awkward to subclass / mock from tests.  The setter below
    # is the supported way to swap the approver; the legacy attribute is
    # still readable so any not-yet-migrated callers don't break.

    def set_approver(self, fn: ApproverFn) -> None:
        """Replace the gate's approver function.

        Used by :func:`aiops.policy.install_default_approver` to wire the
        chatops-bridged ``ApprovalRequester`` into the gate at startup.
        Tests use it to install fakes/stubs without reaching into private
        state.  Passing ``_no_approver`` restores the fail-closed default.
        """
        self._approver = fn

    @property
    def approver(self) -> ApproverFn:
        """The currently installed approver function (read-only)."""
        return self._approver

    def level_for(self, action: str) -> AutonomyLevel:
        if action in self._levels:
            return self._levels[action]
        # Unknown action: fall back to env default so we fail safe-ish.
        default = os.environ.get("AIOPS_HITL_DEFAULT", "optional").lower()
        return AutonomyLevel(default)

    def check(self, action: str, context: dict[str, Any] | None = None) -> Decision:
        # Preserve the caller's dict identity (don't replace an empty dict with
        # a fresh one): ApprovalRequester writes ``pending_approval_id`` back
        # into ``context`` so the agent can surface it to the user.
        ctx = {} if context is None else context
        level = self.level_for(action)
        if level is AutonomyLevel.NONE:
            return Decision(
                allowed=True,
                level=level,
                reason="autonomous (level=none)",
            )
        if level is AutonomyLevel.OPTIONAL:
            tenant_gate = ctx.get("tenant_requires_hitl", False)
            if not tenant_gate:
                return Decision(
                    allowed=True,
                    level=level,
                    reason="tenant has not enabled HITL gate",
                )
            approver = self._approver(action, ctx)
            return Decision(
                allowed=approver is not None,
                level=level,
                reason=_outcome_reason(approver, ctx, prefix="tenant gate on; "),
                approver=approver,
            )
        # REQUIRED
        approver = self._approver(action, ctx)
        return Decision(
            allowed=approver is not None,
            level=level,
            reason=_outcome_reason(approver, ctx, prefix="required HITL; "),
            approver=approver,
        )

    def enforce(self, action: str, context: dict[str, Any] | None = None) -> Decision:
        d = self.check(action, context)
        if not d.allowed:
            raise GateError(f"blocked: action={action!r} level={d.level.value} reason={d.reason}")
        return d


def _outcome_reason(approver: str | None, ctx: dict[str, Any], *, prefix: str = "") -> str:
    """Build a human-readable reason for the gate's Decision.

    ``ApprovalRequester`` annotates ``ctx`` with the resolved approval's
    status / approver / reason before returning ``None`` on deny/expire.
    Surfacing those here turns the gate's error from a generic "approver
    missing" into the spec's "denied by <approver>" / "expired" wording,
    which the agent + UI + audit log all reuse.

    Falls back to the v0 wording when no approval flow was involved (the
    approver function is the legacy stub or a custom synchronous approver).
    """
    if approver is not None:
        return f"{prefix}approved by {approver}"
    decision = ctx.get("approval_decision")
    decision_approver = ctx.get("approval_approver")
    decision_reason = (ctx.get("approval_reason") or "").strip()
    if decision == "denied" and decision_approver:
        tail = f": {decision_reason}" if decision_reason else ""
        return f"{prefix}denied by {decision_approver}{tail}"
    if decision == "expired":
        return f"{prefix}expired (no human response in time)"
    return f"{prefix}approver missing"


_GATE: HITLGate | None = None


def get_gate() -> HITLGate:
    global _GATE
    if _GATE is None:
        _GATE = HITLGate()
    return _GATE
