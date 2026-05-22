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
from typing import Any, Literal


class AutonomyLevel(enum.StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


# Literal type for ApprovalSummary.status. Kept narrow so type-checkers
# catch typos at call sites (avoids "appoved" vs "approved" landing in
# audit logs).
ApprovalOutcome = Literal["approved", "denied", "expired"]


@dataclass
class ApprovalSummary:
    """Structured record of an approval flow that the approver carried out.

    Returned by :class:`~aiops.policy.approvals.ApprovalRequester` and
    surfaced on :attr:`Decision.approval` so callers (agents, UIs, audit
    logs) can render rich "denied by alice@x.io: blast radius too large"
    messages without scraping :class:`Decision.reason` strings.

    HITL-5 (#105): this replaces the old "writeback into the caller's
    ``hitl_context`` dict" back-channel — the gate is no longer spooky
    action at a distance.
    """

    id: str
    status: ApprovalOutcome
    approver: str | None
    reason: str


@dataclass
class ApproverResult:
    """What an :data:`ApproverFn` returns.

    ``approver`` is the canonical approval signal — non-``None`` means
    the gate allows the action.  ``summary`` is optional metadata about
    the flow (populated by :class:`~aiops.policy.approvals.ApprovalRequester`
    on every outcome: approved, denied, expired).  Synchronous test
    approvers can leave ``summary=None`` and return only ``approver``.

    The legacy ``(action, ctx) -> str | None`` protocol is still accepted
    by :meth:`HITLGate.check` via :func:`_coerce_approver_result`, so
    code that hasn't migrated yet keeps working.
    """

    approver: str | None
    summary: ApprovalSummary | None = None


@dataclass
class Decision:
    allowed: bool
    level: AutonomyLevel
    reason: str
    approver: str | None = None
    # HITL-5 (#105): populated for REQUIRED-level (and tenant-gated
    # OPTIONAL-level) decisions that went through the approval flow.
    # ``None`` for NONE-level passes and for OPTIONAL with no tenant
    # gate, since no human approver was consulted in those paths.
    approval: ApprovalSummary | None = None


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


ApproverFn = Callable[[str, dict[str, Any]], "ApproverResult | str | None"]
"""``(action, context) -> ApproverResult`` (preferred) or legacy ``str | None``.

In Phase 0 this is always :func:`_no_approver` so REQUIRED actions block.
Phase 1 wires a real UI (Slack interaction, web approve screen) into here.

HITL-5 (#105) introduced :class:`ApproverResult` so the gate can surface
the structured approval outcome on :attr:`Decision.approval` without the
approver mutating the caller's context dict.  Legacy approvers that
still return a bare ``str | None`` are accepted via
:func:`_coerce_approver_result`.
"""


def _no_approver(_action: str, _ctx: dict[str, Any]) -> ApproverResult:
    return ApproverResult(approver=None, summary=None)


def _coerce_approver_result(value: ApproverResult | str | None) -> ApproverResult:
    """Wrap a legacy ``str | None`` approver return in an :class:`ApproverResult`.

    Lets pre-HITL-5 approvers — ``lambda action, ctx: "alice"``, ``_no_approver``
    callers in tests — keep working without touching the gate's logic.
    """
    if isinstance(value, ApproverResult):
        return value
    # ``value`` is either ``str`` (legacy approved) or ``None`` (legacy blocked).
    return ApproverResult(approver=value, summary=None)


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
        # into ``context`` so the agent can surface it to the user.  That
        # remains the *only* writeback as of HITL-5 (#105) — all other
        # approval metadata is returned via :class:`ApproverResult`.
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
            result = _coerce_approver_result(self._approver(action, ctx))
            return Decision(
                allowed=result.approver is not None,
                level=level,
                reason=_outcome_reason(result, prefix="tenant gate on; "),
                approver=result.approver,
                approval=result.summary,
            )
        # REQUIRED
        result = _coerce_approver_result(self._approver(action, ctx))
        return Decision(
            allowed=result.approver is not None,
            level=level,
            reason=_outcome_reason(result, prefix="required HITL; "),
            approver=result.approver,
            approval=result.summary,
        )

    def enforce(self, action: str, context: dict[str, Any] | None = None) -> Decision:
        d = self.check(action, context)
        if not d.allowed:
            raise GateError(f"blocked: action={action!r} level={d.level.value} reason={d.reason}")
        return d


def _outcome_reason(result: ApproverResult, *, prefix: str = "") -> str:
    """Build a human-readable reason for the gate's Decision.

    Reads the structured :class:`ApprovalSummary` carried on
    :class:`ApproverResult` (HITL-5, #105) — no more scraping ad-hoc
    keys out of the caller's context dict.  Falls back to the v0
    wording when the approver returned no summary (legacy synchronous
    approvers, the default ``_no_approver`` stub).
    """
    if result.approver is not None:
        return f"{prefix}approved by {result.approver}"
    summary = result.summary
    if summary is not None:
        if summary.status == "denied" and summary.approver:
            tail = f": {summary.reason.strip()}" if summary.reason.strip() else ""
            return f"{prefix}denied by {summary.approver}{tail}"
        if summary.status == "expired":
            return f"{prefix}expired (no human response in time)"
    return f"{prefix}approver missing"


_GATE: HITLGate | None = None


def get_gate() -> HITLGate:
    global _GATE
    if _GATE is None:
        _GATE = HITLGate()
    return _GATE
