"""Deterministic risk + autonomy evaluation for a step and for a whole plan (§13/§14).

Pure functions over the action registry, the runbook's declared rollback and the
incident's environment. No I/O, no clock, no LLM: the same plan on the same
environment always produces the same level and the same list of factors, and every
level comes with the factors that produced it so an operator can argue with it.

**This is not a second policy system.** Risk can only ever be *stricter* than
``aiops/policy/gate.py``:

- ``hitl_required`` here means "this run needs a human". Whether a human is obtained,
  and whether the action runs at all, is decided by the platform gate at the registry
  boundary. Risk never grants autonomy and never marks anything pre-approved.
- ``blocked`` means the executor refuses *before* the gate is consulted. A pre-gate
  refusal cannot bypass policy — it can only decline to ask.

The existing ``run_plan`` execution path is unchanged by this module: destructive
steps still route through the REQUIRED-HITL capability exactly as before. Risk informs
the dry run, the candidate list and the new plan/execute path.
"""

from __future__ import annotations

import os
from enum import StrEnum

from pydantic import BaseModel, Field

from agents.runbook_executor.actions import (
    ActionSpec,
    AutonomyClass,
    BlastRadius,
    resolve_action,
)
from agents.runbook_executor.library import ExecutableRunbook
from agents.runbook_executor.models import RunbookStep

# Environments where a mutation is a customer-visible event rather than a rehearsal.
_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod", "live"})

_WIDE_BLAST = frozenset({BlastRadius.MULTI_SERVICE, BlastRadius.CLUSTER})


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def rank(level: RiskLevel) -> int:
    return _ORDER[level]


def max_level(levels: list[RiskLevel]) -> RiskLevel:
    """The highest level in ``levels`` (LOW for an empty plan)."""
    return max(levels, key=rank) if levels else RiskLevel.LOW


def is_production(environment: str) -> bool:
    return (environment or "").strip().lower() in _PRODUCTION_ENVIRONMENTS


def hitl_threshold() -> RiskLevel:
    """Risk level at or above which a human is demanded regardless of the step's own
    ``destructive`` flag. Read per call, never at import (monkeypatch in tests)."""
    raw = os.environ.get("AIOPS_RUNBOOK_HITL_RISK_THRESHOLD", "").strip().upper()
    try:
        return RiskLevel(raw) if raw else RiskLevel.HIGH
    except ValueError:
        return RiskLevel.HIGH


class StepRisk(BaseModel):
    """Risk assessment for one resolved step."""

    step_name: str
    action_id: str
    level: RiskLevel
    autonomy: AutonomyClass
    mutating: bool
    disruptive: bool
    blast_radius: BlastRadius
    rollback_available: bool
    # How the step can be undone: "action" (an explicit rollback_action), "baseline"
    # (the action itself restores the declared default, so no reverse is needed),
    # "not_needed" (read-only), or "none" (irreversible by the executor).
    rollback_kind: str = "none"
    rollback_action: str | None = None
    retry_safe: bool = False
    expected_impact: str = ""
    factors: list[str] = Field(default_factory=list)

    @property
    def requires_hitl(self) -> bool:
        """A human is needed for this step — because the platform gate will demand it
        (destructive ⇒ REQUIRED capability) or because the risk is high enough that
        the executor demands it anyway."""
        return self.autonomy >= AutonomyClass.HUMAN_APPROVAL or rank(self.level) >= rank(
            hitl_threshold()
        )


class PlanRisk(BaseModel):
    """Rolled-up risk for an ordered plan of steps."""

    level: RiskLevel
    hitl_required: bool
    production_mutation: bool
    rollback_available: bool  # every mutating step can be undone
    blocked: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    factors: list[str] = Field(default_factory=list)
    steps: list[StepRisk] = Field(default_factory=list)

    @property
    def mutating_steps(self) -> int:
        return sum(1 for s in self.steps if s.mutating)


def assess_step(
    step: RunbookStep,
    *,
    environment: str = "",
    spec: ActionSpec | None = None,
) -> StepRisk:
    """Risk for one step. An unresolvable action is CRITICAL, not LOW.

    Escalation ladder, in order of severity:

    - read-only                                            → LOW
    - mutating but not disruptive (a drain)                 → LOW, MEDIUM in production
    - disruptive with a declared rollback                   → MEDIUM, HIGH if wide blast
    - disruptive with no rollback                           → HIGH, CRITICAL if wide blast
    - unknown action                                        → CRITICAL (refused)
    """
    spec = spec or resolve_action(step.action)
    prod = is_production(environment)
    factors: list[str] = []

    if spec is None:
        return StepRisk(
            step_name=step.name,
            action_id=step.action,
            level=RiskLevel.CRITICAL,
            autonomy=AutonomyClass.BLOCKED,
            mutating=True,  # unknown ⇒ assume the worst
            disruptive=True,
            blast_radius=BlastRadius.CLUSTER,
            rollback_available=False,
            rollback_kind="none",
            factors=[f"action {step.action!r} is not in the action registry"],
        )

    if not spec.mutating:
        rollback_kind = "not_needed"
    elif step.rollback_action:
        rollback_kind = "action"
    elif spec.restores_default:
        rollback_kind = "baseline"
    else:
        rollback_kind = "none"
    rollback_available = rollback_kind != "none"
    if prod:
        factors.append("production environment")

    if not spec.mutating:
        level = RiskLevel.LOW
        factors.append("read-only action — no mutation")
    elif not spec.disruptive:
        level = RiskLevel.MEDIUM if prod else RiskLevel.LOW
        factors.append("mutating but non-disruptive")
    elif rollback_kind == "action":
        level = RiskLevel.HIGH if spec.blast_radius in _WIDE_BLAST else RiskLevel.MEDIUM
        factors.append(f"disruptive, reversible via {step.rollback_action!r}")
    elif rollback_kind == "baseline":
        # Restoring the declared default is not an irreversible mutation: the worst
        # case is that the system is back where it started, which is the goal.
        level = RiskLevel.HIGH if spec.blast_radius in _WIDE_BLAST else RiskLevel.MEDIUM
        factors.append("disruptive, but restores the declared baseline — no reverse needed")
    else:
        level = RiskLevel.CRITICAL if spec.blast_radius in _WIDE_BLAST else RiskLevel.HIGH
        factors.append("disruptive with no declared rollback — not reversible by the executor")

    if spec.blast_radius is not BlastRadius.NONE:
        factors.append(f"blast radius: {spec.blast_radius.value}")
    if spec.mutating and not spec.retry_safe:
        factors.append("not retry-safe — a timeout cannot be safely re-issued")

    return StepRisk(
        step_name=step.name,
        action_id=spec.action_id,
        level=level,
        autonomy=spec.autonomy,
        mutating=spec.mutating,
        disruptive=spec.disruptive,
        blast_radius=spec.blast_radius,
        rollback_available=rollback_available,
        rollback_kind=rollback_kind,
        rollback_action=step.rollback_action,
        retry_safe=spec.retry_safe,
        expected_impact=spec.expected_impact,
        factors=factors,
    )


def assess_plan(
    runbook: ExecutableRunbook,
    *,
    environment: str = "",
    steps: list[RunbookStep] | None = None,
) -> PlanRisk:
    """Roll step risks up into a plan verdict.

    The plan level is the highest step level — remediation is not averaged, because a
    plan containing one irreversible step is an irreversible plan. ``CRITICAL``
    blocks: §14's LEVEL 4 is "blocked", and the executor declines to ask for approval
    for something it should not be automating at all.
    """
    ordered = steps if steps is not None else runbook.steps
    step_risks = [assess_step(s, environment=environment) for s in ordered]
    level = max_level([s.level for s in step_risks])
    prod = is_production(environment)
    mutating = [s for s in step_risks if s.mutating]

    factors: list[str] = []
    if prod and mutating:
        factors.append(f"{len(mutating)} mutating step(s) against production")
    # Only *disruptive* steps decide whether the plan is undoable. A non-disruptive
    # mutation with no declared reverse (a drain annotation) is reported as a factor
    # rather than making the whole plan "no rollback": the executor's rollback loop
    # already treats such a step as trivially reverted, and calling the plan
    # irreversible because of it would overstate the risk of every restart runbook.
    disruptive = [s for s in mutating if s.disruptive]
    irreversible = [s for s in disruptive if not s.rollback_available]
    if irreversible:
        factors.append(
            "irreversible step(s): " + ", ".join(sorted(s.step_name for s in irreversible))
        )
    undeclared = [s for s in mutating if not s.disruptive and not s.rollback_available]
    if undeclared:
        factors.append(
            "non-disruptive step(s) with no declared reverse: "
            + ", ".join(sorted(s.step_name for s in undeclared))
        )
    if not mutating:
        factors.append("read-only plan — nothing is changed")

    blocking: list[str] = []
    for s in step_risks:
        if s.level is RiskLevel.CRITICAL:
            blocking.append(
                f"step {s.step_name!r} ({s.action_id}) is CRITICAL risk: "
                + "; ".join(s.factors)
                + " — CRITICAL actions are never executed automatically"
            )

    hitl = any(s.requires_hitl for s in step_risks) or rank(level) >= rank(hitl_threshold())

    return PlanRisk(
        level=level,
        hitl_required=hitl,
        production_mutation=bool(prod and mutating),
        rollback_available=all(s.rollback_available for s in disruptive) if disruptive else True,
        blocked=bool(blocking),
        blocking_reasons=blocking,
        factors=factors,
        steps=step_risks,
    )


__all__ = [
    "PlanRisk",
    "RiskLevel",
    "StepRisk",
    "assess_plan",
    "assess_step",
    "hitl_threshold",
    "is_production",
    "max_level",
    "rank",
]
