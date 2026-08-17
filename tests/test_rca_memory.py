"""Phase 3 — historical outcome memory: lifecycle, weighting, and the limits on influence.

Organised around the four claims Phase 3 makes, because each is a place the design could
silently stop being true:

1. **Only verified outcomes become memory.** A prediction is not knowledge.
2. **A prior is attenuated before it is capped.** Reliability and freshness first, then
   ``PRIOR_MAX``.
3. **Current evidence wins, arithmetically.** Contradiction cancels a prior outright, and
   a prior can neither promote a status band nor manufacture discrimination.
4. **Influence is reported, and measured rather than asserted.**

Every test here is offline: the state DB is per-test and empty (``conftest``'s
``_hermetic_state_db``), so a "cold start" needs no setup and a populated store cannot
leak between tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.rca_agent.investigation import memory, scoring
from agents.rca_agent.investigation.models import (
    EvidenceItem,
    EvidenceMatrix,
    EvidenceStance,
    HistoricalPrior,
    Hypothesis,
    MemoryReliability,
    MemoryStatus,
    RCAOutcome,
    RootCauseStatus,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


# ─── fixtures and builders ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _memory_enabled(monkeypatch):
    """Default these tests to the learning configuration.

    Explicit rather than relying on the module default, so a change to that default
    cannot silently turn this whole file into a cold-start suite that passes for the
    wrong reason.
    """
    monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "rca_outcomes")


def _outcome(
    *,
    incident_id: str = "INC-1",
    service: str = "payment-service",
    cause: str = "redis unreachable from payment-service",
    hypothesis: str = "dependency_unavailable",
    verification: str = "resolved",
    corrected: str | None = None,
    confidence: float = 0.8,
    recorded_at: datetime | None = None,
) -> RCAOutcome:
    return RCAOutcome(
        incident_id=incident_id,
        affected_service=service,
        recorded_at=recorded_at or NOW,
        predicted_root_cause=cause,
        predicted_status=RootCauseStatus.PROBABLE,
        confidence=confidence,
        selected_hypothesis_class=hypothesis,
        verification_result=verification,  # type: ignore[arg-type]
        human_corrected_root_cause=corrected,
        extra={"signatures": ["PaymentRedisDown", "redis_up:cache:unreachable"]},
    )


def _matrix(
    hypothesis_id: str,
    *,
    supporting: int = 1,
    sources: tuple[str, ...] = ("metrics",),
    contradicting: int = 0,
    priors: tuple[HistoricalPrior, ...] = (),
) -> EvidenceMatrix:
    def items(n: int, stance: EvidenceStance, prefix: str) -> tuple[EvidenceItem, ...]:
        return tuple(
            EvidenceItem(
                evidence_id=f"{prefix}-{hypothesis_id}-{i}",
                stance=stance,
                statement=f"{prefix} {i}",
                source=sources[i % len(sources)],
            )
            for i in range(n)
        )

    return EvidenceMatrix(
        hypothesis=Hypothesis(
            hypothesis_id=hypothesis_id,
            label=hypothesis_id.replace("_", " "),
            mechanism=f"{hypothesis_id} is the cause",
            category=hypothesis_id,
        ),
        supporting=items(supporting, EvidenceStance.SUPPORTS, "sup"),
        contradicting=items(contradicting, EvidenceStance.CONTRADICTS, "con"),
        priors=priors,
        contradiction_search_performed=True,
    )


def _prior(
    memory_id: str = "INC-1",
    *,
    similarity: float = 0.9,
    hypothesis: str = "dependency_unavailable",
    status: MemoryStatus = MemoryStatus.VERIFIED,
) -> HistoricalPrior:
    return HistoricalPrior(
        memory_id=memory_id,
        status=status,
        similarity=similarity,
        recorded_cause="redis unreachable",
        matched_on=("service:payment-service", f"class:{hypothesis}"),
    )


def _seed(outcome: RCAOutcome, *, verified_recurrences: int = 0) -> int | None:
    return memory.record_outcome(outcome, verified_recurrences=verified_recurrences)


# ─── 1. only verified outcomes become memory ────────────────────────────────


class TestPromotionLifecycle:
    def test_a_bare_prediction_is_never_more_than_new(self):
        outcome = _outcome(verification="not_run")
        assert memory.promote(outcome) is MemoryStatus.NEW

    def test_an_executed_but_unconfirmed_prediction_stops_at_unverified(self):
        outcome = _outcome(verification="not_run").model_copy(
            update={"executed_action": "restart-pod"}
        )
        assert memory.promote(outcome) is MemoryStatus.UNVERIFIED

    def test_a_not_resolved_verification_stops_at_unverified(self):
        assert memory.promote(_outcome(verification="not_resolved")) is MemoryStatus.UNVERIFIED

    def test_partially_resolved_is_not_good_enough(self):
        """``partially_resolved`` is not ``resolved``.

        The looser reading would let a fix that helped-but-did-not-work become trusted
        knowledge, which is how a half-correct cause gets remembered as a whole one.
        """
        assert memory.promote(_outcome(verification="partially_resolved")) is (
            MemoryStatus.UNVERIFIED
        )

    def test_confirmed_recovery_becomes_verified(self):
        assert memory.promote(_outcome(verification="resolved")) is MemoryStatus.VERIFIED

    def test_a_human_correction_is_verified_knowledge_even_though_the_prediction_failed(self):
        outcome = _outcome(verification="not_resolved", corrected="postgres connection pool full")
        assert outcome.eligible_for_memory
        assert memory.promote(outcome) is MemoryStatus.VERIFIED

    def test_trust_needs_repetition(self):
        outcome = _outcome(verification="resolved")
        assert memory.promote(outcome, verified_recurrences=1) is MemoryStatus.VERIFIED
        assert (
            memory.promote(outcome, verified_recurrences=memory.TRUST_THRESHOLD)
            is MemoryStatus.TRUSTED
        )

    def test_retractions_beat_everything(self):
        outcome = _outcome(verification="resolved")
        assert memory.promote(outcome, superseded=True) is MemoryStatus.SUPERSEDED
        assert memory.promote(outcome, invalidated=True) is MemoryStatus.INVALIDATED
        # Invalidated wins over superseded: "known wrong" is a stronger statement than
        # "replaced", and the weaker label would leave bad knowledge looking merely dated.
        assert (
            memory.promote(outcome, superseded=True, invalidated=True) is MemoryStatus.INVALIDATED
        )

    def test_confidence_alone_never_promotes(self):
        """The property that stops the agent bootstrapping its own mistakes."""
        cocky = _outcome(verification="not_run", confidence=0.95)
        assert memory.promote(cocky) is MemoryStatus.NEW
        assert not cocky.eligible_for_memory


class TestRecallableStatuses:
    def test_only_verified_and_trusted_are_usable_for_ranking(self):
        usable = {s for s in MemoryStatus if s.usable_for_ranking}
        assert usable == {MemoryStatus.VERIFIED, MemoryStatus.TRUSTED}

    def test_the_repository_and_the_enum_agree(self):
        """The storage layer duplicates the recallable set as string literals because
        ``aiops/`` may not import ``agents/``. This is the test that stops that
        duplication drifting."""
        from aiops.state.repository import RECALLABLE_MEMORY_STATUSES

        assert set(RECALLABLE_MEMORY_STATUSES) == {
            s.value for s in MemoryStatus if s.usable_for_ranking
        }

    def test_an_unverified_row_is_not_recalled(self):
        _seed(_outcome(incident_id="INC-good"))
        _seed(_outcome(incident_id="INC-bad", verification="not_run"))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        assert [p.memory_id for p in result.priors] == ["INC-good"]

    def test_an_invalidated_row_is_not_recalled(self):
        from aiops.state.repository import update_rca_outcome_memory_status

        row_id = _seed(_outcome(incident_id="INC-wrong"))
        assert row_id is not None
        update_rca_outcome_memory_status(row_id, MemoryStatus.INVALIDATED.value)
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.priors == ()

    def test_invalidating_retains_the_row_for_audit(self):
        """Bad knowledge is retracted, never deleted — deleting it destroys the evidence
        that it was ever used to reach a conclusion."""
        from aiops.state.repository import count_rca_outcomes, update_rca_outcome_memory_status

        row_id = _seed(_outcome(incident_id="INC-wrong"))
        assert row_id is not None
        update_rca_outcome_memory_status(row_id, MemoryStatus.INVALIDATED.value)
        assert count_rca_outcomes() == 1


# ─── 2. attenuation before the cap ──────────────────────────────────────────


class TestFreshnessDecay:
    def test_a_brand_new_outcome_is_undiscounted(self):
        assert memory.freshness_weight(0.0) == 1.0

    def test_one_half_life_halves_the_weight(self):
        assert memory.freshness_weight(memory.MEMORY_HALF_LIFE_DAYS) == pytest.approx(0.5)

    def test_two_half_lives_quarter_it(self):
        assert memory.freshness_weight(2 * memory.MEMORY_HALF_LIFE_DAYS) == pytest.approx(
            0.25, abs=1e-3
        )

    def test_past_the_stale_horizon_the_weight_is_zero(self):
        assert memory.freshness_weight(memory.MEMORY_STALE_DAYS) == 0.0
        assert memory.freshness_weight(memory.MEMORY_STALE_DAYS + 500) == 0.0

    def test_an_unknown_age_is_not_treated_as_ancient(self):
        """A missing timestamp is a provenance gap. Inventing a penalty for it would be
        as unfounded as inventing a date."""
        assert memory.freshness_weight(None) == 1.0

    def test_a_stale_outcome_is_dropped_and_the_drop_is_reported(self):
        _seed(_outcome(incident_id="INC-ancient", recorded_at=NOW - timedelta(days=400)))
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.priors == ()
        assert "INC-ancient" in result.dropped_stale
        assert any("stale" in note for note in result.notes)


class TestReliabilityWeighting:
    def test_an_ungraded_pattern_is_discounted_not_trusted(self):
        assert memory.reliability_weight(MemoryReliability()) == memory.UNPROVEN_RELIABILITY

    def test_success_rate_is_none_rather_than_zero_with_no_history(self):
        assert MemoryReliability().success_rate is None

    def test_a_good_track_record_weighs_nearly_full(self):
        rel = MemoryReliability(occurrences=17, verified_correct=15)
        assert memory.reliability_weight(rel) == pytest.approx(0.8421, abs=1e-3)

    def test_a_single_confirmation_does_not_earn_a_track_records_weight(self):
        """The over-trust ``RELIABILITY_SMOOTHING`` exists to fix: a raw ratio scored
        1-of-1 as 1.0, identical to 15-of-17, so a pattern carried maximum prior weight
        the very first time it was ever confirmed."""
        once = memory.reliability_weight(MemoryReliability(occurrences=1, verified_correct=1))
        often = memory.reliability_weight(MemoryReliability(occurrences=17, verified_correct=15))
        assert once < often
        assert once == pytest.approx(0.6667, abs=1e-3)

    def test_a_pattern_that_has_never_been_right_contributes_nothing(self):
        rel = MemoryReliability(occurrences=4, verified_correct=0, rejected=4)
        assert memory.reliability_weight(rel) == 0.0

    def test_reliability_counts_a_human_correction_as_a_rejection(self):
        _seed(_outcome(incident_id="INC-1"))
        _seed(_outcome(incident_id="INC-2", corrected="actually the connection pool"))
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.priors, "expected at least one prior"
        rel = result.priors[0].reliability
        assert rel.occurrences == 2
        assert rel.verified_correct == 1
        assert rel.rejected == 1

    def test_a_corrected_outcome_carries_no_weight_for_the_class_it_refuted(self):
        """A correction is evidence *against* the hypothesis class that was concluded.

        The outcome is filed under the hypothesis the agent selected — the wrong one —
        so its track record for that class is 0 of 1 and its prior weight is 0. That is
        the correct reading: a human said this class was not the cause here.

        The correction *is* verified knowledge, but about a class nothing records: the
        human supplied prose, not a hypothesis id. So a correction can currently only
        de-weight the wrong answer, never boost the right one. Capturing a corrected
        hypothesis class is HITL work and belongs with Phase 6.
        """
        _seed(
            _outcome(
                incident_id="INC-corrected",
                cause="dns failure",
                corrected="postgres connection pool exhausted",
            )
        )
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.priors == ()
        assert "INC-corrected" in result.dropped_stale

    def test_a_correction_outweighs_the_prediction_when_the_class_is_otherwise_sound(self):
        """With a track record, the remembered cause is the correction, not the guess."""
        for i in range(3):
            _seed(_outcome(incident_id=f"INC-ok-{i}"))
        _seed(
            _outcome(
                incident_id="INC-corrected",
                cause="dns failure",
                corrected="postgres connection pool exhausted",
            )
        )
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        corrected = [p for p in result.priors if p.memory_id == "INC-corrected"]
        assert corrected, "expected the corrected outcome to survive with 3 of 4 verified"
        assert corrected[0].recorded_cause == "postgres connection pool exhausted"
        assert corrected[0].provenance.human_corrected is True


class TestAttenuationCompounds:
    def test_similarity_is_attenuated_below_the_raw_retrieval_score(self):
        """A once-confirmed, month-old outcome should not arrive as a 1.0 match.

        Raw similarity 1.0 x reliability 0.667 (1 of 1, shrunk) x one half-life 0.5.
        """
        _seed(_outcome(incident_id="INC-1", recorded_at=NOW - timedelta(days=30)))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        assert result.priors
        assert result.priors[0].similarity == pytest.approx(0.3333, abs=0.02)

    def test_attenuation_can_push_a_match_below_the_floor(self):
        _seed(_outcome(incident_id="INC-old-unproven", recorded_at=NOW - timedelta(days=90)))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        # 1.0 x 0.667 x 0.125 = 0.083, below MIN_PRIOR_SIMILARITY.
        assert result.priors == ()
        assert "INC-old-unproven" in result.dropped_stale


# ─── 3. current evidence wins, arithmetically ───────────────────────────────


class TestPriorCeiling:
    def test_a_perfect_prior_contributes_no_more_than_prior_max(self):
        with_prior = scoring.score(_matrix("h", priors=(_prior(similarity=1.0),)))
        without = scoring.score(_matrix("h"))
        assert with_prior.score - without.score == pytest.approx(scoring.PRIOR_MAX, abs=1e-4)

    def test_prior_max_is_bounded_by_the_weakest_current_evidence_increment(self):
        """A prior is worth at most one checked-and-absent signal, and always less than a
        direct observation, a second signal, or cross-source corroboration."""
        increments = (
            scoring.DELTA_DIRECT,
            scoring.DELTA_MULTI_SIGNAL,
            scoring.DELTA_CROSS_SOURCE,
            scoring.DELTA_NEGATIVE_COROBORATION,
        )
        assert scoring.PRIOR_MAX <= min(increments)
        stronger = [d for d in increments if d > scoring.PRIOR_MAX]
        assert len(stronger) == len(increments) - 1, "only the weakest term may tie"

    def test_an_ineligible_prior_contributes_nothing(self):
        for status in (
            MemoryStatus.NEW,
            MemoryStatus.UNVERIFIED,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.INVALIDATED,
        ):
            scored = scoring.score(_matrix("h", priors=(_prior(status=status),)))
            assert scored.score == scoring.score(_matrix("h")).score, status

    def test_contradicting_evidence_cancels_the_prior_entirely(self):
        contradicted = _matrix("h", contradicting=1, priors=(_prior(similarity=1.0),))
        no_prior = _matrix("h", contradicting=1)
        assert scoring.score(contradicted).score == scoring.score(no_prior).score
        cancelled = [
            r
            for r in scoring.score(contradicted).unapplied
            if r.rule_id == "historical_prior" and "cancelled" in r.reason
        ]
        assert cancelled, "the cancellation must be recorded, not merely applied"


class TestHistoryCannotUpgradeTheClaim:
    """The gap this closes: ``PRIOR_MAX`` (0.10) exceeds the gap between status
    thresholds (0.30/0.50/0.75) and exceeds what ``DISCRIMINATION_MARGIN`` absorbs, so
    the documented "history may never make the difference between abstaining and
    asserting" rule was not enforced by the arithmetic until ``_status_for`` grew these
    two rules."""

    def test_a_prior_cannot_lift_a_status_across_a_band(self):
        from agents.rca_agent.investigation.pipeline import _status_for

        facts = _observed_facts()
        ranked = [_scored(_matrix("h"), 0.55)]
        # Evidence alone put it at 0.45 -> UNCERTAIN. The prior-inclusive 0.55 must not
        # read as PROBABLE.
        status, confidence = _status_for(
            ranked, facts, discriminated=True, evidence_only_confidence=0.45
        )
        assert status is RootCauseStatus.UNCERTAIN
        assert confidence < 0.5

    def test_a_prior_may_still_raise_the_number_inside_its_band(self):
        from agents.rca_agent.investigation.pipeline import _status_for

        _, confidence = _status_for(
            [_scored(_matrix("h"), 0.48)],
            _observed_facts(),
            discriminated=True,
            evidence_only_confidence=0.42,
        )
        assert 0.42 < confidence < 0.5

    def test_evidence_alone_still_decides_the_band_when_no_prior_is_present(self):
        from agents.rca_agent.investigation.pipeline import _status_for

        status, confidence = _status_for(
            [_scored(_matrix("h"), 0.8)], _observed_facts(), discriminated=True
        )
        assert status is RootCauseStatus.CONFIRMED
        assert confidence == pytest.approx(0.8)

    def test_a_prior_cannot_manufacture_discrimination(self):
        """Two hypotheses the evidence could not separate must stay unseparated, even if
        a prior opens a gap wider than ``DISCRIMINATION_MARGIN``."""
        from agents.rca_agent.investigation import pipeline

        facts = _observed_facts()
        # 0.12 apart on evidence alone — inside DISCRIMINATION_MARGIN. A full 0.10 prior
        # on the leader opens the gap to 0.22, which would separate them.
        evidence_only = [_scored(_matrix("a"), 0.45), _scored(_matrix("b"), 0.33)]
        with_memory = [_scored(_matrix("a"), 0.55), _scored(_matrix("b"), 0.33)]

        assert not scoring.discriminates(evidence_only)
        assert scoring.discriminates(with_memory), "precondition: the prior opens the gap"

        status, confidence = pipeline._status_for(
            with_memory,
            facts,
            discriminated=scoring.discriminates(evidence_only),
            evidence_only_confidence=0.45,
        )
        assert status is RootCauseStatus.UNCERTAIN
        assert confidence <= 0.5


# ─── 4. influence is reported and measured ──────────────────────────────────


class TestInfluenceReporting:
    def test_no_recall_reads_as_not_consulted(self):
        assert memory.influence([], None).level == "none"
        assert "not consulted" in (memory.influence([], None).note or "")

    def test_cold_start_says_so(self, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "")
        result = memory.recall(service="payment-service", signatures=["X"], now=NOW)
        assert result.status == "disabled"
        influence = memory.influence([], result)
        assert influence.level == "none"
        assert "cold" in (influence.note or "").lower()

    def test_an_unreadable_store_is_not_an_empty_history(self, monkeypatch):
        """The distinction a bare empty list would erase."""
        from aiops.tools.incident_history.retriever import _PROVIDERS

        provider = _PROVIDERS["rca_outcomes"]
        monkeypatch.setattr(provider, "search", lambda q: _unavailable_result(), raising=True)
        result = memory.recall(service="payment-service", signatures=["X"], now=NOW)
        assert result.status == "unavailable"
        note = memory.influence([], result).note or ""
        assert "unavailable" in note or "not the same as no history" in note

    def test_influence_names_the_priors_it_applied(self):
        matrices = [_scored(_matrix("dependency_unavailable", priors=(_prior(),)), None)]
        recall_result = memory.MemoryRecall(
            status="recalled", priors=(_prior(),), providers_used=("rca_outcomes",), considered=1
        )
        influence = memory.influence(matrices, recall_result)
        assert influence.priors_eligible == 1
        assert influence.priors_applied == ("INC-1",)
        assert str(scoring.PRIOR_MAX) in (influence.note or "")

    def test_a_cancelled_prior_is_reported_as_overridden(self):
        matrices = [
            _scored(_matrix("dependency_unavailable", contradicting=1, priors=(_prior(),)), None)
        ]
        recall_result = memory.MemoryRecall(
            status="recalled", priors=(_prior(),), providers_used=("rca_outcomes",), considered=1
        )
        influence = memory.influence(matrices, recall_result)
        assert influence.overridden_by_current_evidence == ("INC-1",)
        assert "current evidence" in (influence.note or "")

    def test_changed_ranking_is_measured_by_ranking_twice(self):
        without = [_scored(_matrix("b"), 0.50), _scored(_matrix("a"), 0.45)]
        with_memory = [_scored(_matrix("a"), 0.55), _scored(_matrix("b"), 0.50)]
        recall_result = memory.MemoryRecall(
            status="recalled", priors=(_prior(hypothesis="a"),), considered=1
        )
        influence = memory.influence(with_memory, recall_result, ranked_without_memory=without)
        assert influence.changed_ranking is True
        assert influence.level == "strong"
        assert "changed the ranking" in (influence.note or "")

    def test_an_unchanged_ranking_is_stated_as_such(self):
        ranked = [_scored(_matrix("a", priors=(_prior(hypothesis="a"),)), None)]
        recall_result = memory.MemoryRecall(
            status="recalled", priors=(_prior(hypothesis="a"),), considered=1
        )
        influence = memory.influence(ranked, recall_result, ranked_without_memory=ranked)
        assert influence.changed_ranking is False
        assert "did not change" in (influence.note or "")


# ─── prior/hypothesis attachment ────────────────────────────────────────────


class TestPriorAttachment:
    def test_priors_attach_by_hypothesis_id(self):
        matrices = [_matrix("dependency_unavailable"), _matrix("resource_saturation_cpu")]
        recall_result = memory.MemoryRecall(
            status="recalled", priors=(_prior(hypothesis="dependency_unavailable"),)
        )
        attached = memory.attach_priors(matrices, recall_result)
        assert len(attached[0].priors) == 1
        assert attached[1].priors == ()

    def test_a_prior_with_no_hypothesis_id_attaches_to_nothing(self):
        orphan = HistoricalPrior(
            memory_id="INC-orphan",
            status=MemoryStatus.VERIFIED,
            similarity=0.9,
            matched_on=("service:payment-service",),
        )
        attached = memory.attach_priors(
            [_matrix("dependency_unavailable")],
            memory.MemoryRecall(status="recalled", priors=(orphan,)),
        )
        assert attached[0].priors == ()

    def test_attachment_does_not_match_on_prose(self):
        """A prior remembering "Redis connection pool exhausted" must not attach to a CPU
        hypothesis because both mention a resource."""
        prior = HistoricalPrior(
            memory_id="INC-1",
            status=MemoryStatus.VERIFIED,
            similarity=0.9,
            recorded_cause="Redis connection pool exhausted on payment-service",
            matched_on=("class:dependency_unavailable",),
        )
        attached = memory.attach_priors(
            [_matrix("resource_exhaustion_memory")],
            memory.MemoryRecall(status="recalled", priors=(prior,)),
        )
        assert attached[0].priors == ()


# ─── recall mechanics ───────────────────────────────────────────────────────


class TestRecallMechanics:
    def test_an_empty_store_reports_empty_not_disabled(self):
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.status == "empty"
        assert result.providers_used == ("rca_outcomes",)

    def test_no_signatures_means_no_recall_attempt(self):
        result = memory.recall(service="payment-service", signatures=[], now=NOW)
        assert result.status == "empty"
        assert any("signature" in n for n in result.notes)

    def test_leave_one_out_exclusion(self):
        """Scoring a scenario against memory containing its own outcome measures nothing."""
        _seed(_outcome(incident_id="INC-self"))
        _seed(_outcome(incident_id="INC-other"))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            exclude_incident_ids=("INC-self",),
            now=NOW,
        )
        assert [p.memory_id for p in result.priors] == ["INC-other"]

    def test_dissimilar_symptoms_do_not_match(self):
        """Same service, no shared symptom, no prior.

        The service term alone (0.25) clears the similarity floor, so without an
        explicit shared-symptom requirement every past incident on a service became a
        prior for every new one - "this service has had incidents before" presented as
        precedent.
        """
        _seed(_outcome(incident_id="INC-1"))
        result = memory.recall(
            service="payment-service",
            signatures=["SomethingCompletelyDifferent", "unrelated_metric_total"],
            now=NOW,
        )
        assert result.priors == ()

    def test_a_trusted_row_is_recalled_as_trusted(self):
        _seed(_outcome(incident_id="INC-trusted"), verified_recurrences=memory.TRUST_THRESHOLD)
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        assert result.priors
        assert result.priors[0].status is MemoryStatus.TRUSTED

    def test_priors_carry_provenance_back_to_the_incident(self):
        _seed(_outcome(incident_id="INC-1"))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            now=NOW,
        )
        assert result.priors
        provenance = result.priors[0].provenance
        assert provenance.source_incident_ids == ("INC-1",)
        assert provenance.verification_result == "resolved"

    def test_recall_never_raises_on_a_broken_provider(self, monkeypatch):
        from aiops.tools.incident_history.retriever import _PROVIDERS

        def boom(_query):
            raise RuntimeError("store on fire")

        monkeypatch.setattr(_PROVIDERS["rca_outcomes"], "search", boom, raising=True)
        result = memory.recall(service="payment-service", signatures=["X"], now=NOW)
        assert result.status == "unavailable"
        assert result.priors == ()

    def test_priors_are_capped_in_number(self):
        for i in range(12):
            _seed(_outcome(incident_id=f"INC-{i}"))
        result = memory.recall(
            service="payment-service",
            signatures=["PaymentRedisDown", "redis_up:cache:unreachable"],
            limit=memory.MAX_PRIORS,
            now=NOW,
        )
        assert len(result.priors) <= memory.MAX_PRIORS


class TestRecordOutcome:
    def test_recording_stores_the_promoted_status(self):
        row_id = _seed(_outcome())
        from aiops.state.repository import get_rca_outcome

        assert row_id is not None
        row = get_rca_outcome(row_id)
        assert row is not None
        assert row["memory_status"] == MemoryStatus.VERIFIED.value

    def test_recording_an_unverified_outcome_stores_it_unrecallable(self):
        row_id = _seed(_outcome(verification="not_run"))
        from aiops.state.repository import get_rca_outcome

        assert row_id is not None
        row = get_rca_outcome(row_id)
        assert row is not None
        assert row["memory_status"] == MemoryStatus.NEW.value

    def test_the_prediction_survives_a_correction(self):
        row_id = _seed(_outcome(cause="dns failure", corrected="pool exhausted"))
        from aiops.state.repository import get_rca_outcome

        assert row_id is not None
        row = get_rca_outcome(row_id)
        assert row is not None
        assert row["predicted_root_cause"] == "dns failure"
        assert row["human_corrected_root_cause"] == "pool exhausted"

    def test_recording_survives_a_storage_failure(self, monkeypatch):
        import aiops.state.repository as repo

        monkeypatch.setattr(
            repo, "save_rca_outcome", lambda **_: (_ for _ in ()).throw(RuntimeError("disk"))
        )
        assert memory.record_outcome(_outcome()) is None


# ─── helpers ────────────────────────────────────────────────────────────────


def _observed_facts():
    from agents.rca_agent.investigation.facts import FiringAlert, ObservedFacts

    return ObservedFacts(alerts=[FiringAlert(name="PaymentRedisDown")])


def _scored(matrix: EvidenceMatrix, forced: float | None) -> EvidenceMatrix:
    """A matrix with a score attached; ``forced`` overrides the arithmetic.

    Forcing is used only where the test is about ``_status_for``'s banding rather than
    about the scoring rules, so the numbers under test can be stated exactly instead of
    reverse-engineered out of rule combinations.
    """
    real = scoring.score(matrix)
    if forced is None:
        return matrix.model_copy(update={"score": real})
    return matrix.model_copy(update={"score": real.model_copy(update={"score": forced})})


def _unavailable_result():
    from aiops.tools.incident_history.base import RetrievalResult, RetrievalStatus

    return RetrievalResult(
        provider="rca_outcomes",
        status=RetrievalStatus.UNAVAILABLE,
        note="store unreadable",
    )
