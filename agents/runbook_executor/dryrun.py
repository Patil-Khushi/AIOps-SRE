"""Dry run — the gate every mutating execution passes through first (§15–§17).

The v0 executor previewed each step and then executed regardless: the preview was
*information*, never a *decision*. This module makes it a decision. ``dry_run`` runs the
whole validation chain — applicability → prerequisites → action resolution → parameter
validation → risk assessment → simulation — and returns ``READY`` or ``BLOCKED`` with
the reasons. Nothing downstream may execute a ``BLOCKED`` plan; the new execution path
refuses to start without a ``READY`` report whose ``plan_hash`` matches what it is asked
to run.

The dry run itself cannot mutate anything. It dispatches exactly one capability,
``automation.runbook.simulate``, which is NONE-level (read-only, never gated) and whose
providers return predictions. The apply/execute capabilities are not reachable from this
module — ``tests/test_runbook_dryrun.py`` asserts that by recording every capability a
dry run dispatches.

``simulate_call`` is injectable for the same reason ``resolution_verifier`` injects its
``metrics_call``: the whole chain then unit-tests with no registry, no cluster and no
provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from agents.runbook_executor import risk as risk_mod
from agents.runbook_executor.actions import (
    SIMULATE_CAP,
    StepValidation,
    capability_for,
    validate_runbook,
)
from agents.runbook_executor.applicability import (
    ApplicabilityResult,
    ApplicabilityStatus,
    IncidentContext,
    evaluate,
)
from agents.runbook_executor.library import ExecutableRunbook
from agents.runbook_executor.models import RunbookStep
from agents.runbook_executor.simulation import SimulationDetail

logger = logging.getLogger(__name__)


class DryRunStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class PlannedStepView(BaseModel):
    """One step as the dry run sees it — §16's per-step block, structured."""

    step_id: str  # the step name; stable within a runbook version
    index: int
    action_id: str
    action_title: str
    target: str
    namespace: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    capability: str  # which capability it will dispatch through
    mutation: bool
    destructive: bool
    risk_level: risk_mod.RiskLevel
    autonomy_level: int
    rollback_available: bool
    rollback_kind: str
    rollback_action: str | None = None
    retry_safe: bool = False
    expected_impact: str = ""
    risk_factors: list[str] = Field(default_factory=list)
    simulation: SimulationDetail | None = None
    simulate_raw: dict[str, Any] | None = None
    simulated_ok: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DryRunReport(BaseModel):
    """§17's structured dry-run result.

    ``plan_hash`` is what ties a report to an execution: it covers the runbook identity
    and version plus every step's action, target, namespace and validated parameters.
    Edit the runbook, bump its version, or change a parameter and the hash changes, so a
    stale approval can never be replayed against a different plan.
    """

    status: DryRunStatus
    runbook_id: str
    runbook_version: int
    runbook_title: str = ""
    service: str = ""
    incident_id: str = ""
    plan_hash: str = ""
    steps: list[PlannedStepView] = Field(default_factory=list)
    risk_level: risk_mod.RiskLevel = risk_mod.RiskLevel.LOW
    hitl_required: bool = True
    rollback_available: bool = False
    production_mutation: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    applicability_status: ApplicabilityStatus = ApplicabilityStatus.UNKNOWN
    applicability: ApplicabilityResult | None = None
    expected_impact: str = ""

    @property
    def ready(self) -> bool:
        return self.status is DryRunStatus.READY

    @property
    def mutating_steps(self) -> int:
        return sum(1 for s in self.steps if s.mutation)


def _default_simulate(step: RunbookStep, incident_service: str) -> Any:
    """Preview one step through the NONE-level simulate capability.

    Parameters are forwarded so the preview describes the call that will actually be
    made — a dry run that omitted them would predict a different action than the one
    the execution performs.
    """
    from aiops.tools import get_registry

    return get_registry().call(
        SIMULATE_CAP,
        step=step.name,
        target=step.target or incident_service,
        namespace=step.namespace,
        action=step.action,
        params=dict(step.params or {}),
    )


def plan_hash(runbook: ExecutableRunbook, views: list[PlannedStepView]) -> str:
    """Stable digest of "exactly this plan". Same inputs ⇒ same hash, any process."""
    payload = {
        "runbook_id": runbook.id,
        "runbook_version": runbook.version,
        "steps": [
            {
                # Position is part of the identity: without it, reordering two steps that
                # share a name — or editing the first of them — leaves the digest
                # unchanged, and a stale approval could be replayed against a different
                # plan. That is the one thing this hash exists to prevent.
                "index": v.index,
                "step_id": v.step_id,
                "action": v.action_id,
                "target": v.target,
                "namespace": v.namespace,
                "parameters": {k: v.parameters[k] for k in sorted(v.parameters)},
            }
            for v in views
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _view(
    index: int,
    step: RunbookStep,
    validation: StepValidation,
    step_risk: risk_mod.StepRisk,
) -> PlannedStepView:
    spec = validation.spec
    return PlannedStepView(
        step_id=step.name,
        index=index,
        action_id=validation.action_id,
        action_title=spec.title if spec else validation.action_id,
        target=step.target or "",
        namespace=step.namespace,
        parameters=dict(validation.parameters),
        capability=capability_for(step),
        mutation=bool(spec.mutating) if spec else True,
        destructive=step.destructive,
        risk_level=step_risk.level,
        autonomy_level=int(step_risk.autonomy),
        rollback_available=step_risk.rollback_available,
        rollback_kind=step_risk.rollback_kind,
        rollback_action=step.rollback_action,
        retry_safe=step_risk.retry_safe,
        expected_impact=step_risk.expected_impact,
        risk_factors=list(step_risk.factors),
        warnings=list(validation.warnings),
        errors=list(validation.errors),
    )


def dry_run(
    runbook: ExecutableRunbook,
    ctx: IncidentContext,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    simulate_call: Callable[[RunbookStep, str], Any] | None = None,
    now: datetime | None = None,
    applicability: ApplicabilityResult | None = None,
) -> DryRunReport:
    """Validate and preview a plan without touching production.

    Blocks — in this order, because the earliest refusal is the most useful one to
    report — on: a runbook that is not ACTIVE/approved or not applicable to this
    incident; a step whose action, target, scope or parameters fail validation; a plan
    whose risk is CRITICAL. Simulation still runs for every step even when the plan is
    already blocked, so the operator sees the whole procedure they are being refused.
    """
    result = applicability or evaluate(runbook, ctx, now=now)
    validations = validate_runbook(runbook, overrides=overrides)
    plan_risk = risk_mod.assess_plan(runbook, environment=ctx.environment)

    # Paired by POSITION, not by name. ``validate_runbook`` and ``assess_plan`` both
    # walk ``runbook.steps`` in order, so index i belongs to step i — whereas a
    # name-keyed lookup silently hands two steps that share a name the same
    # validation and the same risk, and the plan then reports (and hashes) one of them
    # twice. Two shipped runbooks used to declare ``verify-health`` twice.
    views: list[PlannedStepView] = [
        _view(index, step, validation, step_risk)
        for index, (step, validation, step_risk) in enumerate(
            zip(runbook.steps, validations, plan_risk.steps, strict=True), start=1
        )
    ]

    # Preview every step. A failed simulation is a warning, not a block: the simulate
    # provider is a prediction service, and an unavailable one must not be able to
    # prevent an approved recovery (it changes nothing either way).
    sim = simulate_call or _default_simulate
    for view, step in zip(views, runbook.steps, strict=True):
        try:
            res = sim(step, ctx.service)
        except Exception as exc:  # boundary: a broken provider must not sink the plan
            logger.warning("simulate failed for step %r: %s", step.name, exc)
            view.simulate_raw = {"error": f"{type(exc).__name__}: {exc}"}
            view.simulation = SimulationDetail.from_provider(None)
            view.warnings.append(f"dry-run preview unavailable: {type(exc).__name__}")
            continue
        ok = bool(getattr(res, "ok", False))
        data = getattr(res, "data", None)
        view.simulated_ok = ok
        view.simulate_raw = data if ok else {"error": getattr(res, "error", "simulate failed")}
        view.simulation = SimulationDetail.from_provider(data if ok else None)
        if not ok:
            view.warnings.append(
                f"dry-run preview unavailable: {getattr(res, 'error', 'simulate failed')}"
            )
        elif view.simulation and view.simulation.warnings:
            view.warnings.extend(view.simulation.warnings)

    blocking: list[str] = []
    if result.status is not ApplicabilityStatus.APPLICABLE:
        blocking.append(f"applicability is {result.status.value}")
        blocking += result.blocking_reasons
    blocking += [e for v in validations for e in v.errors]
    if plan_risk.blocked:
        blocking += plan_risk.blocking_reasons

    warnings = list(result.warnings)
    warnings += [w for v in validations for w in v.warnings]
    warnings += [w for view in views for w in view.warnings]

    mutating_impacts = [v.expected_impact for v in views if v.mutation and v.expected_impact]
    expected_impact = " ".join(dict.fromkeys(mutating_impacts)) or (
        "No production change — every step is read-only."
    )

    return DryRunReport(
        status=DryRunStatus.BLOCKED if blocking else DryRunStatus.READY,
        runbook_id=runbook.id,
        runbook_version=runbook.version,
        runbook_title=runbook.title,
        service=runbook.service,
        incident_id=ctx.incident_id,
        plan_hash=plan_hash(runbook, views),
        steps=views,
        risk_level=plan_risk.level,
        hitl_required=plan_risk.hitl_required,
        rollback_available=plan_risk.rollback_available,
        production_mutation=plan_risk.production_mutation,
        blocking_reasons=blocking,
        warnings=warnings,
        risk_factors=list(plan_risk.factors),
        applicability_status=result.status,
        applicability=result,
        expected_impact=expected_impact,
    )


def render_summary(report: DryRunReport) -> str:
    """§16's human-readable rendering — used by the CLI and chatops surfaces."""
    lines = [
        "DRY RUN",
        "",
        "Runbook:",
        f"{report.runbook_id}-v{report.runbook_version}",
        "",
    ]
    for view in report.steps:
        lines += [
            f"Step {view.index}:",
            f"Action: {view.action_title}",
            f"Target: {view.target or '-'}",
            f"Mutation: {'YES' if view.mutation else 'NO'}",
            f"Risk: {view.risk_level.value}",
        ]
        if view.mutation:
            label = {
                "action": f"AVAILABLE ({view.rollback_action})",
                "baseline": "NOT REQUIRED (restores baseline)",
                "not_needed": "NOT REQUIRED (read-only)",
                "none": "NONE",
            }[view.rollback_kind]
            lines.append(f"Rollback: {label}")
        for err in view.errors:
            lines.append(f"BLOCKED: {err}")
        lines.append("")
    lines += [
        "Overall Risk:",
        report.risk_level.value,
        "",
        "Production Mutation:",
        "YES" if report.production_mutation else "NO",
        "",
        "Rollback:",
        "AVAILABLE" if report.rollback_available else "NOT AVAILABLE",
        "",
        "HITL:",
        "REQUIRED" if report.hitl_required else "NOT REQUIRED",
        "",
        "Expected impact:",
        report.expected_impact,
        "",
        "Status:",
        report.status.value,
    ]
    if report.blocking_reasons:
        lines += ["", "Blocking reasons:"]
        lines += [f"- {r}" for r in report.blocking_reasons]
    return "\n".join(lines)


__all__ = [
    "DryRunReport",
    "DryRunStatus",
    "PlannedStepView",
    "dry_run",
    "plan_hash",
    "render_summary",
]
