"""Tests for the RCA chat: agents/rca_agent/chat.py (grounding pack, the two
answer paths, validation guards) and demo/ui/rca_chat_routes.py (the HTTP
surface, session rehydration, turn caps).

The deterministic-answerer tests are the substantive ones — under
``AIOPS_LLM_PROVIDER=stub`` (pinned by conftest's ``_hermetic_llm_provider``),
every answer must come from the real ``Investigation`` fixture, never from
``"[stub]"`` echo text.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

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
    RootCauseStatus,
    ServiceImpact,
    TemporalRelation,
    VerificationPlan,
)
from agents.rca_agent.models import RankedFixStep, RCAAuditMetadata, RCAVerdict

# ─── fixtures ────────────────────────────────────────────────────────────────


def _matrix(
    hyp_id: str, category: str, *, score: float = 0.8, supporting_id: str = "EV-1"
) -> EvidenceMatrix:
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
                statement=f"{supporting_id}: signal supporting {category}",
            ),
        ),
        contradicting=(
            EvidenceItem(
                evidence_id=f"{supporting_id}-c",
                stance=EvidenceStance.CONTRADICTS,
                statement=f"{supporting_id}-c: signal against {category}",
            ),
        ),
        gaps=(
            EvidenceItem(
                evidence_id=f"{supporting_id}-g",
                stance=EvidenceStance.UNAVAILABLE,
                statement=f"{supporting_id}-g: could not check a related signal",
            ),
        ),
        score=HypothesisScore(score=score),
    )


def _investigation(**overrides) -> Investigation:
    winner = _matrix("hid-1", "dependency_unavailable", score=0.82, supporting_id="EV-1")
    runner_up = _matrix("hid-2", "resource_saturation_cpu", score=0.4, supporting_id="EV-2")
    payload = dict(
        scope=IncidentScope(
            incident_id="INC-1",
            affected_service="payment-service",
            severity="sev2",
            user_visible_symptom="checkout requests are failing",
        ),
        matrices=(winner, runner_up),
        selected_hypothesis_id="hid-1",
        status=RootCauseStatus.CONFIRMED,
        confidence=0.82,
        discriminated=True,
        timeline=IncidentTimelineView(
            events=(
                RcaTimelineEvent(
                    timestamp=datetime(2026, 8, 3, 9, 55, tzinfo=UTC),
                    source="deployment",
                    service="payment-service",
                    event="deploy 4f2a1c",
                    is_change=True,
                    temporal_relation=TemporalRelation.PRECEDES_ONSET,
                ),
            ),
        ),
        blast_radius=BlastRadiusReport(
            impacts=(ServiceImpact(service="checkout-service", state="indirectly_affected", rationale="calls payment"),),
            topology_available=True,
        ),
        verification=VerificationPlan(
            checks=("redis_up returns 1",), success_criteria=("no more CONTRADICTS on redis_up",)
        ),
        historical_influence=HistoricalInfluence(level="none"),
    )
    payload.update(overrides)
    return Investigation(**payload)


def _verdict(investigation: Investigation | None = None, *, confidence: float = 0.82) -> RCAVerdict:
    return RCAVerdict(
        affected_service="payment-service",
        root_cause="payment-service cannot reach its Redis dependency",
        ranked_fix_steps=[
            RankedFixStep(description="Clear the fault.", blast_radius="low", rollback="undo")
        ],
        confidence_score=confidence,
        audit_metadata=RCAAuditMetadata(created_at=datetime.now(UTC)),
        root_cause_status=RootCauseStatus.CONFIRMED,
        investigation=investigation if investigation is not None else _investigation(),
    )


# ─── grounding pack ──────────────────────────────────────────────────────────


class TestGroundingPack:
    def test_it_collects_evidence_and_hypothesis_ids_from_every_matrix(self):
        pack = rca_chat.build_grounding_pack(_verdict(), _investigation(), "payment-service")
        assert "EV-1" in pack.valid_evidence_ids
        assert "EV-2" in pack.valid_evidence_ids
        assert "EV-1-c" in pack.valid_evidence_ids
        assert "EV-1-g" in pack.valid_evidence_ids
        assert "hid-1" in pack.valid_hypothesis_ids
        assert "hid-2" in pack.valid_hypothesis_ids

    def test_every_section_renders_against_a_full_fixture(self):
        """Catches a field rename in the (uncommitted, still-moving)
        investigation package — a renamed field must shrink one section to
        '(section unavailable)', not raise."""
        pack = rca_chat.build_grounding_pack(_verdict(), _investigation(), "payment-service")
        assert "(section unavailable)" not in pack.text
        assert "Timeline" in pack.text
        assert "Blast radius" in pack.text
        assert "Verification plan" in pack.text
        assert "Historical influence" in pack.text

    def test_a_broken_section_degrades_rather_than_raises(self, monkeypatch):
        monkeypatch.setattr(
            rca_chat, "_render_blast_radius_section", lambda inv: (_ for _ in ()).throw(RuntimeError())
        )
        pack = rca_chat.build_grounding_pack(_verdict(), _investigation(), "payment-service")
        assert "(section unavailable)" in pack.text

    def test_no_investigation_still_produces_a_pack(self):
        pack = rca_chat.build_grounding_pack(_verdict(investigation=None), None, "payment-service")
        assert pack.investigation is None
        assert pack.valid_evidence_ids == frozenset()


# ─── deterministic answerer ──────────────────────────────────────────────────


class TestDeterministicAnswerer:
    def _pack(self, inv: Investigation) -> rca_chat.GroundingPack:
        return rca_chat.build_grounding_pack(_verdict(inv), inv, "payment-service")

    def test_cause_question_cites_the_selected_hypothesis(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "why this cause?", history_truncated=False)
        assert result.source == "deterministic"
        assert "dependency_unavailable mechanism" in result.answer
        assert "EV-1" in result.citations
        assert result.referenced_hypotheses == ("hid-1",)

    def test_ruled_out_question_names_the_runner_up(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(
            pack, _verdict(inv), "why was resource_saturation_cpu ruled out?", history_truncated=False
        )
        assert "resource_saturation_cpu" in result.answer
        assert "hid-2" in result.referenced_hypotheses

    def test_gaps_question_lists_what_could_not_be_checked(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "what couldn't you check?", history_truncated=False)
        assert "EV-1-g" in result.citations

    def test_blast_radius_question_lists_impacts(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "what's the blast radius?", history_truncated=False)
        assert "checkout-service" in result.answer

    def test_blast_radius_not_examined_abstains_rather_than_says_healthy(self):
        inv = _investigation(blast_radius=None)
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "who else is affected?", history_truncated=False)
        assert result.answerable is False
        assert "not examined" in result.missing[0]

    def test_severity_question_states_the_scope_severity(self):
        """Severity is set once at triage time (Investigation.scope), never
        re-derived by a hypothesis — a question about it must answer directly
        from scope, not report the fact as unanswerable/needing reanalysis."""
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "what severity is this incident?", history_truncated=False)
        assert result.answerable is True
        assert "sev2" in result.answer

    def test_confidence_question_states_the_platform_number_verbatim(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv, confidence=0.82), "how confident are you?", history_truncated=False)
        assert "0.82" in result.answer
        assert "confirmed" in result.answer

    def test_unmatched_question_abstains_honestly(self):
        inv = _investigation()
        pack = self._pack(inv)
        result = rca_chat._deterministic_answer(pack, _verdict(inv), "what's the weather like", history_truncated=False)
        assert result.answerable is False
        assert result.source == "deterministic"
        # A real sentence, not an empty string the UI has to paper over — the
        # precise technical reason still lives in `missing`, for citations/logs.
        assert result.answer != ""
        assert "no model is configured" in result.missing[0]

    def test_no_investigation_abstains(self):
        pack = rca_chat.build_grounding_pack(_verdict(investigation=None), None, "payment-service")
        result = rca_chat._deterministic_answer(pack, _verdict(investigation=None), "why this cause?", history_truncated=False)
        assert result.answerable is False


# ─── validation guards ───────────────────────────────────────────────────────


class TestValidate:
    def test_unknown_citations_are_dropped_and_counted(self):
        inv = _investigation()
        pack = rca_chat.build_grounding_pack(_verdict(inv), inv, "payment-service")
        parsed = rca_chat.ChatAnswer(answer="x", citations=("EV-1", "EV-DOES-NOT-EXIST"))
        result = rca_chat._validate(parsed, pack, _verdict(inv), history_truncated=False)
        assert result.citations == ("EV-1",)
        assert result.fabricated_citations == 1

    def test_unknown_hypotheses_are_dropped(self):
        inv = _investigation()
        pack = rca_chat.build_grounding_pack(_verdict(inv), inv, "payment-service")
        parsed = rca_chat.ChatAnswer(answer="x", referenced_hypotheses=("hid-1", "hid-does-not-exist"))
        result = rca_chat._validate(parsed, pack, _verdict(inv), history_truncated=False)
        assert result.referenced_hypotheses == ("hid-1",)

    def test_a_divergent_stated_confidence_is_flagged_not_edited(self):
        inv = _investigation()
        pack = rca_chat.build_grounding_pack(_verdict(inv, confidence=0.82), inv, "payment-service")
        parsed = rca_chat.ChatAnswer(answer="I'd put this at 0.99 confidence.")
        result = rca_chat._validate(parsed, pack, _verdict(inv, confidence=0.82), history_truncated=False)
        assert "0.99" in result.answer  # untouched
        assert any("0.82" in w for w in result.warnings)

    def test_a_matching_stated_confidence_is_not_flagged(self):
        inv = _investigation()
        pack = rca_chat.build_grounding_pack(_verdict(inv, confidence=0.82), inv, "payment-service")
        parsed = rca_chat.ChatAnswer(answer="Roughly 0.82 confidence, matching the platform.")
        result = rca_chat._validate(parsed, pack, _verdict(inv, confidence=0.82), history_truncated=False)
        assert result.warnings == ()


# ─── HTTP surface ────────────────────────────────────────────────────────────


@pytest.fixture()
def client():
    import demo.ui.server as srv

    with TestClient(srv.app) as c:
        yield c


class TestChatRoute:
    def test_chat_without_a_session_and_no_verdict_is_404(self, client):
        r = client.post("/api/rca/chat", json={"run_id": "no-such-run", "message": "why?"})
        assert r.status_code == 404

    def test_rehydration_from_a_posted_verdict_then_answers_deterministically(self, client):
        verdict = _verdict()
        r = client.post(
            "/api/rca/chat",
            json={
                "run_id": "run-1",
                "message": "why this cause?",
                "rca_verdict": verdict.model_dump(mode="json"),
                "incident_id": "INC-1",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["message"]["answer"]["source"] == "deterministic"
        assert "[stub]" not in body["message"]["answer"]["answer"]
        assert body["verdict_snapshot"]["confidence_score"] == pytest.approx(0.82)
        assert body["verdict_snapshot"]["root_cause_status"] == "confirmed"

    def test_a_second_call_with_the_same_run_id_does_not_recreate_the_session(self, client):
        verdict = _verdict()
        body = {"run_id": "run-2", "rca_verdict": verdict.model_dump(mode="json"), "incident_id": "INC-2"}
        client.post("/api/rca/chat", json={**body, "message": "first question"})
        client.post("/api/rca/chat", json={**body, "message": "second question"})

        hist = client.get("/api/rca/chat/run-2")
        assert hist.status_code == 200
        roles = [m["role"] for m in hist.json()["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_chat_cannot_change_the_verdict(self, client, monkeypatch):
        """The load-bearing test: even when the model claims a different
        cause and a different confidence, the stored snapshot doesn't move."""
        import json

        from aiops.llm.base import LLMResponse

        def fake_complete(**_kwargs):
            return LLMResponse(
                text=json.dumps(
                    {
                        "answer": "Actually the real cause is a bad deploy, confidence 0.99.",
                        "answerable": True,
                    }
                ),
                model="fake",
                provider="fake",
            )

        monkeypatch.setattr(rca_chat, "llm_complete", fake_complete)

        verdict = _verdict(confidence=0.82)
        r = client.post(
            "/api/rca/chat",
            json={
                "run_id": "run-3",
                "message": "are you sure?",
                "rca_verdict": verdict.model_dump(mode="json"),
                "incident_id": "INC-3",
            },
        )
        assert r.status_code == 200
        body = r.json()
        # (a) the response has nowhere to put a new verdict
        assert "confidence_score" not in body["message"]["answer"]
        assert "root_cause" not in body["message"]["answer"]
        # (b) the snapshot is unchanged
        assert body["verdict_snapshot"]["confidence_score"] == pytest.approx(0.82)
        assert body["verdict_snapshot"]["root_cause"] == verdict.root_cause
        # (c) a mismatched stated number is flagged, not silently edited
        assert any("0.82" in w for w in body["message"]["answer"]["warnings"])
        # (d) a second read confirms the stored snapshot didn't move either
        hist = client.get("/api/rca/chat/run-3").json()
        assert hist["verdict_snapshot"]["confidence_score"] == pytest.approx(0.82)

    def test_history_endpoint_404s_for_an_unknown_run(self, client):
        assert client.get("/api/rca/chat/no-such-run").status_code == 404

    def test_by_incident_falls_back_to_the_persisted_verdict(self, client):
        from aiops.state.repository import save_rca_result

        verdict = _verdict()
        save_rca_result(incident_id="INC-9", verdict=verdict.model_dump(mode="json"))

        r = client.get("/api/rca/chat/by-incident/INC-9")
        assert r.status_code == 200
        assert r.json()["has_session"] is False
        assert r.json()["verdict"]["affected_service"] == "payment-service"

    def test_by_incident_prefers_a_live_session(self, client):
        verdict = _verdict()
        client.post(
            "/api/rca/chat",
            json={
                "run_id": "run-4",
                "message": "hi",
                "rca_verdict": verdict.model_dump(mode="json"),
                "incident_id": "INC-4",
            },
        )
        r = client.get("/api/rca/chat/by-incident/INC-4")
        assert r.json() == {"run_id": "run-4", "has_session": True, "verdict": r.json()["verdict"]}

    def test_by_incident_404s_when_nothing_exists(self, client):
        assert client.get("/api/rca/chat/by-incident/INC-nonexistent").status_code == 404

    def test_delete_drops_the_session(self, client):
        verdict = _verdict()
        client.post(
            "/api/rca/chat",
            json={"run_id": "run-5", "message": "hi", "rca_verdict": verdict.model_dump(mode="json")},
        )
        assert client.delete("/api/rca/chat/run-5").status_code == 200
        assert client.get("/api/rca/chat/run-5").status_code == 404

    def test_message_over_the_length_cap_is_rejected(self, client):
        verdict = _verdict()
        r = client.post(
            "/api/rca/chat",
            json={
                "run_id": "run-6",
                "message": "x" * 3000,
                "rca_verdict": verdict.model_dump(mode="json"),
            },
        )
        assert r.status_code == 422

    def test_max_turns_cap_is_enforced(self, client, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_CHAT_MAX_TURNS", "1")
        verdict = _verdict()
        body = {"run_id": "run-7", "rca_verdict": verdict.model_dump(mode="json")}
        r1 = client.post("/api/rca/chat", json={**body, "message": "first"})
        assert r1.status_code == 200
        r2 = client.post("/api/rca/chat", json={**body, "message": "second"})
        assert r2.status_code == 429

    def test_a_seeded_rca_run_creates_a_resolvable_session(self, client, monkeypatch):
        """POST /api/rca with a run_id seeds a session that the chat can
        immediately answer follow-ups against, with no rca_verdict payload
        needed on the chat call."""
        monkeypatch.setattr("agents.rca_agent.agent._fetch_change_evidence", lambda *a, **k: None)
        triage = {
            "affected_service": "order-service",
            "severity": "Sev-1",
            "alert_summary": "EcommercePostgresDown firing: postgres_connection_status at 0.0",
            "audit_metadata": {"created_at": "2026-08-03T10:00:00Z"},
        }
        run_id = "11111111-1111-1111-1111-111111111111"
        r = client.post(
            "/api/rca",
            json={"triage_verdict": triage, "run_id": run_id, "incident_id": "INC-seeded"},
        )
        assert r.status_code == 200

        chat_r = client.post("/api/rca/chat", json={"run_id": run_id, "message": "why this cause?"})
        assert chat_r.status_code == 200
