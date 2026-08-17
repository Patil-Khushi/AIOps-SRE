"""Tests for agents/rca_agent/incident_rag.py — the read-only Historical
Incident RAG search over persisted RCA verdicts.

Uses a fake embedding model (deterministic 2D unit vectors, so cosine
similarity is exactly controllable) rather than the real sentence-transformers
model — CI does not install the ``embeddings`` extra (CLAUDE.md), and these
tests should not depend on whether it happens to be present on a dev machine.
Boundary properties (never writes, never touches scoring/confidence, chat
cannot execute/promote/re-analyze) are covered by
tests/test_rca_chat_boundary.py; this file covers retrieval correctness.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from agents.rca_agent import incident_rag
from aiops.state import repository as state_repo


def _unit_vec(cosine_to_axis: float) -> tuple[float, float]:
    """A 2D unit vector whose dot product with (1, 0) is exactly
    ``cosine_to_axis`` — lets a test assert precise similarity scores."""
    cosine_to_axis = max(-1.0, min(1.0, cosine_to_axis))
    return (cosine_to_axis, math.sqrt(max(0.0, 1.0 - cosine_to_axis**2)))


class _FakeModel:
    """Deterministic stand-in for the real sentence-transformers model. Maps
    each exact input string to a pre-chosen vector; unmapped strings default
    to an orthogonal (zero-similarity) vector rather than raising, so a
    signature-construction change in the module under test surfaces as a
    similarity-score assertion failure, not a KeyError."""

    def __init__(self, vectors: dict[str, tuple[float, float]]):
        self._vectors = vectors

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        return [self._vectors.get(t, (0.0, 1.0)) for t in texts]


def _save(
    incident_id: str,
    *,
    service: str,
    root_cause: str,
    status: str = "confirmed",
    category: str | None = "dependency_unavailable",
    fix: str | None = "Restart the dependency",
    created_at: datetime | None = None,
) -> None:
    verdict: dict = {
        "affected_service": service,
        "root_cause": root_cause,
        "root_cause_status": status,
        "confidence_score": 0.8,
        "ranked_fix_steps": [{"description": fix, "blast_radius": "low", "rollback": "n/a"}]
        if fix
        else [],
    }
    if category:
        verdict["investigation"] = {
            "selected_hypothesis_id": "hid-1",
            "matrices": [{"hypothesis": {"hypothesis_id": "hid-1", "category": category}}],
        }
    state_repo.save_rca_result(incident_id=incident_id, verdict=verdict, affected_service=service)


@pytest.fixture()
def clean_rca_results():
    state_repo.delete_all_rca_results()
    yield
    state_repo.delete_all_rca_results()


def _patch_model(monkeypatch, vectors: dict[str, tuple[float, float]]) -> None:
    import aiops.tools.incident_history.providers.embedding as embed_mod

    monkeypatch.setattr(embed_mod, "get_shared_model", lambda: _FakeModel(vectors))


# ─── A: similar incident found ──────────────────────────────────────────────


def test_a_similar_incident_is_found_above_threshold(clean_rca_results, monkeypatch):
    query_text = incident_rag._signature_text(
        service="order-service",
        summary="order-service cannot reach postgres",
        category="dependency_unavailable",
    )
    _save("INC-100", service="order-service", root_cause="order-service cannot reach postgres")
    doc_text = incident_rag._signature_text(
        service="order-service",
        summary="order-service cannot reach postgres",
        category="dependency_unavailable",
    )
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_text: _unit_vec(1.0)})

    results = incident_rag.search_similar_incidents(
        service="order-service",
        summary="order-service cannot reach postgres",
        category="dependency_unavailable",
    )
    assert len(results) == 1
    assert results[0].incident_id == "INC-100"
    assert results[0].similarity == pytest.approx(1.0, abs=0.01)
    assert results[0].recorded_fix == "Restart the dependency"


# ─── B: multiple similar incidents ranked by similarity ────────────────────


def test_b_multiple_matches_are_ranked_best_first(clean_rca_results, monkeypatch):
    query_text = "QUERY"
    _save("INC-201", service="order-service", root_cause="cause A")
    _save("INC-202", service="order-service", root_cause="cause B")
    doc_a = incident_rag._signature_text(
        service="order-service", summary="cause A", category="dependency_unavailable"
    )
    doc_b = incident_rag._signature_text(
        service="order-service", summary="cause B", category="dependency_unavailable"
    )
    _patch_model(
        monkeypatch,
        {query_text: _unit_vec(1.0), doc_a: _unit_vec(0.60), doc_b: _unit_vec(0.90)},
    )
    # search_similar_incidents builds its own query text internally; force it
    # to equal "QUERY" by using a service/summary/category combo that maps to
    # it via a tiny monkeypatch of the signature builder for the query only.
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: (
            query_text
            if summary == "QUERY_SUMMARY"
            else doc_a
            if summary == "cause A"
            else doc_b
            if summary == "cause B"
            else ""
        ),
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="QUERY_SUMMARY", category=None, min_similarity=0.5
    )
    assert [r.incident_id for r in results] == ["INC-202", "INC-201"]
    assert results[0].similarity > results[1].similarity


# ─── C: similarity threshold rejects weak matches ──────────────────────────


def test_c_a_match_below_the_floor_is_excluded(clean_rca_results, monkeypatch):
    query_text = "QUERY"
    _save("INC-300", service="order-service", root_cause="unrelated cause")
    doc_text = incident_rag._signature_text(
        service="order-service", summary="unrelated cause", category="dependency_unavailable"
    )
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_text: _unit_vec(0.10)})
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: query_text if summary == "Q" else doc_text,
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="Q", category=None, min_similarity=0.55
    )
    assert results == []


def test_c_the_same_corpus_matches_once_the_floor_is_lowered(clean_rca_results, monkeypatch):
    query_text = "QUERY"
    _save("INC-301", service="order-service", root_cause="unrelated cause")
    doc_text = incident_rag._signature_text(
        service="order-service", summary="unrelated cause", category="dependency_unavailable"
    )
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_text: _unit_vec(0.10)})
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: query_text if summary == "Q" else doc_text,
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="Q", category=None, min_similarity=0.05
    )
    assert len(results) == 1


# ─── D: no similar incidents ────────────────────────────────────────────────


def test_d_no_persisted_incidents_at_all_returns_empty(clean_rca_results, monkeypatch):
    _patch_model(monkeypatch, {})
    results = incident_rag.search_similar_incidents(service="order-service", summary="anything")
    assert results == []


def test_d_embedding_model_unavailable_returns_empty_not_an_error(clean_rca_results, monkeypatch):
    import aiops.tools.incident_history.providers.embedding as embed_mod

    monkeypatch.setattr(embed_mod, "get_shared_model", lambda: None)
    _save("INC-400", service="order-service", root_cause="some cause")
    results = incident_rag.search_similar_incidents(service="order-service", summary="some cause")
    assert results == []


# ─── eligibility: only resolved (confirmed/probable) incidents are candidates ──


def test_uncertain_and_insufficient_evidence_incidents_are_never_candidates(
    clean_rca_results, monkeypatch
):
    query_text = "QUERY"
    _save("INC-500", service="order-service", root_cause="cause A", status="uncertain")
    _save("INC-501", service="order-service", root_cause="cause B", status="insufficient_evidence")
    _save("INC-502", service="order-service", root_cause="cause C", status="confirmed")
    doc_c = incident_rag._signature_text(
        service="order-service", summary="cause C", category="dependency_unavailable"
    )
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_c: _unit_vec(1.0)})
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: query_text if summary == "Q" else doc_c,
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="Q", min_similarity=0.5
    )
    assert [r.incident_id for r in results] == ["INC-502"]


def test_the_current_incident_excludes_itself(clean_rca_results, monkeypatch):
    query_text = "QUERY"
    _save("INC-600", service="order-service", root_cause="cause A")
    doc_a = incident_rag._signature_text(
        service="order-service", summary="cause A", category="dependency_unavailable"
    )
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_a: _unit_vec(1.0)})
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: query_text if summary == "Q" else doc_a,
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="Q", exclude_incident_id="INC-600", min_similarity=0.5
    )
    assert results == []


# ─── F: historical fix is recorded, never phrased as the current fix ──────


def test_f_recorded_fix_prefers_the_remediation_option_over_the_ranked_fix_step(
    clean_rca_results, monkeypatch
):
    query_text = "QUERY"
    verdict = {
        "affected_service": "order-service",
        "root_cause": "cause A",
        "root_cause_status": "confirmed",
        "confidence_score": 0.8,
        "ranked_fix_steps": [
            {"description": "generic step", "blast_radius": "low", "rollback": "n/a"}
        ],
        "remediation_options": [{"option_id": "opt-1", "description": "flip the feature flag"}],
        "recommended_option_id": "opt-1",
    }
    state_repo.save_rca_result(
        incident_id="INC-700", verdict=verdict, affected_service="order-service"
    )
    doc_a = incident_rag._signature_text(service="order-service", summary="cause A", category=None)
    _patch_model(monkeypatch, {query_text: _unit_vec(1.0), doc_a: _unit_vec(1.0)})
    monkeypatch.setattr(
        incident_rag,
        "_signature_text",
        lambda *, service, summary, category: query_text if summary == "Q" else doc_a,
    )

    results = incident_rag.search_similar_incidents(
        service="order-service", summary="Q", min_similarity=0.5
    )
    assert results[0].recorded_fix == "flip the feature flag"


def test_f_recorded_fix_is_never_asserted_as_the_current_fix_in_chat_prose():
    """Prompt-level guarantee: the system prompt explicitly forbids
    presenting a historical fix as the current one (see
    tests/test_rca_chat_prompt.py for the exact-substring ratchet); this test
    just re-confirms chat.py's own rendering helper phrases it historically."""
    from agents.rca_agent.chat import _render_similar_incidents_block

    match = incident_rag.SimilarIncident(
        incident_id="INC-800",
        similarity=0.9,
        affected_service="order-service",
        root_cause_summary="cause A",
        category=None,
        recorded_fix="flip the flag",
        occurred_at=None,
    )
    text = _render_similar_incidents_block([match])
    assert "Recorded fix: flip the flag." in text
    assert "the fix for this incident is" not in text.lower()


# ─── E: historical results are always clearly labeled ─────────────────────


def test_e_the_render_block_always_carries_the_historical_banner():
    from agents.rca_agent.chat import _HISTORICAL_BANNER, _render_similar_incidents_block

    assert _HISTORICAL_BANNER in _render_similar_incidents_block([])
    match = incident_rag.SimilarIncident(
        incident_id="INC-900",
        similarity=0.7,
        affected_service="x",
        root_cause_summary="y",
        category=None,
        recorded_fix=None,
        occurred_at=None,
    )
    assert _HISTORICAL_BANNER in _render_similar_incidents_block([match])


def test_e_empty_result_is_honest_not_silent(clean_rca_results, monkeypatch):
    from agents.rca_agent.chat import _render_similar_incidents_block

    text = _render_similar_incidents_block([])
    assert "No sufficiently similar resolved incident was found" in text
