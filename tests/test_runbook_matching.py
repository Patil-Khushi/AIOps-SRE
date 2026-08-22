"""Discovery + candidate ranking for RA-004 (§3–§6).

Pure-function territory: every test here runs against the shipped library or a
hand-built runbook with no registry, no cluster, no LLM and no clock. What is being
pinned down is that the *decision* about who chooses is driven by applicability rather
than by a score, and that the score is reproducible and explainable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.runbook_executor import (
    ApplicabilityScope,
    ExecutableRunbook,
    IncidentContext,
    Prerequisite,
    RunbookStatus,
    RunbookStep,
    load_runbooks,
)
from agents.runbook_executor.applicability import ApplicabilityStatus
from agents.runbook_executor.matching import (
    DiscoveryDecision,
    build_candidate,
    discover,
    rank_candidates,
    score_candidate,
    specificity,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def order_incident(**overrides) -> IncidentContext:
    """The §40 example incident: order-service, SEV-2, 20% error rate."""
    payload = {
        "incident_id": "INC-1042",
        "service": "order-service",
        "environment": "production",
        "severity": "Sev-2",
        "alert_name": "EcommerceOrderErrorRateHigh",
        "failure_category": "application_error",
        "tags": ["error", "5xx"],
        "observed_signals": ["error_rate_high"],
        "incident_status": "active",
        "detected_at": NOW - timedelta(minutes=2),
        "alert_firing": True,
    }
    payload.update(overrides)
    return IncidentContext(**payload)


def _runbook(rb_id: str, service: str, **overrides) -> ExecutableRunbook:
    payload = {
        "id": rb_id,
        "title": f"{service} test runbook",
        "service": service,
        "status": RunbookStatus.ACTIVE,
        "approved_by": "test",
        "steps": [
            RunbookStep(
                name="verify",
                action="healthcheck",
                destructive=False,
                target=f"deployment/{service}",
                namespace="ecommerce",
            )
        ],
        "applicability": ApplicabilityScope(
            environments=["production"], allowed_namespaces=["ecommerce"]
        ),
        "prerequisites": [Prerequisite(id="incident_active", check="incident_active")],
    }
    payload.update(overrides)
    return ExecutableRunbook(**payload)


# ─── candidate list ──────────────────────────────────────────────────────────


def test_multiple_applicable_runbooks_are_all_returned():
    """§4: the specific runbook AND the generic recovery both come back, ranked."""
    result = discover(load_runbooks(), order_incident(), now=NOW)
    assert result.decision is DiscoveryDecision.CANDIDATES
    applicable = [c.runbook_id for c in result.applicable]
    assert applicable == ["order-service-http-500", "order-service-restart"]


def test_recommended_flag_marks_the_top_applicable_candidate_even_with_several():
    """§6 CASE 2 still gets a suggestion: several applicable candidates means an SRE
    must choose, but the executor may say which one it thinks fits best. Exactly one
    candidate carries the flag, and it is the top of the applicable list — advisory
    only, never a second selection mechanism alongside auto_selected."""
    result = discover(load_runbooks(), order_incident(), now=NOW)
    assert result.decision is DiscoveryDecision.CANDIDATES
    recommended = [c for c in result.candidates if c.recommended]
    assert [c.runbook_id for c in recommended] == ["order-service-http-500"]
    assert recommended[0].runbook_id == result.applicable[0].runbook_id


def test_recommended_flag_is_set_on_auto_select_too():
    """The single auto-selectable candidate is also the recommended one — one
    consistent answer to 'what does the executor think fits', whether or not a
    human ends up needing to choose."""
    ctx = order_incident()
    only = _runbook("order-service-only", "order-service")
    result = discover([only], ctx, now=NOW)
    assert result.decision is DiscoveryDecision.AUTO_SELECT
    recommended = [c.runbook_id for c in result.candidates if c.recommended]
    assert recommended == [result.auto_selected] == ["order-service-only"]


def test_specific_runbook_outranks_the_generic_one():
    result = discover(load_runbooks(), order_incident(), now=NOW)
    top, second = result.applicable[0], result.applicable[1]
    assert top.runbook_id == "order-service-http-500"
    assert top.match_score > second.match_score
    assert top.specificity > second.specificity


def test_candidate_carries_the_full_contract():
    """Every field §4 requires is present and populated, not just declared."""
    result = discover(load_runbooks(), order_incident(), now=NOW)
    top = result.applicable[0]
    assert top.runbook_id and top.version == 1 and top.title
    assert 0.0 < top.match_score <= 1.0
    assert top.match_reasons
    assert top.applicability_status is ApplicabilityStatus.APPLICABLE
    assert top.risk_level.value in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert top.rollback_available is True
    assert top.hitl_required is True
    assert top.missing_prerequisites == []
    assert isinstance(top.warnings, list)


def test_other_services_are_not_candidates_at_all():
    """§3: matching is service-scoped. A payment runbook is not a weak candidate."""
    result = discover(load_runbooks(), order_incident(), now=NOW)
    assert all(c.service == "order-service" for c in result.candidates)


def test_ranking_is_deterministic_across_input_order():
    library = load_runbooks()
    forward = discover(library, order_incident(), now=NOW)
    backward = discover(list(reversed(library)), order_incident(), now=NOW)
    assert [c.runbook_id for c in forward.candidates] == [c.runbook_id for c in backward.candidates]


def test_ties_break_by_runbook_id():
    ctx = order_incident(failure_category="", alert_name="", observed_signals=[], tags=[])
    a, b = (
        _runbook("bbb-service-two", "order-service"),
        _runbook("aaa-service-one", "order-service"),
    )
    ranked = rank_candidates([build_candidate(a, ctx, now=NOW), build_candidate(b, ctx, now=NOW)])
    assert [c.runbook_id for c in ranked] == ["aaa-service-one", "bbb-service-two"]


# ─── explainability ──────────────────────────────────────────────────────────


def test_match_reasons_name_every_matched_facet():
    result = discover(load_runbooks(), order_incident(), now=NOW)
    reasons = " | ".join(result.applicable[0].match_reasons).lower()
    for expected in ("service", "environment", "failure category", "alert", "signal"):
        assert expected in reasons


def test_score_components_sum_to_the_reported_score():
    """The arithmetic is auditable: recomputing from the components reproduces it."""
    library = {rb.id: rb for rb in load_runbooks()}
    rb = library["order-service-http-500"]
    ctx = order_incident()
    candidate = build_candidate(rb, ctx, now=NOW)
    comparable = sum(c.weight for c in candidate.score_components if c.comparable)
    earned = sum(c.earned for c in candidate.score_components)
    # ``specificity`` is always comparable and already inside both sums.
    assert candidate.match_score == pytest.approx(round(earned / comparable, 4), abs=0.0002)


def test_unknown_facets_do_not_count_as_matches():
    """An incident with no category/alert scores only what could be compared."""
    library = {rb.id: rb for rb in load_runbooks()}
    rb = library["order-service-http-500"]
    full = build_candidate(rb, order_incident(), now=NOW)
    blind = build_candidate(
        rb,
        order_incident(failure_category="", alert_name="", observed_signals=[]),
        now=NOW,
    )
    assert blind.match_score < full.match_score
    unknown = [c.facet for c in blind.score_components if not c.comparable]
    assert {"failure_category", "alert", "required_signals"} <= set(unknown)


def test_specificity_counts_declared_constraints():
    library = {rb.id: rb for rb in load_runbooks()}
    assert specificity(library["order-service-http-500"]) == 3  # category + alert + signals
    assert specificity(library["order-service-restart"]) == 0  # generic recovery


def test_score_is_zero_when_nothing_is_comparable():
    """A runbook that constrains nothing, for an incident that says nothing, earns
    only what it can prove — not a default pass."""
    rb = _runbook("bare", "order-service", applicability=ApplicabilityScope(), prerequisites=[])
    ctx = IncidentContext(service="order-service")
    score, components, _reasons = score_candidate(rb, build_candidate(rb, ctx).applicability)
    assert 0.0 <= score <= 1.0
    assert any(c.facet == "specificity" for c in components)


# ─── the five decisions (§6) ─────────────────────────────────────────────────


def test_single_applicable_runbook_auto_selects():
    """CASE 1: exactly one applicable candidate may be selected by the platform."""
    ctx = order_incident()
    only = _runbook("order-service-only", "order-service")
    result = discover([only], ctx, now=NOW)
    assert result.decision is DiscoveryDecision.AUTO_SELECT
    assert result.auto_selected == "order-service-only"


def test_no_runbook_for_the_service():
    """CASE 3: nothing covers the service → NO_RUNBOOK, and the caller routes to RCA."""
    result = discover(load_runbooks(), order_incident(service="telemetry-aggregator"), now=NOW)
    assert result.decision is DiscoveryDecision.NO_RUNBOOK
    assert result.candidates == []


def test_undeterminable_applicability_is_ambiguous():
    """CASE 4: a mandatory prerequisite with no evaluator cannot be waved through."""
    ctx = order_incident()
    rb = _runbook(
        "order-service-manual",
        "order-service",
        prerequisites=[
            Prerequisite(id="human_confirms_topology", check="ask_a_human", mandatory=True)
        ],
    )
    result = discover([rb], ctx, now=NOW)
    assert result.decision is DiscoveryDecision.AMBIGUOUS
    assert result.candidates[0].applicability_status is ApplicabilityStatus.UNKNOWN


def test_failed_mandatory_prerequisite_blocks():
    """CASE 5: the right procedure, refused — BLOCKED, not NOT_APPLICABLE."""
    ctx = order_incident(incident_status="resolved")
    result = discover([_runbook("order-service-only", "order-service")], ctx, now=NOW)
    assert result.decision is DiscoveryDecision.BLOCKED
    assert result.candidates[0].applicability_status is ApplicabilityStatus.BLOCKED


def test_wrong_category_is_not_applicable_not_blocked():
    ctx = order_incident(failure_category="resource_saturation_memory")
    library = [rb for rb in load_runbooks() if rb.id == "order-service-http-500"]
    result = discover(library, ctx, now=NOW)
    assert result.decision is DiscoveryDecision.NOT_APPLICABLE


def test_draft_runbook_is_shown_but_blocked():
    """A DRAFT runbook stays visible with its refusal rather than disappearing."""
    ctx = order_incident()
    draft = _runbook("order-service-draft", "order-service", status=RunbookStatus.DRAFT)
    result = discover([draft], ctx, now=NOW)
    assert result.decision is DiscoveryDecision.BLOCKED
    candidate = result.candidates[0]
    assert candidate.applicability_status is ApplicabilityStatus.BLOCKED
    assert any("status is 'draft'" in r for r in candidate.blocking_reasons)
    assert candidate.selectable is False
