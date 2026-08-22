"""Applicability, prerequisites and runbook lifecycle gating for RA-004 (§8–§10).

All pure: no registry, no cluster, no clock beyond the ``now`` passed in. The library
assertions double as a guard on the *generated* runbooks — if
``scripts/generate_runbooks.py`` ever emits a runbook whose declared alert does not
exist as a real Prometheus rule, or whose scope does not cover its own steps, these
fail rather than the mismatch surfacing as a mis-selected runbook during a demo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from agents.runbook_executor import (
    ApplicabilityScope,
    ExecutableRunbook,
    Prerequisite,
    RunbookStatus,
    RunbookStep,
    load_runbooks,
)
from agents.runbook_executor.applicability import (
    ApplicabilityStatus,
    FacetVerdict,
    PrerequisiteStatus,
    evaluate,
)
from tests.test_runbook_matching import NOW, order_incident

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _rb(**overrides) -> ExecutableRunbook:
    payload = {
        "id": "rb-test",
        "title": "test",
        "service": "order-service",
        "status": RunbookStatus.ACTIVE,
        "approved_by": "test",
        "steps": [
            RunbookStep(
                name="verify",
                action="healthcheck",
                destructive=False,
                target="deployment/order-service",
                namespace="ecommerce",
            )
        ],
        "applicability": ApplicabilityScope(
            environments=["production"],
            failure_category="application_error",
            alerts=["EcommerceOrderErrorRateHigh"],
            required_signals=["error_rate_high"],
            allowed_services=["order-service"],
            allowed_namespaces=["ecommerce"],
        ),
        "prerequisites": [
            Prerequisite(id="incident_active", check="incident_active"),
            Prerequisite(id="target_in_scope", check="service_scope"),
        ],
    }
    payload.update(overrides)
    return ExecutableRunbook(**payload)


# ─── the happy path ──────────────────────────────────────────────────────────


def test_fully_matching_incident_is_applicable():
    result = evaluate(_rb(), order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.APPLICABLE
    assert result.blocking_reasons == []
    assert result.missing_prerequisites == []


# ─── disqualifying mismatches (§8) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("override", "facet"),
    [
        ({"service": "payment-service"}, "service"),
        ({"environment": "staging"}, "environment"),
        ({"failure_category": "resource_saturation_memory"}, "failure_category"),
        ({"alert_name": "EcommerceMySQLDown"}, "alert"),
    ],
)
def test_contradicted_facet_is_not_applicable(override, facet):
    result = evaluate(_rb(), order_incident(**override), now=NOW)
    assert result.status is ApplicabilityStatus.NOT_APPLICABLE
    assert result.facet(facet).verdict is FacetVerdict.MISMATCH


def test_unknown_facet_warns_but_does_not_disqualify():
    """Not knowing the environment is not the same as being in the wrong one."""
    result = evaluate(_rb(), order_incident(environment=""), now=NOW)
    assert result.status is ApplicabilityStatus.APPLICABLE
    assert result.facet("environment").verdict is FacetVerdict.UNKNOWN
    assert any("environment" in w for w in result.warnings)


def test_missing_signal_warns_but_does_not_disqualify():
    """Signals are keyword-derived and sparse: absence is 'not observed', not 'wrong'."""
    result = evaluate(_rb(), order_incident(observed_signals=["cpu_saturation"]), now=NOW)
    assert result.status is ApplicabilityStatus.APPLICABLE
    assert result.facet("required_signals").verdict is FacetVerdict.MISMATCH
    assert any("error_rate_high" in w for w in result.warnings)


def test_declared_severity_gate_is_honoured():
    rb = _rb(
        applicability=ApplicabilityScope(severities=["sev1"], allowed_namespaces=["ecommerce"])
    )
    assert evaluate(rb, order_incident(severity="Sev-2"), now=NOW).status is (
        ApplicabilityStatus.NOT_APPLICABLE
    )
    assert evaluate(rb, order_incident(severity="Sev-1"), now=NOW).status is (
        ApplicabilityStatus.APPLICABLE
    )


# ─── lifecycle gating (§9/§10) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        RunbookStatus.DRAFT,
        RunbookStatus.PENDING_REVIEW,
        RunbookStatus.APPROVED,  # approved but not activated
        RunbookStatus.SUPERSEDED,
        RunbookStatus.ARCHIVED,
        RunbookStatus.REJECTED,
    ],
)
def test_only_active_runbooks_may_execute(status):
    result = evaluate(_rb(status=status), order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    assert any(status.value in r for r in result.blocking_reasons)


def test_active_without_an_approver_is_refused():
    """ACTIVE + APPROVED means both halves; neither is satisfied by omission."""
    result = evaluate(_rb(approved_by=""), order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    assert any("approved_by" in r for r in result.blocking_reasons)


def test_runbook_with_no_steps_is_not_executable():
    result = evaluate(_rb(steps=[]), order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    assert any("no executable steps" in r for r in result.blocking_reasons)


def test_unknown_status_string_degrades_to_draft(tmp_path):
    """A typo'd status must show up as refused, not vanish from the library."""
    (tmp_path / "typo.md").write_text(
        "---\ntitle: t\nservice: order-service\nstatus: activ\napproved_by: x\n"
        "steps:\n- name: verify\n  action: healthcheck\n  destructive: false\n"
        "  target: deployment/order-service\n  namespace: ecommerce\n---\nbody\n",
        encoding="utf-8",
    )
    library = load_runbooks(tmp_path)
    assert [rb.id for rb in library] == ["typo"]
    assert library[0].status is RunbookStatus.DRAFT
    assert library[0].is_executable is False


# ─── prerequisites (§8/§24) ──────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["resolved", "closed", "cancelled", "superseded"])
def test_closed_incident_blocks(state):
    result = evaluate(_rb(), order_incident(incident_status=state), now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    prereq = next(p for p in result.prerequisites if p.id == "incident_active")
    assert prereq.status is PrerequisiteStatus.FAILED


def test_suppressed_incident_is_still_active():
    """A Suppressed verdict is a *deduplicated* alert in this codebase, not a resolved
    one — blocking it would refuse remediation for every duplicate of a live incident."""
    result = evaluate(_rb(), order_incident(incident_status="suppressed"), now=NOW)
    assert result.status is ApplicabilityStatus.APPLICABLE


def test_aged_out_incident_blocks():
    ctx = order_incident(detected_at=NOW - timedelta(hours=9), max_incident_age_minutes=60)
    result = evaluate(_rb(), ctx, now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    assert any("past the 60 min limit" in r for r in result.blocking_reasons)


def test_max_age_reads_the_env_var_per_call(monkeypatch):
    ctx = order_incident(detected_at=NOW - timedelta(minutes=90))
    assert evaluate(_rb(), ctx, now=NOW).status is ApplicabilityStatus.APPLICABLE
    monkeypatch.setenv("AIOPS_RUNBOOK_MAX_INCIDENT_AGE_MINUTES", "30")
    assert evaluate(_rb(), ctx, now=NOW).status is ApplicabilityStatus.BLOCKED


def test_alert_no_longer_firing_is_advisory_by_default():
    rb = _rb(
        prerequisites=[
            Prerequisite(id="incident_active", check="incident_active"),
            Prerequisite(id="alert_firing", check="alert_firing", mandatory=False),
        ]
    )
    result = evaluate(rb, order_incident(alert_firing=False), now=NOW)
    assert result.status is ApplicabilityStatus.APPLICABLE
    assert "alert_firing" in result.missing_prerequisites
    assert any("no longer firing" in w for w in result.warnings)


def test_alert_firing_can_be_made_mandatory():
    rb = _rb(prerequisites=[Prerequisite(id="alert_firing", check="alert_firing", mandatory=True)])
    assert evaluate(rb, order_incident(alert_firing=False), now=NOW).status is (
        ApplicabilityStatus.BLOCKED
    )


def test_unprobed_alert_is_skipped_not_failed():
    """A missing data source must not read as a failed check."""
    rb = _rb(prerequisites=[Prerequisite(id="alert_firing", check="alert_firing", mandatory=True)])
    result = evaluate(rb, order_incident(alert_firing=None), now=NOW)
    prereq = next(p for p in result.prerequisites if p.id == "alert_firing")
    assert prereq.status is PrerequisiteStatus.SKIPPED
    assert result.status is ApplicabilityStatus.APPLICABLE


def test_unknown_check_on_a_mandatory_prerequisite_is_unknown_not_satisfied():
    rb = _rb(prerequisites=[Prerequisite(id="ask", check="consult_the_oracle", mandatory=True)])
    result = evaluate(rb, order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.UNKNOWN


def test_out_of_scope_step_fails_the_scope_prerequisite():
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
    result = evaluate(rb, order_incident(), now=NOW)
    assert result.status is ApplicabilityStatus.BLOCKED
    assert any("outside this runbook" in r for r in result.blocking_reasons)


# ─── the shipped library keeps its declarations honest ───────────────────────


def test_every_shipped_runbook_is_active_approved_and_versioned():
    for rb in load_runbooks():
        assert rb.status is RunbookStatus.ACTIVE, rb.id
        assert rb.approved_by, rb.id
        assert rb.version >= 1, rb.id
        assert rb.owner, rb.id


def test_every_shipped_runbook_declares_scope_covering_its_own_steps():
    """A generated runbook cannot ship with steps outside the scope it declares."""
    from agents.runbook_executor.actions import target_in_scope

    for rb in load_runbooks():
        for step in rb.steps:
            ok, reason = target_in_scope(step, rb)
            assert ok, f"{rb.id}: {reason}"


def test_declared_alerts_exist_as_real_prometheus_rules():
    """The alert a runbook matches on must be a rule that can actually fire.

    Three runbooks previously named an alert their fault does not raise (high-CPU
    pointing at an order-service latency rule, the memory leak pointing at a rule that
    only fires after the OOMKill). Now that the alert name is a *matching* input, a
    stale one silently mis-ranks candidates — so it is asserted, not reviewed.
    """
    values = yaml.safe_load(
        (REPO_ROOT / "infra" / "observability" / "prometheus-values.yaml").read_text(
            encoding="utf-8"
        )
    )
    rules = set()

    def _walk(node):
        if isinstance(node, dict):
            if "alert" in node and isinstance(node["alert"], str):
                rules.add(node["alert"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str) and "alert:" in node:
            for line in node.splitlines():
                stripped = line.strip()
                if stripped.startswith("- alert:"):
                    rules.add(stripped.split(":", 1)[1].strip())

    _walk(values)
    assert rules, "no alert rules found in prometheus-values.yaml — parser needs updating"
    for rb in load_runbooks():
        for alert in rb.applicability.alerts:
            assert alert in rules, f"{rb.id} declares alert {alert!r}, which is not a real rule"


def test_declared_alerts_agree_with_the_scenario_mapping():
    """The runbook's alert must be the one the *scenario* raises for its fault key."""
    from demo.ui.scenario_provider import ALERTNAMES

    # A LIST per fault, not one runbook: a fault legitimately has several runbooks
    # (alternative remediation strategies for the same cause — release-only versus
    # release-then-recycle). Keyed by fault into a single slot, every runbook but the
    # last silently escaped this assertion.
    by_fault: dict[str, list] = {}
    for rb in load_runbooks():
        for step in rb.steps:
            if step.action == "clear_fault" and (step.target or "").startswith("fault/"):
                by_fault.setdefault(step.target.split("/", 1)[1], []).append(rb)
    assert by_fault, "no fault-clearing runbooks found"
    for fault_key, runbooks in by_fault.items():
        scenario_id = fault_key.replace(".", "_")
        expected = ALERTNAMES.get(scenario_id)
        if expected is None:
            continue
        for rb in runbooks:
            # The primary alert must be first; a runbook may declare additional
            # `also_alerts` after it for a fault that legitimately cross-fires
            # under more than one alert (see RB.also_alerts).
            assert rb.applicability.alerts[:1] == [expected], (
                f"{rb.id} declares {rb.applicability.alerts} but scenario {scenario_id} "
                f"raises {expected!r} first"
            )


def test_detected_at_naive_datetime_is_treated_as_utc():
    """A naive timestamp from JSON must not crash the age comparison."""
    ctx = order_incident(detected_at=datetime(2026, 8, 20, 11, 58))
    result = evaluate(_rb(), ctx, now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    assert result.status is ApplicabilityStatus.APPLICABLE
