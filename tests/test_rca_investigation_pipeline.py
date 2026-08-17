"""The deterministic investigation: facts, hypotheses, evidence matrix, scoring.

Every test here is a pure function over literal facts — no cluster, no LLM, no mocks.
That is the point of the design: the stages are deterministic, so their behaviour can be
pinned exactly rather than approximated.

The cases are grouped around the properties that are easy to break and expensive to get
wrong: negative evidence must not be confused with a blind spot, a contradiction must be
able to push a hypothesis down, corroboration must require a second source, and history
must never beat current evidence.

Several tests encode bugs that were actually found while building this — the reachable
gauge scored as a contradiction, the timeout rule firing on latency alone — because those
are the mistakes a future edit is most likely to reintroduce.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from agents.rca_agent.investigation import catalog, pipeline, scoring
from agents.rca_agent.investigation.facts import (
    Availability,
    DependencyGauge,
    ErrorRate,
    FiringAlert,
    LatencyP95,
    ObservedFacts,
    PodLifecycle,
    PodResource,
)
from agents.rca_agent.investigation.models import (
    EvidenceItem,
    EvidenceMatrix,
    EvidenceStance,
    HistoricalPrior,
    Hypothesis,
    MemoryStatus,
    RootCauseStatus,
)

TRIAGE = {
    "affected_service": "order-service",
    "severity": "Sev-1",
    "alert_summary": "EcommercePostgresDown firing: postgres_connection_status at 0.0",
    "audit_metadata": {
        "created_at": "2026-08-03T10:00:00Z",
        "source_alerts": ["ALT-order-service-postgres-down"],
    },
}


def _facts(**kwargs) -> ObservedFacts:
    """Facts with both sources CHECKED unless a test says otherwise.

    ``CHECKED`` is the interesting default: it is what licenses negative evidence, so a
    test that wants to prove absence is *not* used has to opt into ``UNAVAILABLE``
    explicitly and is thereby readable as doing so.
    """
    kwargs.setdefault("metrics", Availability.CHECKED)
    kwargs.setdefault("logs", Availability.CHECKED)
    return ObservedFacts(**kwargs)


def _store_down(**extra) -> ObservedFacts:
    return _facts(
        gauges=[
            DependencyGauge(metric="postgres_connection_status", label="PostgreSQL", value=0.0),
            DependencyGauge(metric="mysql_connection_status", label="MySQL", value=1.0),
            DependencyGauge(metric="redis_connection_status", label="Redis", value=1.0),
        ],
        alerts=[FiringAlert(name="EcommercePostgresDown", severity="critical")],
        **extra,
    )


def _investigate(facts: ObservedFacts, **kwargs):
    kwargs.setdefault("change_evidence", [])
    return pipeline.investigate(TRIAGE, facts, **kwargs)


# ─── facts: availability ────────────────────────────────────────────────────


def test_absence_is_evidence_only_when_the_source_was_checked():
    assert Availability.CHECKED.absence_is_evidence
    assert not Availability.UNAVAILABLE.absence_is_evidence


def test_unavailable_metrics_produce_gaps_not_checked_absence():
    """The core negative-evidence rule, at pipeline level.

    With metrics unavailable, a rule's needed-but-missing signal must land in ``gaps``
    (``UNAVAILABLE``) and never in ``checked_absent``. Otherwise the agent earns
    ``negative_corroboration`` for a signal it never read.
    """
    facts = ObservedFacts(
        gauges=[
            DependencyGauge(metric="postgres_connection_status", label="PostgreSQL", value=0.0)
        ],
        metrics=Availability.UNAVAILABLE,
        logs=Availability.UNAVAILABLE,
    )
    result = _investigate(facts)
    for matrix in result.matrices:
        assert not matrix.checked_absent, "an unavailable source cannot yield checked-absent"
        for item in matrix.gaps:
            assert item.stance is EvidenceStance.UNAVAILABLE


def test_checked_but_empty_category_becomes_checked_absent():
    """A reachable source with nothing to report is real evidence.

    This is the counterpart of the test above, and the pair is what makes the distinction
    meaningful: the same empty category means different things depending on availability.
    """
    # A timeout hypothesis needs both error counters and latency. Supplying the counter
    # and no latency gives it a need that was queried and came back empty — which is the
    # only shape that can produce CHECKED_ABSENT.
    facts = _facts(
        error_rates=[ErrorRate(metric="payment_timeout_total", reason="timeout", rate=1.5)]
    )
    result = _investigate(facts)
    absent = [i for m in result.matrices for i in m.checked_absent]
    assert absent, "a CHECKED source with an empty needed category should yield checked-absent"
    assert all(i.stance is EvidenceStance.CHECKED_ABSENT for i in absent)
    assert all("queried" in i.statement for i in absent)


# ─── catalog: rules are evidence-triggered ──────────────────────────────────


def test_no_evidence_proposes_no_hypothesis():
    """A rule must not propose itself on an empty observation.

    Without this the catalog would emit ten candidates for every incident and hand the
    discrimination back to the LLM — the arrangement the pipeline replaces.
    """
    result = _investigate(_facts())
    assert result.matrices == ()
    assert result.status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert result.confidence == 0.0


def test_reachable_gauge_narrows_rather_than_contradicts():
    """Regression: healthy stores are supporting evidence, not counter-evidence.

    An earlier version listed every REACHABLE gauge as contradicting
    ``dependency_unavailable``, which penalised the correct answer by 0.35 on exactly the
    scenarios where the gauges were most decisive. Two healthy stores beside one dead one
    is the strongest localisation available.
    """
    outcome = catalog.RULES_BY_ID["dependency_unavailable"].evaluate(_store_down())
    joined = " ".join(statement for statement, _ in outcome.supporting)
    assert "narrowing the fault to PostgreSQL" in joined
    assert not outcome.contradicting


def test_all_gauges_healthy_does_contradict_a_store_outage():
    facts = _facts(
        gauges=[DependencyGauge(metric="postgres_connection_status", label="PostgreSQL", value=1.0)]
    )
    outcome = catalog.RULES_BY_ID["dependency_unavailable"].evaluate(facts)
    assert not outcome.triggered
    assert any("REACHABLE" in statement for statement, _ in outcome.contradicting)


def test_timeout_rule_requires_a_timeout_signal_not_merely_latency():
    """Regression: slow is not the same finding as timed out.

    The timeout rule used to fire on any latency breach, making it indistinguishable from
    ``latency_regression`` — and on ``user_service_high_latency`` it outscored the correct
    latency hypothesis using evidence that says nothing about timing out.
    """
    slow_only = _facts(latencies=[LatencyP95(hop="order", seconds=10.4, threshold=2.0)])
    assert not catalog.RULES_BY_ID["dependency_timeout"].evaluate(slow_only).triggered

    with_timeouts = _facts(
        latencies=[LatencyP95(hop="order", seconds=10.4, threshold=2.0)],
        error_rates=[ErrorRate(metric="payment_timeout_total", reason="timeout", rate=1.5)],
    )
    assert catalog.RULES_BY_ID["dependency_timeout"].evaluate(with_timeouts).triggered


def test_oom_and_non_oom_termination_select_different_hypotheses():
    """Restart count cannot separate these; the termination reason can.

    Both failure modes share a "service is down" alert, and one of them (external memory
    pressure) never restarts at all — so a rule that keyed on restarts would collapse them.
    """
    oom = _investigate(
        _facts(lifecycles=[PodLifecycle(pod="order-1", restarts=3, terminated_reason="OOMKilled")])
    )
    crash = _investigate(
        _facts(lifecycles=[PodLifecycle(pod="order-1", restarts=3, terminated_reason="Error")])
    )
    assert oom.selected.hypothesis.category == "resource_exhaustion_memory_oom"
    assert crash.selected.hypothesis.category == "startup_failure"


def test_crash_looping_pod_contradicts_a_store_outage():
    """A pod that dies on startup can zero its own dependency gauge.

    So an unreachable store beside a crash-looping container is as likely to be a symptom
    as a cause, and the startup hypothesis — which explains both the restarts and the
    gauge — must outrank it rather than tie with it.
    """
    facts = _store_down(
        lifecycles=[PodLifecycle(pod="order-1", restarts=4, terminated_reason="Error")]
    )
    result = _investigate(facts)
    assert result.selected.hypothesis.category == "startup_failure"


def test_stale_alert_hypothesis_needs_checked_metrics():
    """ "Nothing is wrong" is only observable on a source that answered.

    On an unavailable backend it is the absence of an observation, and this hypothesis must
    never fire on a blind spot — that would turn a monitoring outage into "no live fault".
    """
    quiet_checked = _facts(alerts=[FiringAlert(name="EcommerceServiceDown")])
    assert catalog.RULES_BY_ID["alert_stale_or_resolved"].evaluate(quiet_checked).triggered

    quiet_blind = ObservedFacts(
        alerts=[FiringAlert(name="EcommerceServiceDown")],
        metrics=Availability.UNAVAILABLE,
        logs=Availability.UNAVAILABLE,
    )
    assert not catalog.RULES_BY_ID["alert_stale_or_resolved"].evaluate(quiet_blind).triggered


# ─── scoring ────────────────────────────────────────────────────────────────


def _matrix(**kwargs) -> EvidenceMatrix:
    kwargs.setdefault(
        "hypothesis",
        Hypothesis(hypothesis_id="h1", label="test", mechanism="test mechanism"),
    )
    kwargs.setdefault("contradiction_search_performed", True)
    return EvidenceMatrix(**kwargs)


def _item(eid: str, stance: EvidenceStance, source: str = "metrics") -> EvidenceItem:
    return EvidenceItem(evidence_id=eid, stance=stance, statement=f"{eid} statement", source=source)


def test_score_explains_itself_and_the_arithmetic_reproduces():
    """Every applied factor is named, and base plus deltas equals the score.

    A number handed to a human or a prompt has to be verifiable, not merely readable —
    the convention ``log_correlation/confidence.py`` established.
    """
    result = scoring.score(
        _matrix(
            supporting=(
                _item("e1", EvidenceStance.SUPPORTS),
                _item("e2", EvidenceStance.SUPPORTS, "logs"),
            )
        )
    )
    assert result.factors and result.unapplied
    assert result.rule_trace[0].startswith("base=")
    replayed = scoring.BASE + sum(f.delta for f in result.factors)
    assert result.score == pytest.approx(replayed, abs=1e-9)
    assert "cross_source" in {f.rule_id for f in result.factors}


def test_contradiction_pushes_a_hypothesis_below_base():
    """A supported-but-contradicted hypothesis must rank below an unchallenged one.

    This is why the contradiction penalty exceeds the direct-evidence increment: something
    observed argues against it, and ranking it above an untouched rival is how a confident
    wrong answer is produced.
    """
    supported = scoring.score(_matrix(supporting=(_item("e1", EvidenceStance.SUPPORTS),)))
    contradicted = scoring.score(
        _matrix(
            supporting=(_item("e1", EvidenceStance.SUPPORTS),),
            contradicting=(_item("e2", EvidenceStance.CONTRADICTS),),
        )
    )
    assert contradicted.score < supported.score
    assert contradicted.score < scoring.BASE


def test_cross_source_requires_two_distinct_sources():
    one = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS), _item("b", EvidenceStance.SUPPORTS))
        )
    )
    two = scoring.score(
        _matrix(
            supporting=(
                _item("a", EvidenceStance.SUPPORTS),
                _item("b", EvidenceStance.SUPPORTS, "logs"),
            )
        )
    )
    assert two.score > one.score
    assert "cross_source" in {rule.rule_id for rule in one.unapplied}


def test_gap_penalises_but_does_not_refute():
    """An untested hypothesis must not outrank a tested one, yet stays a candidate."""
    clean = scoring.score(_matrix(supporting=(_item("a", EvidenceStance.SUPPORTS),)))
    gapped = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS),),
            gaps=(_item("g", EvidenceStance.UNAVAILABLE),),
        )
    )
    assert gapped.score < clean.score
    assert gapped.score > scoring.MIN_SCORE


def test_absent_contradiction_search_is_not_a_clean_bill_of_health():
    """The reason matters even when the score does not move.

    An empty ``contradicting`` list means two different things depending on whether
    refutation was attempted, and the explanation has to say which.
    """
    searched = scoring.score(_matrix(supporting=(_item("a", EvidenceStance.SUPPORTS),)))
    unsearched = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS),),
            contradiction_search_performed=False,
        )
    )
    reason = next(r.reason for r in unsearched.unapplied if r.rule_id == "contradicted")
    assert "no contradiction search" in reason
    searched_reason = next(r.reason for r in searched.unapplied if r.rule_id == "contradicted")
    assert "nothing contradicts" in searched_reason


def test_score_is_bounded_and_never_claims_certainty():
    strong = scoring.score(
        _matrix(
            supporting=tuple(
                _item(f"e{i}", EvidenceStance.SUPPORTS, "logs" if i else "metrics")
                for i in range(6)
            ),
            checked_absent=(_item("abs", EvidenceStance.CHECKED_ABSENT),),
            priors=(HistoricalPrior(memory_id="m1", status=MemoryStatus.TRUSTED, similarity=1.0),),
        )
    )
    assert strong.score <= scoring.MAX_SCORE < 1.0


# ─── current evidence beats history ─────────────────────────────────────────


def test_verified_prior_lifts_the_score_but_only_within_its_cap():
    without = scoring.score(_matrix(supporting=(_item("a", EvidenceStance.SUPPORTS),)))
    with_prior = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS),),
            priors=(HistoricalPrior(memory_id="m1", status=MemoryStatus.VERIFIED, similarity=1.0),),
        )
    )
    assert with_prior.score > without.score
    assert with_prior.score - without.score <= scoring.PRIOR_MAX + 1e-9


def test_unverified_prior_is_ignored_entirely():
    """A prediction is not knowledge, so it cannot act as a prior."""
    plain = scoring.score(_matrix(supporting=(_item("a", EvidenceStance.SUPPORTS),)))
    with_unverified = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS),),
            priors=(
                HistoricalPrior(memory_id="m1", status=MemoryStatus.UNVERIFIED, similarity=1.0),
            ),
        )
    )
    assert with_unverified.score == plain.score


def test_contradicting_current_evidence_cancels_the_prior_completely():
    """The requirement, as arithmetic rather than as prompt text.

    History may order an investigation; it may never rescue a hypothesis that current
    evidence argues against.
    """
    result = scoring.score(
        _matrix(
            supporting=(_item("a", EvidenceStance.SUPPORTS),),
            contradicting=(_item("b", EvidenceStance.CONTRADICTS),),
            priors=(HistoricalPrior(memory_id="m1", status=MemoryStatus.TRUSTED, similarity=1.0),),
        )
    )
    assert "historical_prior" not in {f.rule_id for f in result.factors}
    reason = next(r.reason for r in result.unapplied if r.rule_id == "historical_prior")
    assert "current evidence wins" in reason


def test_prior_cap_is_below_every_current_evidence_increment():
    """Structural guarantee, not a value judgement re-litigated per review."""
    assert scoring.PRIOR_MAX < scoring.DELTA_DIRECT
    assert scoring.PRIOR_MAX < scoring.DELTA_CROSS_SOURCE
    assert scoring.PRIOR_MAX <= scoring.DELTA_NEGATIVE_COROBORATION


# ─── discrimination and status ──────────────────────────────────────────────


def test_a_tie_is_reported_as_uncertain_not_as_a_winner():
    """When the evidence does not separate two candidates, say so.

    Presenting a coin flip as a conclusion is worse than admitting the ambiguity, and the
    confidence is capped as well as relabelled so the number cannot contradict the status.
    """
    tied = [
        _matrix(
            hypothesis=Hypothesis(hypothesis_id=f"h{i}", label=f"h{i}", mechanism="m"),
            supporting=(_item(f"e{i}", EvidenceStance.SUPPORTS),),
        )
        for i in range(2)
    ]
    ranked = scoring.rank(tied)
    assert not scoring.discriminates(ranked)


def test_a_clear_leader_discriminates():
    strong = _matrix(
        hypothesis=Hypothesis(hypothesis_id="h1", label="strong", mechanism="m"),
        supporting=(
            _item("a", EvidenceStance.SUPPORTS),
            _item("b", EvidenceStance.SUPPORTS, "logs"),
        ),
    )
    weak = _matrix(
        hypothesis=Hypothesis(hypothesis_id="h2", label="weak", mechanism="m"),
        supporting=(_item("c", EvidenceStance.SUPPORTS),),
        contradicting=(_item("d", EvidenceStance.CONTRADICTS),),
    )
    ranked = scoring.rank([weak, strong])
    assert ranked[0].hypothesis.hypothesis_id == "h1"
    assert scoring.discriminates(ranked)


def test_ranking_is_deterministic_regardless_of_input_order():
    """Equal scores break on hypothesis id, never on arrival order.

    Otherwise an eval diff reports a top-hypothesis change with no code change behind it.
    """
    made = [
        _matrix(
            hypothesis=Hypothesis(hypothesis_id=f"h{i}", label="x", mechanism="m"),
            supporting=(_item(f"e{i}", EvidenceStance.SUPPORTS),),
        )
        for i in ("c", "a", "b")
    ]
    forward = [m.hypothesis.hypothesis_id for m in scoring.rank(made)]
    backward = [m.hypothesis.hypothesis_id for m in scoring.rank(list(reversed(made)))]
    assert forward == backward == ["ha", "hb", "hc"]


def test_selected_hypothesis_is_none_when_the_status_is_not_actionable():
    """An unsettled investigation must not nominate a cause for remediation."""
    result = _investigate(_facts())
    assert result.selected_hypothesis_id is None
    assert not result.status.is_actionable


# ─── scope, timeline, baseline, completeness ────────────────────────────────


def test_scope_derives_the_symptom_from_evidence_not_the_alert_summary():
    """Copying the alert summary into the symptom is the "root cause: HTTP 500" collapse."""
    scope = pipeline.build_scope(TRIAGE, _store_down())
    assert "unreachable" in scope.user_visible_symptom
    assert scope.alert_name == "EcommercePostgresDown"
    assert scope.incident_id == "ALT-order-service-postgres-down"


def test_scope_reports_no_observable_symptom_rather_than_inventing_one():
    scope = pipeline.build_scope(TRIAGE, _facts())
    assert scope.user_visible_symptom == "no symptom is currently observable"


def test_timeline_separates_changes_before_and_after_onset():
    """A change after onset cannot be a cause, and the exclusion comes from data."""
    scope = pipeline.build_scope(TRIAGE, _store_down())
    timeline = pipeline.build_timeline(
        scope,
        _store_down(),
        change_evidence=[
            {"sha": "aaa", "date": "2026-08-03T09:30:00Z", "message": "before"},
            {"sha": "bbb", "date": "2026-08-03T10:30:00Z", "message": "after"},
        ],
    )
    assert [e.event.split(":")[1].strip() for e in timeline.pre_onset_changes] == ["before"]
    assert len(timeline.changes) == 2


def test_timeline_says_which_sources_it_could_not_see():
    """An absent source is ambiguous unless the timeline names it."""
    scope = pipeline.build_scope(TRIAGE, _store_down())
    timeline = pipeline.build_timeline(scope, _store_down(), change_evidence=None)
    assert "deployment" in timeline.sources_unavailable
    assert timeline.coverage_note


def test_baselines_are_partial_never_available():
    """The reference is a threshold or a limit, not a learned baseline.

    Reporting ``AVAILABLE`` would claim a per-service history this repo does not have.
    """
    facts = _facts(
        latencies=[LatencyP95(hop="order", seconds=10.4, threshold=2.0)],
        resources=[PodResource(pod="order-1", cpu_cores=0.9, memory_ratio=0.9)],
    )
    baselines = pipeline.build_baselines(facts)
    assert baselines
    assert all(b.status.value in ("partial", "unavailable") for b in baselines)
    latency = next(b for b in baselines if b.metric.startswith("order"))
    assert latency.is_abnormal is True
    assert latency.deviation_ratio == pytest.approx(5.2)


def test_baseline_without_a_reference_is_unavailable_not_normal():
    facts = _facts(latencies=[LatencyP95(hop="login", seconds=8.0, threshold=None)])
    baseline = pipeline.build_baselines(facts)[0]
    assert baseline.status.value == "unavailable"
    assert baseline.is_abnormal is None, "no reference means cannot say, not 'normal'"


def test_completeness_counts_unavailable_sources_as_gaps():
    blind = ObservedFacts(metrics=Availability.UNAVAILABLE, logs=Availability.UNAVAILABLE)
    report = pipeline.build_completeness(blind, change_evidence=None)
    assert report.overall == 0.0
    assert "logs" in report.critical_gaps
    assert catalog.NEED_CHANGES in report.critical_gaps


def test_completeness_distinguishes_empty_from_unavailable():
    """An empty-but-queried source counts as answered; an unavailable one does not."""
    report = pipeline.build_completeness(_facts(), change_evidence=[])
    assert report.overall == 1.0
    assert report.critical_gaps == ()
    assert set(report.per_source.values()) == {"empty"}


def test_investigation_never_raises_on_a_broken_rule(monkeypatch):
    """A buggy rule costs its own hypothesis, not the investigation.

    Same posture as every other lookup in this agent: a defect degrades the result rather
    than failing the incident.
    """

    def explode(_facts):
        raise RuntimeError("rule is broken")

    # ``HypothesisRule`` is a frozen dataclass, so the broken rule is built by replacing
    # the callable rather than patching the instance — which also proves the guard works
    # for a genuinely new rule, not just a mutated existing one.
    good = catalog.RULES_BY_ID["oom_kill"]
    broken = dataclasses.replace(catalog.RULES_BY_ID["dependency_unavailable"], evaluate=explode)
    monkeypatch.setattr(catalog, "RULES", (broken, good))

    result = _investigate(
        _store_down(lifecycles=[PodLifecycle(pod="p", restarts=1, terminated_reason="OOMKilled")])
    )
    assert result.selected is not None, "a broken rule must not lose the whole investigation"
    assert result.selected.hypothesis.category == "resource_exhaustion_memory_oom"


def test_evidence_ids_are_stable_across_runs():
    """Deterministic ids let a verdict be compared with its predecessor.

    Random ids would make every re-run differ for reasons unrelated to the code.
    """
    first = _investigate(_store_down())
    second = _investigate(_store_down())
    ids = lambda inv: [i.evidence_id for m in inv.matrices for i in m.supporting]  # noqa: E731
    assert ids(first) == ids(second)


def test_timeline_handles_a_naive_timestamp_without_raising():
    """Providers disagree about offsets, and a mixed subtraction raises.

    On the incident path that would cost the whole timeline for one badly formatted date.
    """
    scope = pipeline.build_scope(TRIAGE, _store_down())
    timeline = pipeline.build_timeline(
        scope,
        _store_down(),
        change_evidence=[{"sha": "aaa", "date": "2026-08-03T09:30:00", "message": "naive"}],
    )
    assert len(timeline.changes) == 1
    assert timeline.changes[0].timestamp.replace(tzinfo=UTC) <= datetime(
        2026, 8, 3, 10, 0, tzinfo=UTC
    )
