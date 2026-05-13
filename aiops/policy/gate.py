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

    def level_for(self, action: str) -> AutonomyLevel:
        if action in self._levels:
            return self._levels[action]
        # Unknown action: fall back to env default so we fail safe-ish.
        default = os.environ.get("AIOPS_HITL_DEFAULT", "optional").lower()
        return AutonomyLevel(default)

    def check(self, action: str, context: dict[str, Any] | None = None) -> Decision:
        ctx = context or {}
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
                reason="tenant gate on; approver " + ("present" if approver else "missing"),
                approver=approver,
            )
        # REQUIRED
        approver = self._approver(action, ctx)
        return Decision(
            allowed=approver is not None,
            level=level,
            reason="required HITL; approver " + ("present" if approver else "missing"),
            approver=approver,
        )

    def enforce(self, action: str, context: dict[str, Any] | None = None) -> Decision:
        d = self.check(action, context)
        if not d.allowed:
            raise GateError(f"blocked: action={action!r} level={d.level.value} reason={d.reason}")
        return d


_GATE: HITLGate | None = None


def get_gate() -> HITLGate:
    global _GATE
    if _GATE is None:
        _GATE = HITLGate()
    return _GATE
