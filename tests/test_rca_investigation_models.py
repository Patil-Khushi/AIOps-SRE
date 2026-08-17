"""Contracts on the RCA investigation data model.

These are the invariants later phases will be written against, so they are pinned
before any logic depends on them. The ones that matter most are the distinctions the
brief is explicit about and that are easy to erode by "simplifying" an enum:
unavailable evidence is not negative evidence, an unverified prediction is not
knowledge, and a service nobody looked at is not a healthy service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from agents.rca_agent.investigation import models as m

# ─── negative evidence ──────────────────────────────────────────────────────


def test_evidence_stance_partitions_evidence_from_gaps():
    """Every stance is either evidence or a gap, and never both.

    A total, disjoint partition is what lets the scoring stage ask one question
    (``is_evidence``) instead of enumerating members — and a stance added later
    without a decision about which side it falls on fails here rather than silently
    becoming a gap.
    """
    for stance in m.EvidenceStance:
        assert stance.is_evidence != stance.is_gap, f"{stance} is neither or both"


def test_checked_absent_is_evidence_but_unavailable_is_not():
    """The central negative-evidence rule.

    "We looked and CPU was 35%" argues against a CPU cause. "We could not read CPU"
    argues nothing. Collapsing them lets an agent rule out a cause it never checked,
    which is the failure mode the whole distinction exists to prevent.
    """
    assert m.EvidenceStance.CHECKED_ABSENT.is_evidence
    assert not m.EvidenceStance.UNAVAILABLE.is_evidence
    assert not m.EvidenceStance.NOT_REQUESTED.is_evidence
    assert not m.EvidenceStance.FAILED.is_evidence


def test_gap_states_are_all_distinct():
    """``UNAVAILABLE`` / ``NOT_REQUESTED`` / ``FAILED`` stay separate values.

    They mean "could not ask", "chose not to ask" and "asked and it broke". Only the
    last is a defect worth alerting on, and merging any two would lose that.
    """
    gaps = {m.EvidenceStance.UNAVAILABLE, m.EvidenceStance.NOT_REQUESTED, m.EvidenceStance.FAILED}
    assert len({g.value for g in gaps}) == 3


def test_evidence_matrix_keeps_absence_and_gaps_in_separate_lists():
    absent = m.EvidenceItem(
        evidence_id="e1", stance=m.EvidenceStance.CHECKED_ABSENT, statement="cpu 0.05 cores"
    )
    gap = m.EvidenceItem(
        evidence_id="e2", stance=m.EvidenceStance.UNAVAILABLE, statement="no traces provider"
    )
    matrix = m.EvidenceMatrix(
        hypothesis=m.Hypothesis(hypothesis_id="h1", label="cpu saturation", mechanism="throttling"),
        checked_absent=(absent,),
        gaps=(gap,),
    )
    assert matrix.has_unresolved_gap
    assert absent not in matrix.gaps
    assert gap not in matrix.checked_absent


def test_corroboration_requires_more_than_one_source():
    """Matches the correlator's own-source-inclusive ``sources_agreeing`` convention.

    ``aiops/context/correlator.py`` includes the observation's own source in the
    tuple, so the cross-source test is ``> 1``. A ``>= 1`` reading here would mark
    every single-source observation as corroborated.
    """
    one = m.EvidenceItem(
        evidence_id="e", stance=m.EvidenceStance.SUPPORTS, statement="x", sources_agreeing=("logs",)
    )
    two = one.model_copy(update={"sources_agreeing": ("logs", "metrics")})
    assert not one.is_corroborated
    assert two.is_corroborated


# ─── uncertainty ────────────────────────────────────────────────────────────


def test_only_settled_statuses_are_actionable():
    """A one-click fix is offered only for a conclusion the evidence supports."""
    assert m.RootCauseStatus.CONFIRMED.is_actionable
    assert m.RootCauseStatus.PROBABLE.is_actionable
    assert not m.RootCauseStatus.UNCERTAIN.is_actionable
    assert not m.RootCauseStatus.INSUFFICIENT_EVIDENCE.is_actionable


def test_uncertain_and_insufficient_are_different_answers():
    """ "We looked and cannot separate the candidates" is not "we could not look"."""
    assert m.RootCauseStatus.UNCERTAIN != m.RootCauseStatus.INSUFFICIENT_EVIDENCE


# ─── memory lifecycle ───────────────────────────────────────────────────────


def test_only_verified_and_trusted_memory_can_rank():
    """A prediction must never become a prior.

    If ``NEW``/``UNVERIFIED`` entries could rank, the agent's own mistake becomes the
    prior that reproduces it — confidence compounding on nothing. ``SUPERSEDED`` and
    ``INVALIDATED`` are excluded too: they are retained for audit, not for reasoning.
    """
    usable = {s for s in m.MemoryStatus if s.usable_for_ranking}
    assert usable == {m.MemoryStatus.VERIFIED, m.MemoryStatus.TRUSTED}


def test_historical_prior_is_ineligible_until_verified():
    prior = m.HistoricalPrior(memory_id="mem1", status=m.MemoryStatus.UNVERIFIED, similarity=0.99)
    assert not prior.eligible
    assert prior.model_copy(update={"status": m.MemoryStatus.VERIFIED}).eligible


def test_historical_prior_cannot_express_a_claim_about_the_current_incident():
    """The field is ``recorded_cause``, and there is no ``root_cause``.

    Same discipline as ``aiops.tools.incident_history.ResolutionMetadata``: history
    carries what happened last time and has nowhere to put a verdict on now, so a
    consumer cannot mistake one for the other.
    """
    assert "recorded_cause" in m.HistoricalPrior.model_fields
    assert "root_cause" not in m.HistoricalPrior.model_fields


def test_success_rate_is_none_with_no_history_not_zero():
    """An ungraded pattern is unproven, not a total failure.

    ``0.0`` would bury a first-ever correct recall beneath patterns that merely have
    a longer track record of being wrong.
    """
    assert m.MemoryReliability().success_rate is None
    assert m.MemoryReliability(occurrences=17, verified_correct=15).success_rate == pytest.approx(
        0.8824, abs=1e-4
    )


def test_unverified_outcome_is_not_eligible_for_memory_even_at_full_confidence():
    """Confidence does not substitute for verification."""
    outcome = m.RCAOutcome(
        incident_id="INC1",
        affected_service="order-service",
        recorded_at=datetime.now(UTC),
        predicted_root_cause="PostgreSQL unavailable",
        predicted_status=m.RootCauseStatus.CONFIRMED,
        confidence=1.0,
    )
    assert outcome.verification_result == "not_run"
    assert not outcome.eligible_for_memory


def test_verified_outcome_is_eligible_and_human_correction_also_qualifies():
    """A corrected wrong prediction still teaches something.

    It is the one path by which a failure becomes knowledge, so it has to be eligible
    even though the prediction itself was wrong.
    """
    base = dict(
        incident_id="INC1",
        affected_service="order-service",
        recorded_at=datetime.now(UTC),
        predicted_root_cause="CPU saturation",
        predicted_status=m.RootCauseStatus.PROBABLE,
        confidence=0.6,
    )
    assert m.RCAOutcome(**base, verification_result="resolved").eligible_for_memory
    assert m.RCAOutcome(
        **base, human_corrected_root_cause="PostgreSQL unavailable"
    ).eligible_for_memory
    assert not m.RCAOutcome(**base, verification_result="not_resolved").eligible_for_memory


def test_outcome_preserves_the_original_prediction_alongside_a_correction():
    """A correction never overwrites what was predicted.

    Losing the prediction destroys the only record that the agent was wrong, which is
    exactly the data calibration is measured from.
    """
    outcome = m.RCAOutcome(
        incident_id="INC1",
        affected_service="user-service",
        recorded_at=datetime.now(UTC),
        predicted_root_cause="CPU saturation",
        predicted_status=m.RootCauseStatus.PROBABLE,
        confidence=0.7,
        human_corrected_root_cause="MySQL connection pool exhausted",
    )
    assert outcome.predicted_root_cause == "CPU saturation"
    assert outcome.human_corrected_root_cause == "MySQL connection pool exhausted"


# ─── blast radius ───────────────────────────────────────────────────────────


def test_not_observed_is_never_reported_as_healthy():
    """The blast-radius form of the negative-evidence rule.

    Claiming a service is fine because nothing was collected about it is how an
    incident report understates its own scope.
    """
    report = m.BlastRadiusReport(
        impacts=(
            m.ServiceImpact(service="order-service", state=m.ImpactState.DIRECTLY_AFFECTED),
            m.ServiceImpact(service="user-service", state=m.ImpactState.OBSERVED_HEALTHY),
            m.ServiceImpact(service="notification-service", state=m.ImpactState.NOT_OBSERVED),
            m.ServiceImpact(service="mystery", state=m.ImpactState.UNKNOWN),
        )
    )
    assert report.observed_healthy == ("user-service",)
    assert report.not_observed == ("notification-service",)
    assert "notification-service" not in report.observed_healthy
    assert report.unknown == ("mystery",)


def test_blast_radius_records_whether_topology_was_available():
    """Without it, every ``UNKNOWN`` reads as a finding rather than a coverage gap."""
    assert m.BlastRadiusReport().topology_available is False


# ─── recovery risk ──────────────────────────────────────────────────────────


def test_unassessed_risk_questions_are_reported_not_assumed_clear():
    """Defaults are cautious: unknown risk is visible as unknown.

    The operator reads this to decide whether to approve, so an unasked question must
    not render as a cleared one.
    """
    risk = m.RiskAssessment()
    assert risk.level == "unknown"
    assert len(risk.unassessed) == 7
    assert "destroys_evidence" in risk.unassessed
    assert not risk.reversible and not risk.rollback_available


def test_assessed_risk_leaves_nothing_unassessed():
    risk = m.RiskAssessment(
        level="low",
        causes_downtime=False,
        interrupts_active_requests=False,
        risks_data_loss=False,
        risks_duplicate_transactions=False,
        affects_downstream=False,
        affects_upstream=False,
        destroys_evidence=False,
        reversible=True,
        rollback_available=True,
    )
    assert risk.unassessed == ()


def test_recovery_option_cannot_be_deserialised_without_hitl():
    """``Literal[True]`` makes the invariant uncircumventable.

    Same defence ``RankedFixStep`` uses: an LLM or a stored payload claiming
    ``requires_hitl: false`` is rejected by pydantic before any caller sees it.
    """
    with pytest.raises(ValidationError):
        m.RecoveryOption.model_validate(
            {"option_id": "o1", "description": "restart", "requires_hitl": False}
        )


def test_grounded_and_executable_are_independent():
    """A real action key with no wired executor is still a manual step."""
    option = m.RecoveryOption(
        option_id="o1", description="roll back the deploy", grounded=True, executable=False
    )
    assert option.grounded and not option.executable


# ─── budget ─────────────────────────────────────────────────────────────────


def test_budget_starts_unexhausted_and_allows_one_query():
    budget = m.InvestigationBudget()
    assert not budget.exhausted
    assert budget.may_query("promql:up")


def test_budget_refuses_a_repeat_query():
    """ "Do not repeatedly query the same source" is structural, not aspirational."""
    budget = m.InvestigationBudget()
    budget.record_query("promql:up")
    assert not budget.may_query("promql:up")
    assert budget.may_query("promql:cpu")


def test_budget_exhausts_on_each_limit():
    for field, limit in (
        ("iterations", "max_iterations"),
        ("additional_queries", "max_additional_queries"),
        ("llm_calls", "max_llm_calls"),
        ("elapsed_seconds", "max_duration_seconds"),
    ):
        budget = m.InvestigationBudget()
        setattr(budget, field, getattr(budget, limit))
        assert budget.exhausted, f"{field} at its limit did not exhaust the budget"


def test_explicit_exhaustion_records_a_reason():
    """The reason has to reach the decision trace.

    "Ran out of time" and "the evidence was mute" both end in abstention, and an
    operator deciding what to do next needs to know which one happened.
    """
    budget = m.InvestigationBudget()
    budget.exhaust("no discriminating evidence after 3 iterations")
    assert budget.exhausted
    assert "3 iterations" in (budget.exhaustion_reason or "")


def test_default_budget_allows_exactly_one_llm_call():
    """One LLM call on the normal path is the design commitment, not a coincidence."""
    assert m.InvestigationBudget().max_llm_calls == 1


# ─── immutability discipline ────────────────────────────────────────────────


def _investigation_models():
    return [
        obj
        for obj in vars(m).values()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    ]


def test_every_model_is_frozen_with_tuple_collections():
    """Matches ``aiops/context/pack.py``'s discipline, asserted generically.

    ``frozen=True`` alone only locks attribute rebinding — a ``list`` field would
    still be mutable in place, so a consumer could append to another consumer's
    evidence. Checked over ``model_fields`` rather than field by field so a future
    field added as a ``list`` fails here instead of quietly opening the hole.
    """
    offenders: list[str] = []
    for model in _investigation_models():
        if not model.model_config.get("frozen"):
            offenders.append(f"{model.__name__} is not frozen")
        for name, info in model.model_fields.items():
            if repr(info.annotation).startswith("list["):
                offenders.append(f"{model.__name__}.{name} is a list, must be a tuple")
    assert not offenders, "\n".join(offenders)


def test_timeline_pre_onset_changes_excludes_later_events():
    """Only a change that precedes onset is even eligible as a cause.

    Eligibility, not blame: membership here means "could temporally have caused
    this", and the correlation-is-not-causation rule still applies downstream.
    """
    onset = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    before = m.RcaTimelineEvent(
        timestamp=onset,
        source="deployment",
        service="order-service",
        event="rollout completed",
        is_change=True,
        temporal_relation=m.TemporalRelation.PRECEDES_ONSET,
    )
    after = before.model_copy(
        update={"event": "rollback started", "temporal_relation": m.TemporalRelation.FOLLOWS_ONSET}
    )
    symptom = before.model_copy(
        update={"source": "metrics", "event": "error rate up", "is_change": False}
    )
    view = m.IncidentTimelineView(events=(before, after, symptom), onset_at=onset)
    assert view.changes == (before, after)
    assert view.pre_onset_changes == (before,)
