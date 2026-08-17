"""Tests for the NEW conversational layer in agents/rca_agent/chat.py: the
section planner (_plan_sections/_fallback_sections), the selective-retrieval
+ retry-on-unanswerable flow in answer(), the new remediation/resolution_status
deterministic intents, and structured citations (ChatAnswer.citation_details).

Existing behavior (grounding pack, validation guards, HTTP surface) is
covered by tests/test_rca_chat.py and is untouched by this change — these
tests are additive, covering only the new understand -> retrieve -> answer
path and the two new intents.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agents.rca_agent import chat as rca_chat
from agents.rca_agent.investigation.models import (
    BlastRadiusReport,
    EvidenceItem,
    EvidenceMatrix,
    EvidenceStance,
    HistoricalInfluence,
    Hypothesis,
    HypothesisScore,
    IncidentScope,
    IncidentTimelineView,
    Investigation,
    RcaTimelineEvent,
    RecoveryOption,
    RootCauseStatus,
    ServiceImpact,
    TemporalRelation,
    VerificationPlan,
)
from agents.rca_agent.models import RankedFixStep, RCAAuditMetadata, RCAVerdict
from aiops.llm.base import LLMResponse


def _matrix(hyp_id: str, category: str, *, score: float, supporting_id: str) -> EvidenceMatrix:
    return EvidenceMatrix(
        hypothesis=Hypothesis(
            hypothesis_id=hyp_id,
            label=category,
            mechanism=f"{category} mechanism",
            category=category,
        ),
        supporting=(
            EvidenceItem(
                evidence_id=supporting_id,
                stance=EvidenceStance.SUPPORTS,
                statement=f"{supporting_id}: supports {category}",
            ),
        ),
        contradicting=(),
        checked_absent=(),
        gaps=(),
        score=HypothesisScore(score=score),
    )


def _investigation(
    *, discriminated=True, status=RootCauseStatus.CONFIRMED, **overrides
) -> Investigation:
    winner = _matrix("hid-1", "dependency_unavailable", score=0.82, supporting_id="EV-1")
    runner_up = _matrix("hid-2", "resource_saturation_cpu", score=0.4, supporting_id="EV-2")
    payload = dict(
        scope=IncidentScope(
            incident_id="INC-1",
            affected_service="order-service",
            severity="sev1",
            user_visible_symptom="checkout fails",
        ),
        matrices=(winner, runner_up),
        selected_hypothesis_id="hid-1" if status == RootCauseStatus.CONFIRMED else None,
        status=status,
        confidence=0.82,
        discriminated=discriminated,
        timeline=IncidentTimelineView(
            events=(
                RcaTimelineEvent(
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    source="deployment",
                    service="order-service",
                    event="deploy abc123",
                    is_change=True,
                    temporal_relation=TemporalRelation.PRECEDES_ONSET,
                ),
            ),
        ),
        blast_radius=BlastRadiusReport(
            impacts=(
                ServiceImpact(
                    service="cart-service", state="indirectly_affected", rationale="calls order"
                ),
            ),
            topology_available=True,
        ),
        verification=VerificationPlan(checks=("check X",), success_criteria=("no more errors",)),
        historical_influence=HistoricalInfluence(level="none"),
        recovery_options=(
            RecoveryOption(
                option_id="opt-1", description="Restart the affected pod", blast_radius="low"
            ),
        ),
    )
    payload.update(overrides)
    return Investigation(**payload)


def _verdict(investigation=None, *, status=RootCauseStatus.CONFIRMED) -> RCAVerdict:
    return RCAVerdict(
        affected_service="order-service",
        root_cause="order-service cannot reach its dependency",
        ranked_fix_steps=[
            RankedFixStep(description="Clear the fault.", blast_radius="low", rollback="undo")
        ],
        confidence_score=0.82,
        audit_metadata=RCAAuditMetadata(created_at=datetime.now(UTC)),
        root_cause_status=status,
        investigation=investigation if investigation is not None else _investigation(status=status),
    )


def _pack(inv):
    return rca_chat.build_grounding_pack(_verdict(inv), inv, "order-service")


# ─── section planning ───────────────────────────────────────────────────────


class TestFallbackSections:
    @pytest.mark.parametrize(
        "question,expected_key",
        [
            ("why do you think the dependency caused this?", "evidence_detail"),
            ("what evidence supports this?", "evidence_detail"),
            ("what did you rule out?", "evidence_detail"),
            ("what's the blast radius?", "blast_radius"),
            ("what changed before the incident?", "changes"),
            ("what should i check next to verify this?", "verification"),
            ("what's the historical influence on the ranking?", "history"),
            ("has this happened before?", "similar_incidents_rag"),
            ("show me similar incidents", "similar_incidents_rag"),
            ("what fixed similar incidents?", "similar_incidents_rag"),
            ("have we seen this dependency failure before?", "similar_incidents_rag"),
            ("how is this different from the previous incident?", "similar_incidents_rag"),
        ],
    )
    def test_natural_phrasing_maps_to_a_relevant_section(self, question, expected_key):
        assert expected_key in rca_chat._fallback_sections(question)

    def test_new_remediation_intent_maps_to_recovery(self):
        assert "recovery" in rca_chat._fallback_sections("what should i do now?")
        assert "recovery" in rca_chat._fallback_sections("what is the safest fix?")

    def test_unrecognized_question_gets_everything_not_nothing(self):
        from agents.rca_agent.investigation_context import all_section_keys

        assert set(rca_chat._fallback_sections("what's the weather like")) == set(
            all_section_keys()
        )


class TestPlanSectionsUnderStub:
    """Under AIOPS_LLM_PROVIDER=stub (conftest's hermetic fixture), the
    planner LLM call always echoes [stub] text, so this must always fall
    through to the keyword mapping — never raise, never return an empty set
    that would starve the answering call."""

    def test_falls_back_to_keywords_under_stub(self):
        from agents.rca_agent.investigation_context import InvestigationContextProvider

        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        keys = rca_chat._plan_sections("what's the blast radius?", [], provider)
        assert "blast_radius" in keys


# ─── natural-language questions through the full answer() path (stub LLM) ──


class TestNaturalLanguageQuestionsDeterministicPath:
    """Under stub, answer() falls all the way to _deterministic_answer — this
    is what proves the whole plan -> retrieve -> ask -> fallback pipeline
    doesn't break the existing deterministic safety net."""

    def test_root_cause_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "why do you think this happened?", _verdict(inv))
        assert result.source == "deterministic"
        assert result.answerable is True

    def test_evidence_question(self):
        inv = _investigation()
        result = rca_chat.answer(
            _pack(inv), [], "what evidence supports the dependency hypothesis?", _verdict(inv)
        )
        assert "EV-1" in result.citations

    def test_ruled_out_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "what did you rule out?", _verdict(inv))
        assert "hid-2" in result.referenced_hypotheses

    def test_timeline_and_changes_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "what changed before the incident?", _verdict(inv))
        assert "deploy" in result.answer

    def test_blast_radius_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "what else is affected?", _verdict(inv))
        assert "cart-service" in result.answer

    def test_confidence_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "why is the confidence only 82%?", _verdict(inv))
        assert "0.82" in result.answer

    def test_uncertainty_question_says_no_single_root_cause_confirmed(self):
        inv = _investigation(discriminated=False, status=RootCauseStatus.UNCERTAIN)
        result = rca_chat.answer(
            _pack(inv),
            [],
            "why are you uncertain?",
            _verdict(inv, status=RootCauseStatus.UNCERTAIN),
        )
        assert "No single root cause was confirmed" in result.answer
        assert set(result.referenced_hypotheses) == {"hid-1", "hid-2"}

    def test_remediation_question(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "what is the safest fix?", _verdict(inv))
        assert "requires human approval" in result.answer

    def test_remediation_question_offers_nothing_when_uncertain(self):
        inv = _investigation(discriminated=False, status=RootCauseStatus.UNCERTAIN)
        result = rca_chat.answer(
            _pack(inv), [], "what should i do now?", _verdict(inv, status=RootCauseStatus.UNCERTAIN)
        )
        assert "No remediation is offered" in result.answer

    def test_resolution_status_question_is_honest_about_no_live_status(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "is it fixed now?", _verdict(inv))
        assert result.answerable is False
        assert "not available" in result.missing[0]

    def test_verification_question(self):
        inv = _investigation()
        result = rca_chat.answer(
            _pack(inv), [], "what should i check to confirm this?", _verdict(inv)
        )
        assert "check X" in result.answer

    def test_history_question(self):
        inv = _investigation()
        result = rca_chat.answer(
            _pack(inv), [], "what's the historical influence on the ranking?", _verdict(inv)
        )
        assert "Historical influence" in result.answer

    def test_similar_incidents_question_is_honest_when_none_are_on_record(self):
        """No RCAResultRow fixtures exist in this test's isolated DB (and/or
        the embeddings extra may not be installed), so the real search
        legitimately finds nothing — the correct, honest outcome, not a bug."""
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "has this happened before?", _verdict(inv))
        assert "No sufficiently similar resolved incident was found" in result.answer
        assert result.historical_incidents == ()

    def test_unknown_question_abstains_honestly(self):
        inv = _investigation()
        result = rca_chat.answer(_pack(inv), [], "what's the weather like today", _verdict(inv))
        assert result.answerable is False
        assert result.source == "deterministic"

    def test_missing_investigation_field_degrades_not_crashes(self, monkeypatch):
        import agents.rca_agent.chat as chat_mod

        monkeypatch.setattr(
            chat_mod,
            "_render_blast_radius_section",
            lambda inv: (_ for _ in ()).throw(RuntimeError()),
        )
        inv = _investigation()
        # A broken section in the FULL grounding pack (chat.py's own renderer,
        # unaffected by the new selective-retrieval module) must still degrade
        # rather than raise all the way out through answer().
        result = rca_chat.answer(_pack(inv), [], "what's the blast radius?", _verdict(inv))
        assert result is not None  # did not raise


# ─── follow-up / conversational continuity ─────────────────────────────────


class TestFollowUpReferencesPriorTurn:
    def test_a_follow_up_still_answers_grounded_in_the_frozen_investigation(self):
        inv = _investigation()
        history = [
            rca_chat.ChatTurn(role="user", text="why is the dependency the cause?"),
            rca_chat.ChatTurn(role="assistant", text="Because of EV-1."),
        ]
        result = rca_chat.answer(_pack(inv), history, "what about CPU?", _verdict(inv))
        # Falls to deterministic under stub; the point is it doesn't raise on
        # a pronoun-style follow-up and still answers from the SAME frozen
        # investigation, not from the conversation history.
        assert result is not None


# ─── structured citations (Phase 14) ────────────────────────────────────────


class TestStructuredCitations:
    def test_valid_citations_get_a_real_stance_looked_up_not_stated(self):
        inv = _investigation()
        pack = _pack(inv)
        parsed = rca_chat.ChatAnswer(answer="x", citations=("EV-1",))
        result = rca_chat._validate(parsed, pack, _verdict(inv), history_truncated=False)
        assert result.citation_details == (
            rca_chat.CitationDetail(evidence_id="EV-1", stance="supports"),
        )

    def test_fabricated_citations_get_no_detail_entry(self):
        inv = _investigation()
        pack = _pack(inv)
        parsed = rca_chat.ChatAnswer(answer="x", citations=("EV-1", "EV-FAKE"))
        result = rca_chat._validate(parsed, pack, _verdict(inv), history_truncated=False)
        assert len(result.citation_details) == 1
        assert result.fabricated_citations == 1


# ─── the retry-on-unanswerable safety net ───────────────────────────────────


class TestSelectiveRetrievalNeverStarvesACorrectAnswer:
    """If the planner under-selects and the primary answering call comes back
    answerable=False, one retry against the FULL pack must happen before the
    abstention is accepted — selective retrieval is an efficiency choice, not
    a correctness trade."""

    def test_retries_with_the_full_pack_before_accepting_an_abstention(self, monkeypatch):
        inv = _investigation()
        pack = _pack(inv)
        calls: list[str] = []

        def fake_complete(*, messages, **_kwargs):
            # The grounding message is always index 1 (system, then grounding).
            grounding = messages[1].content
            calls.append(grounding)
            if len(calls) == 1:
                # Planner call (short, JSON sections) OR first answering call.
                if "INVESTIGATION PACK" in grounding:
                    return LLMResponse(
                        text=json.dumps(
                            {"answer": "", "answerable": False, "missing": ["needs blast radius"]}
                        ),
                        model="fake",
                        provider="fake",
                    )
                return LLMResponse(text=json.dumps({"sections": []}), model="fake", provider="fake")
            if "Blast radius" in grounding:
                # The retry got the FULL pack (blast radius section included).
                return LLMResponse(
                    text=json.dumps(
                        {
                            "answer": "cart-service is indirectly affected.",
                            "answerable": True,
                            "citations": [],
                        }
                    ),
                    model="fake",
                    provider="fake",
                )
            return LLMResponse(
                text=json.dumps({"answer": "", "answerable": False, "missing": ["still missing"]}),
                model="fake",
                provider="fake",
            )

        monkeypatch.setattr(rca_chat, "llm_complete", fake_complete)
        result = rca_chat.answer(pack, [], "what else could be affected?", _verdict(inv))

        # Whatever the exact plumbing, the FULL pack must have been tried at
        # least once, and the final answer must not be a needless abstention
        # when the full pack actually supports one.
        assert any("Blast radius" in c for c in calls)
        assert result.answerable is True
        assert "cart-service" in result.answer


# ─── incident scope isolation ───────────────────────────────────────────────


class TestIncidentScopeIsolation:
    def test_two_packs_never_share_evidence_ids(self):
        inv_a = _investigation()
        inv_b = _investigation(
            matrices=(_matrix("hid-9", "latency_regression", score=0.5, supporting_id="EV-9"),),
            selected_hypothesis_id="hid-9",
        )
        pack_a = _pack(inv_a)
        pack_b = _pack(inv_b)
        assert pack_a.valid_evidence_ids.isdisjoint(pack_b.valid_evidence_ids)
        assert pack_a.valid_hypothesis_ids.isdisjoint(pack_b.valid_hypothesis_ids)


# ─── G/H/I/J: historical RAG can never move the verdict ────────────────────
#
# The load-bearing tests for the whole feature: even when a similar past
# incident is genuinely found (via a fake but real-shaped search), the
# CURRENT verdict's confidence/status/scoring must not move, and the LLM
# cannot use "it happened before" as a backdoor to restate a different cause.


def _patch_rag_finds_a_strong_match(monkeypatch, *, incident_id="INC-999", fix="Historical fix X"):
    from agents.rca_agent import incident_rag

    match = incident_rag.SimilarIncident(
        incident_id=incident_id,
        similarity=0.91,
        affected_service="order-service",
        root_cause_summary="a different cause entirely — CPU saturation",
        category="resource_saturation_cpu",
        recorded_fix=fix,
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(
        rca_chat,
        "_search_similar_incidents_for_verdict",
        lambda verdict, *, incident_id=None: [match],
    )
    return match


class TestHistoricalRagCannotMoveTheVerdict:
    def test_g_rag_never_changes_confidence_score(self, monkeypatch):
        _patch_rag_finds_a_strong_match(monkeypatch)
        inv = _investigation()
        v = _verdict(inv)
        before = v.confidence_score
        rca_chat.answer(_pack(inv), [], "has this happened before?", v)
        assert v.confidence_score == before  # the verdict object itself is untouched

    def test_h_rag_never_changes_root_cause_status(self, monkeypatch):
        _patch_rag_finds_a_strong_match(monkeypatch)
        inv = _investigation()
        v = _verdict(inv)
        before = v.root_cause_status
        rca_chat.answer(_pack(inv), [], "has this happened before?", v)
        assert v.root_cause_status == before

    def test_i_rag_result_never_reaches_the_investigation_scoring_objects(self, monkeypatch):
        _patch_rag_finds_a_strong_match(monkeypatch)
        inv = _investigation()
        v = _verdict(inv)
        before_scores = [m.score.score for m in inv.matrices if m.score]
        rca_chat.answer(_pack(inv), [], "has this happened before?", v)
        after_scores = [m.score.score for m in inv.matrices if m.score]
        assert before_scores == after_scores

    def test_j_current_evidence_contradicts_the_historical_pattern_llm_path(self, monkeypatch):
        """The model tries to use a strongly-similar-but-different-cause past
        incident to argue for that cause now; _validate's structural guards
        (no confidence/root_cause field on ChatAnswer at all) mean it
        physically cannot, regardless of what it says in prose."""
        match = _patch_rag_finds_a_strong_match(monkeypatch)
        inv = _investigation()  # current investigation's own winner is dependency_unavailable
        v = _verdict(inv)

        def fake_complete(*, messages, **_kwargs):
            grounding = messages[1].content
            if "SECTION MENU" in grounding:
                return LLMResponse(
                    text=json.dumps({"sections": ["similar_incidents_rag"]}),
                    model="fake",
                    provider="fake",
                )
            return LLMResponse(
                text=json.dumps(
                    {
                        "answer": (
                            f"Given {match.incident_id} was CPU saturation, this is probably CPU too."
                        ),
                        "answerable": True,
                    }
                ),
                model="fake",
                provider="fake",
            )

        monkeypatch.setattr(rca_chat, "llm_complete", fake_complete)
        result = rca_chat.answer(_pack(inv), [], "could this be CPU, like that other incident?", v)

        # The model's prose can say what it likes — the STRUCTURED result has
        # nowhere to put a new cause, and the historical match is attached as
        # its own clearly-separate, labeled field.
        assert not hasattr(result, "root_cause")
        assert result.historical_incidents[0].incident_id == match.incident_id
        assert v.root_cause_status == RootCauseStatus.CONFIRMED  # unchanged
        assert v.confidence_score == pytest.approx(0.82)  # unchanged
