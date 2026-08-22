"""Dry run as a gate, not a preview (§15–§17).

The load-bearing test in this file is
``test_dry_run_dispatches_only_the_simulate_capability``: it records every capability a
dry run reaches for, so a future change that "helpfully" pre-warms something by calling
apply/execute during a preview fails here rather than in production.
"""

from __future__ import annotations

from typing import Any

import pytest

import aiops.tools.mock_providers  # noqa: F401 - registers the automation.runbook.* mocks
from agents.runbook_executor import (
    ApplicabilityScope,
    ExecutableRunbook,
    Prerequisite,
    RunbookStatus,
    RunbookStep,
    dry_run,
    get_runbook,
    render_summary,
)
from agents.runbook_executor.dryrun import DryRunStatus, plan_hash
from agents.runbook_executor.risk import RiskLevel
from aiops.tools import ToolResult, get_registry
from tests.test_runbook_matching import NOW, order_incident


@pytest.fixture
def recorded_capabilities(monkeypatch):
    """Record every capability dispatched through the registry during a test."""
    seen: list[str] = []
    original = get_registry().call

    def _spy(capability: str, **kwargs: Any) -> ToolResult:
        seen.append(capability)
        return original(capability, **kwargs)

    monkeypatch.setattr(get_registry(), "call", _spy)
    return seen


def _rb(**overrides) -> ExecutableRunbook:
    payload = {
        "id": "rb-dry",
        "title": "dry run test",
        "service": "order-service",
        "status": RunbookStatus.ACTIVE,
        "approved_by": "test",
        "steps": [
            RunbookStep(
                name="drain",
                action="drain",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            ),
            RunbookStep(
                name="restart",
                action="restart_deployment",
                destructive=True,
                rollback_action="rescale_previous",
                target="deployment/order-service",
                namespace="ecommerce",
            ),
            RunbookStep(
                name="verify",
                action="healthcheck",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            ),
        ],
        "applicability": ApplicabilityScope(
            environments=["production"],
            allowed_services=["order-service"],
            allowed_namespaces=["ecommerce"],
        ),
        "prerequisites": [Prerequisite(id="incident_active", check="incident_active")],
    }
    payload.update(overrides)
    return ExecutableRunbook(**payload)


# ─── the plan (§16) ──────────────────────────────────────────────────────────


def test_dry_run_produces_the_full_execution_plan():
    report = dry_run(_rb(), order_incident(), now=NOW)
    assert report.status is DryRunStatus.READY
    assert [s.step_id for s in report.steps] == ["drain", "restart", "verify"]
    assert [s.index for s in report.steps] == [1, 2, 3]
    assert [s.mutation for s in report.steps] == [True, True, False]
    assert [s.risk_level for s in report.steps] == [
        RiskLevel.MEDIUM,  # mutating, non-disruptive, in production
        RiskLevel.MEDIUM,  # disruptive but reversible
        RiskLevel.LOW,  # read-only
    ]
    assert report.risk_level is RiskLevel.MEDIUM
    assert report.hitl_required is True
    assert report.rollback_available is True
    assert report.production_mutation is True
    assert report.expected_impact


def test_every_step_carries_its_simulation_preview():
    report = dry_run(_rb(), order_incident(), now=NOW)
    for view in report.steps:
        assert view.simulated_ok is True
        assert view.simulate_raw and view.simulate_raw.get("dry_run") is True
        assert view.simulation is not None
        assert view.simulation.summary


def test_rendered_summary_matches_the_documented_shape():
    text = render_summary(dry_run(_rb(), order_incident(), now=NOW))
    assert text.startswith("DRY RUN")
    assert "Runbook:\nrb-dry-v1" in text
    assert "Mutation: YES" in text and "Mutation: NO" in text
    assert "Overall Risk:\nMEDIUM" in text
    assert "Production Mutation:\nYES" in text
    assert "HITL:\nREQUIRED" in text
    assert "Rollback: AVAILABLE (rescale_previous)" in text
    assert "Status:\nREADY" in text


def test_baseline_restoring_step_reports_rollback_not_required():
    report = dry_run(get_runbook("order-service-http-500"), order_incident(), now=NOW)
    clear = report.steps[0]
    assert clear.action_id == "clear_fault"
    assert clear.rollback_kind == "baseline"
    assert "NOT REQUIRED (restores baseline)" in render_summary(report)


# ─── no mutation, ever (§15) ─────────────────────────────────────────────────


def test_dry_run_dispatches_only_the_simulate_capability(recorded_capabilities):
    dry_run(_rb(), order_incident(), now=NOW)
    assert recorded_capabilities
    assert set(recorded_capabilities) == {"automation.runbook.simulate"}


def test_dry_run_of_a_blocked_plan_still_dispatches_nothing_mutating(recorded_capabilities):
    report = dry_run(_rb(), order_incident(incident_status="resolved"), now=NOW)
    assert report.status is DryRunStatus.BLOCKED
    assert "automation.runbook.apply" not in recorded_capabilities
    assert "automation.runbook.execute" not in recorded_capabilities


def test_simulation_failure_is_a_warning_not_a_block(monkeypatch):
    """A prediction service being down must not veto an approved recovery."""

    def _boom(step, service):
        raise RuntimeError("simulate provider exploded")

    report = dry_run(_rb(), order_incident(), simulate_call=_boom, now=NOW)
    assert report.status is DryRunStatus.READY
    assert all("dry-run preview unavailable" in " ".join(s.warnings) for s in report.steps)


# ─── blocking (§17) ──────────────────────────────────────────────────────────


def test_blocked_dry_run_reports_why():
    report = dry_run(_rb(status=RunbookStatus.DRAFT), order_incident(), now=NOW)
    assert report.status is DryRunStatus.BLOCKED
    assert any("status is 'draft'" in r for r in report.blocking_reasons)
    assert report.applicability_status.value == "BLOCKED"


def test_invalid_step_blocks_the_plan():
    rb = _rb(
        steps=[
            RunbookStep(name="bad", action="rm_minus_rf", destructive=True, target="deployment/x")
        ]
    )
    report = dry_run(rb, order_incident(), now=NOW)
    assert report.status is DryRunStatus.BLOCKED
    assert any("unknown action" in r for r in report.blocking_reasons)


def test_critical_risk_blocks_before_the_gate_is_consulted(recorded_capabilities):
    """§14 LEVEL 4: the executor declines to even ask for approval."""
    rb = _rb(
        steps=[
            RunbookStep(
                name="flush",
                action="flush_cache",
                destructive=True,
                rollback_action=None,
                target="deployment/order-service",
                namespace="ecommerce",
            )
        ]
    )
    report = dry_run(rb, order_incident(), now=NOW)
    assert report.risk_level is RiskLevel.CRITICAL
    assert report.status is DryRunStatus.BLOCKED
    assert any("CRITICAL" in r for r in report.blocking_reasons)
    assert "automation.runbook.execute" not in recorded_capabilities


def test_out_of_scope_step_blocks_the_plan():
    rb = _rb(
        steps=[
            RunbookStep(
                name="restart-other",
                action="restart_deployment",
                destructive=True,
                rollback_action="rescale_previous",
                target="deployment/payment-service",
                namespace="ecommerce",
            )
        ]
    )
    report = dry_run(rb, order_incident(), now=NOW)
    assert report.status is DryRunStatus.BLOCKED


# ─── risk / HITL correctness (§13) ───────────────────────────────────────────


def test_read_only_plan_is_low_risk_and_needs_no_human():
    rb = _rb(
        steps=[
            RunbookStep(
                name="verify",
                action="healthcheck",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            )
        ]
    )
    report = dry_run(rb, order_incident(), now=NOW)
    assert report.risk_level is RiskLevel.LOW
    assert report.hitl_required is False
    assert report.production_mutation is False


def test_non_production_environment_lowers_a_non_disruptive_step():
    rb = _rb(
        steps=[
            RunbookStep(
                name="drain",
                action="drain",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            )
        ],
        applicability=ApplicabilityScope(
            allowed_services=["order-service"], allowed_namespaces=["ecommerce"]
        ),
    )
    prod = dry_run(rb, order_incident(environment="production"), now=NOW)
    demo = dry_run(rb, order_incident(environment="demo"), now=NOW)
    assert prod.risk_level is RiskLevel.MEDIUM
    assert demo.risk_level is RiskLevel.LOW


def test_risk_threshold_env_var_can_demand_a_human_earlier(monkeypatch):
    rb = _rb(
        steps=[
            RunbookStep(
                name="drain",
                action="drain",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            )
        ]
    )
    assert dry_run(rb, order_incident(), now=NOW).hitl_required is False
    monkeypatch.setenv("AIOPS_RUNBOOK_HITL_RISK_THRESHOLD", "MEDIUM")
    assert dry_run(rb, order_incident(), now=NOW).hitl_required is True


# ─── plan hash (§20's identity) ──────────────────────────────────────────────


def test_plan_hash_is_stable_for_the_same_plan():
    a = dry_run(_rb(), order_incident(), now=NOW)
    b = dry_run(_rb(), order_incident(), now=NOW)
    assert a.plan_hash == b.plan_hash


def test_plan_hash_changes_when_the_plan_changes():
    base = dry_run(_rb(), order_incident(), now=NOW)
    reordered = _rb()
    reordered.steps = list(reversed(reordered.steps))
    assert dry_run(reordered, order_incident(), now=NOW).plan_hash != base.plan_hash

    retargeted = _rb()
    retargeted.steps[1].params = {"timeout_seconds": 30}
    assert dry_run(retargeted, order_incident(), now=NOW).plan_hash != base.plan_hash


def test_plan_hash_ignores_incident_details():
    """The hash identifies the *plan*; the incident is keyed separately."""
    a = dry_run(_rb(), order_incident(incident_id="INC-1"), now=NOW)
    b = dry_run(_rb(), order_incident(incident_id="INC-2"), now=NOW)
    assert a.plan_hash == b.plan_hash
    assert plan_hash(_rb(), a.steps) == a.plan_hash


# ─── step identity is positional, not name-keyed (a reproduced defect) ───────


def _two_same_named_steps(first_action: str = "healthcheck") -> ExecutableRunbook:
    """A runbook with two steps sharing a name — which two shipped runbooks did."""
    return ExecutableRunbook(
        id="rb-dupname",
        title="duplicate step names",
        service="order-service",
        status=RunbookStatus.ACTIVE,
        approved_by="test",
        steps=[
            RunbookStep(
                name="verify-health",
                action=first_action,
                destructive=first_action != "healthcheck",
                rollback_action="undrain" if first_action == "drain" else None,
                target="deployment/order-service",
                namespace="ecommerce",
                params={"timeout_seconds": 5} if first_action == "healthcheck" else {},
            ),
            RunbookStep(
                name="verify-health",
                action="healthcheck",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
                params={"timeout_seconds": 599},
            ),
        ],
        applicability=ApplicabilityScope(
            environments=["production"],
            allowed_services=["order-service"],
            allowed_namespaces=["ecommerce"],
        ),
        prerequisites=[Prerequisite(id="incident_active", check="incident_active")],
    )


def test_plan_hash_sees_an_edit_to_the_first_of_two_same_named_steps():
    """The digest must change when the plan does.

    It did not: the per-step hash entry took its action and parameters from a
    name-keyed lookup, so both duplicates hashed as the LAST one. Changing the first
    step from a read-only healthcheck to a mutating drain left the digest identical —
    an approval granted for "no production change" could then be replayed against a
    plan that drains traffic.
    """
    read_only = dry_run(_two_same_named_steps("healthcheck"), order_incident(), now=NOW)
    mutating = dry_run(_two_same_named_steps("drain"), order_incident(), now=NOW)
    assert read_only.plan_hash != mutating.plan_hash


def test_each_of_two_same_named_steps_is_planned_separately():
    report = dry_run(_two_same_named_steps("healthcheck"), order_incident(), now=NOW)
    assert [v.index for v in report.steps] == [1, 2]
    # Each step keeps its OWN parameters rather than inheriting the other's.
    assert report.steps[0].parameters == {"timeout_seconds": 5}
    assert report.steps[1].parameters == {"timeout_seconds": 599}


def test_a_runbook_with_duplicate_step_names_cannot_execute():
    """Names are the operator-facing key for overrides and approvals, so a collision is
    ambiguous — refused rather than silently interpreted."""
    runbook = _two_same_named_steps("healthcheck")
    assert runbook.duplicate_step_names == ["verify-health"]
    assert runbook.is_executable is False
    report = dry_run(runbook, order_incident(), now=NOW)
    assert report.status is DryRunStatus.BLOCKED
    assert any("appear more than once" in r for r in report.blocking_reasons)


def test_no_shipped_runbook_has_duplicate_step_names():
    """Two of them did: order-service-payment-timeout and
    payment-service-gateway-timeout each declared ``verify-health`` twice."""
    from agents.runbook_executor import load_runbooks

    offenders = {
        rb.id: rb.duplicate_step_names for rb in load_runbooks() if rb.duplicate_step_names
    }
    assert offenders == {}
