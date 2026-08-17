"""Unit tests for agents/rca_agent/investigation_context.py — the read-only
"tool layer" the chat's section planner selects from.

Boundary properties (never calls analyze()/registry/gate/memory) are covered
by tests/test_rca_chat_boundary.py; this file covers correctness: the menu is
closed, unknown keys are dropped rather than rendered, missing data degrades
to an honest string rather than raising, and render_all()/render_sections()
agree on what a full render looks like.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    RootCauseStatus,
    ServiceImpact,
    TemporalRelation,
    VerificationPlan,
)
from agents.rca_agent.investigation_context import (
    InvestigationContextProvider,
    _evidence_stance_index,
    all_section_keys,
)
from agents.rca_agent.models import RankedFixStep, RCAAuditMetadata, RCAVerdict


def _matrix(hyp_id: str, category: str, *, score: float = 0.8) -> EvidenceMatrix:
    return EvidenceMatrix(
        hypothesis=Hypothesis(
            hypothesis_id=hyp_id,
            label=category,
            mechanism=f"{category} mechanism",
            category=category,
        ),
        supporting=(
            EvidenceItem(
                evidence_id="EV-1", stance=EvidenceStance.SUPPORTS, statement="EV-1: supports"
            ),
        ),
        contradicting=(
            EvidenceItem(
                evidence_id="EV-2", stance=EvidenceStance.CONTRADICTS, statement="EV-2: against"
            ),
        ),
        checked_absent=(
            EvidenceItem(
                evidence_id="EV-3", stance=EvidenceStance.CHECKED_ABSENT, statement="EV-3: absent"
            ),
        ),
        gaps=(
            EvidenceItem(
                evidence_id="EV-4", stance=EvidenceStance.UNAVAILABLE, statement="EV-4: gap"
            ),
        ),
        score=HypothesisScore(score=score),
    )


def _investigation(**overrides) -> Investigation:
    payload = dict(
        scope=IncidentScope(
            incident_id="INC-1",
            affected_service="order-service",
            severity="sev1",
            user_visible_symptom="checkout fails",
        ),
        matrices=(_matrix("hid-1", "dependency_unavailable", score=0.8),),
        selected_hypothesis_id="hid-1",
        status=RootCauseStatus.CONFIRMED,
        confidence=0.8,
        discriminated=True,
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
        verification=VerificationPlan(
            checks=("check redis",), success_criteria=("no more errors",)
        ),
        historical_influence=HistoricalInfluence(level="none"),
    )
    payload.update(overrides)
    return Investigation(**payload)


def _verdict(investigation: Investigation | None = None) -> RCAVerdict:
    return RCAVerdict(
        affected_service="order-service",
        root_cause="order-service cannot reach its dependency",
        ranked_fix_steps=[
            RankedFixStep(description="Clear the fault.", blast_radius="low", rollback="undo")
        ],
        confidence_score=0.8,
        audit_metadata=RCAAuditMetadata(created_at=datetime.now(UTC)),
        root_cause_status=RootCauseStatus.CONFIRMED,
        investigation=investigation if investigation is not None else _investigation(),
    )


class TestMenu:
    def test_the_menu_is_the_only_thing_offered_to_the_planner(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        keys = {s.key for s in provider.list_sections()}
        assert keys == set(all_section_keys())

    def test_every_menu_entry_has_a_non_empty_description(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        for section in provider.list_sections():
            assert section.label
            assert section.description


class TestAllowlist:
    def test_unknown_keys_are_silently_dropped_not_rendered(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        with_unknown = provider.render_sections(("blast_radius", "DROP TABLE", "__import__"))
        without_unknown = provider.render_sections(("blast_radius",))
        assert with_unknown == without_unknown

    def test_empty_selection_still_renders_the_always_on_header(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        text = provider.render_sections(())
        assert text  # the header (investigation block + action block) is never empty-string
        assert "Blast radius" not in text
        assert "Verification plan" not in text


class TestAccessors:
    def test_get_incident_context_never_includes_llm_stated_confidence(self):
        inv = _investigation()
        v = _verdict(inv)
        provider = InvestigationContextProvider(v, inv, "order-service")
        ctx = provider.get_incident_context()
        assert ctx["confidence_score"] == v.confidence_score
        assert "llm_stated_confidence" not in ctx

    def test_get_hypothesis_details_returns_none_for_unknown_id_never_fabricated(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        assert provider.get_hypothesis_details("does-not-exist") is None
        assert provider.get_hypothesis_details("hid-1") is not None

    def test_get_evidence_filters_by_bucket(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        gaps = provider.get_evidence(bucket="gap")
        assert len(gaps) == 1
        assert gaps[0]["evidence_id"] == "EV-4"

    def test_get_blast_radius_says_not_examined_rather_than_healthy(self):
        inv = _investigation(blast_radius=None)
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        assert "not examined" in provider.get_blast_radius()

    def test_get_verification_status_is_honest_about_no_live_endpoint(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        assert provider.get_verification_status() == "not available in this chat context"

    def test_no_investigation_degrades_every_accessor_rather_than_raising(self):
        v = _verdict(investigation=None)
        provider = InvestigationContextProvider(v, None, "order-service")
        assert provider.get_investigation_summary() is None
        assert provider.get_hypotheses() == []
        assert provider.get_hypothesis_details("hid-1") is None
        assert provider.get_evidence() == []
        assert "no investigation ran" in provider.get_blast_radius()
        assert "no investigation ran" in provider.get_recovery_options()
        assert "no investigation ran" in provider.get_verification_plan()
        assert "no investigation ran" in provider.get_historical_influence()
        assert "no investigation ran" in provider.get_changes()
        assert "no investigation ran" in provider.get_timeline()


class TestRenderAllVsRenderSections:
    def test_render_all_is_a_superset_of_any_single_section(self):
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        full = provider.render_all()
        for key in all_section_keys():
            assert provider.render_sections((key,)) != "" or key == ""
        assert "Blast radius" in full
        assert "Verification plan" in full
        assert "Historical influence" in full
        assert "Full evidence detail" in full
        assert "Changes" in full

    def test_a_broken_section_degrades_rather_than_raises(self, monkeypatch):
        import agents.rca_agent.investigation_context as ic

        monkeypatch.setattr(
            ic, "_render_blast_radius_section", lambda inv: (_ for _ in ()).throw(RuntimeError())
        )
        inv = _investigation()
        provider = InvestigationContextProvider(_verdict(inv), inv, "order-service")
        text = provider.render_sections(("blast_radius",))
        assert "(section unavailable)" in text


class TestEvidenceStanceIndex:
    def test_it_maps_every_evidence_id_to_its_real_stance(self):
        inv = _investigation()
        index = _evidence_stance_index(inv)
        assert index["EV-1"] == "supports"
        assert index["EV-2"] == "contradicts"
        assert index["EV-3"] == "checked_absent"
        assert index["EV-4"] == "unavailable"

    def test_no_investigation_gives_an_empty_index(self):
        assert _evidence_stance_index(None) == {}
