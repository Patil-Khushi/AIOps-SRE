"""Tests for the semantic incident-history tier and the combined corpus.

Two things are being protected here:

1. **The corpus covers both truth-file families.** Retrieval returned nothing for
   every live incident because the only corpus loader understood the Astronomy
   Shop YAML files and the running SUT is the ecommerce app, whose truth files are
   JSON with a different schema. That is a silent failure — correct scoring over a
   population describing a different system — so it needs a test that fails loudly
   if either family stops loading.

2. **The honesty posture survives.** UNAVAILABLE (could not search) must never
   collapse into EMPTY (searched, found nothing), because the second reads as
   "this has never happened before" and would be acted on.

Model-dependent tests gate on ``importorskip`` so CI stays hermetic without the
``embeddings`` extra — same pattern as ``test_alert_triage_dedup``.
"""

from __future__ import annotations

import time

import pytest

from aiops.tools.incident_history.base import RetrievalQuery, RetrievalStatus
from aiops.tools.incident_history.providers import embedding as embed_mod
from aiops.tools.incident_history.providers.embedding import EmbeddingIncidentHistoryProvider


@pytest.fixture(autouse=True)
def _clean_module_state():
    from aiops.tools.incident_history import corpus as corpus_mod

    corpus_mod.reset_corpus_for_tests()
    embed_mod.reset_index_for_tests()
    yield
    corpus_mod.reset_corpus_for_tests()
    embed_mod.reset_index_for_tests()


def _query(**over) -> RetrievalQuery:
    base = {
        "service": "user-service",
        "signatures": ["EcommerceUserServiceCPUHigh", "cpu_percent > 90"],
        "services_involved": ["user-service", "mysql"],
        "topology": ["mysql"],
        "limit": 5,
        "min_similarity": 0.1,
    }
    base.update(over)
    return RetrievalQuery(**base)


# ─── corpus ──────────────────────────────────────────────────────────────────


def test_corpus_loads_both_truth_file_families():
    """The bug this whole tier existed to fix: only one family was ever loaded."""
    from aiops.tools.incident_history.corpus import load_corpus

    corpus = load_corpus()
    sources = {r["source"] for r in corpus}
    assert "otel-demo" in sources, "Astronomy Shop YAML truth files stopped loading"
    assert "ecommerce" in sources, (
        "ecommerce JSON truth files stopped loading — retrieval would score live "
        "incidents against a corpus describing a different application"
    )
    assert len(corpus) > 20, f"expected both families, got {len(corpus)} incident(s)"


def test_ecommerce_records_map_cause_and_remediation():
    """The JSON schema has no real_cause block, so its mapping is easy to break."""
    from aiops.tools.incident_history.corpus import load_corpus

    rec = next(r for r in load_corpus() if r["incident_id"] == "user_service_high_cpu")
    assert rec["recorded_cause"] == "Application CPU saturation"
    assert rec["resolution_summary"], "remediation must survive into resolution_summary"
    assert rec["services"] == ["user-service"]
    # Alertname + fault category + keywords + metric names all become signatures.
    assert "EcommerceUserServiceCPUHigh" in rec["signatures"]
    assert "resource_saturation_cpu" in rec["signatures"]


def test_corpus_incident_ids_are_unique():
    """Two truth files claiming one id would make retrieval non-deterministic."""
    from aiops.tools.incident_history.corpus import load_corpus

    ids = [r["incident_id"] for r in load_corpus()]
    assert len(ids) == len(set(ids)), f"duplicate incident ids: {sorted(ids)}"


def test_ecommerce_records_do_not_fabricate_a_date():
    """These truth files carry no timestamp; inventing one would make a synthetic
    corpus look like real operational history."""
    from aiops.tools.incident_history.corpus import load_corpus

    for rec in load_corpus():
        if rec["source"] == "ecommerce":
            assert rec["occurred_at"] is None


# ─── request-path safety ─────────────────────────────────────────────────────


def test_cold_search_returns_immediately_instead_of_loading_inline(monkeypatch):
    """A cold search must not load the model on the request path.

    The chain guards each provider at AIOPS_INCIDENT_HISTORY_TIMEOUT (3s) and the
    first load measured ~18.5s, so loading inline is not merely slow: it is
    cancelled, counted as a failure, and opens a 30s breaker on a tier that was
    only starting up.
    """
    # Stub the warm-up so the test never downloads a model.
    monkeypatch.setattr(embed_mod, "warm", lambda *a, **k: None)

    started = time.monotonic()
    result = EmbeddingIncidentHistoryProvider().search(_query())
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert elapsed_ms < 1000, f"cold search took {elapsed_ms:.0f}ms; must not block"
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.matches == []


def test_cold_search_is_unavailable_never_empty(monkeypatch):
    """EMPTY would assert the corpus was searched and held nothing similar."""
    monkeypatch.setattr(embed_mod, "warm", lambda *a, **k: None)
    result = EmbeddingIncidentHistoryProvider().search(_query())
    assert result.status is not RetrievalStatus.EMPTY
    assert result.corpus_size is None, "a search that did not happen has no population"


def test_broken_model_reports_the_reason_not_a_warming_promise(monkeypatch):
    """Once the load has genuinely failed, saying "retry shortly" would be a lie."""
    monkeypatch.setattr(embed_mod, "_MODEL", False)
    monkeypatch.setattr(embed_mod, "_UNAVAILABLE_REASON", "ImportError: no module")
    result = EmbeddingIncidentHistoryProvider().search(_query())
    assert result.status is RetrievalStatus.UNAVAILABLE
    assert "ImportError" in (result.note or "")
    assert "warming" not in (result.note or "").lower()


def test_search_never_raises(monkeypatch):
    """Contract: every failure mode is a status, so retrieval cannot break the
    correlation that asked for it."""
    monkeypatch.setattr(embed_mod, "is_ready", lambda: True)
    monkeypatch.setattr(
        embed_mod, "_get_model", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    result = EmbeddingIncidentHistoryProvider().search(_query())
    assert result.status is RetrievalStatus.FAILED
    assert "boom" in (result.error or "")


# ─── semantic behaviour (needs the embeddings extra) ─────────────────────────


def test_semantic_tier_matches_what_keyword_overlap_cannot():
    """The reason this tier exists.

    The mock scores set overlap, so it can only match incidents sharing literal
    tokens. Embeddings match meaning: at least one returned incident must share no
    signature with the query, which is a match keyword scoring cannot produce.
    """
    pytest.importorskip("sentence_transformers")
    embed_mod.warm(block=True, timeout=300)
    if not embed_mod.is_ready():
        pytest.skip("embedding model could not be loaded in this environment")

    result = EmbeddingIncidentHistoryProvider().search(_query())
    assert result.status is RetrievalStatus.MATCHED
    assert result.matches, "expected semantic matches over the combined corpus"
    assert result.corpus_size and result.corpus_size > 20

    ids = [m.incident_id for m in result.matches]
    assert "user_service_high_cpu" in ids, f"exact analogue missing from {ids}"
    assert any(not m.matching_signatures for m in result.matches), (
        "no token-free match found — this tier would add nothing over the mock"
    )


def test_scores_are_bounded_and_ordered():
    """similarity_score is a [0,1] field but cosine is [-1,1]; an opposed pair is
    not 'negatively similar', it is simply not similar."""
    pytest.importorskip("sentence_transformers")
    embed_mod.warm(block=True, timeout=300)
    if not embed_mod.is_ready():
        pytest.skip("embedding model could not be loaded in this environment")

    matches = EmbeddingIncidentHistoryProvider().search(_query()).matches
    scores = [m.similarity_score for m in matches]
    assert all(0.0 <= s <= 1.0 for s in scores), scores
    assert scores == sorted(scores, reverse=True), "matches must be ranked"


def test_semantic_floor_overrides_a_permissive_caller_floor():
    """Cosine and Jaccard are not the same scale. A caller floor of 0.1 — sensible
    for set overlap — would return most of the corpus as 'similar' under cosine."""
    pytest.importorskip("sentence_transformers")
    embed_mod.warm(block=True, timeout=300)
    if not embed_mod.is_ready():
        pytest.skip("embedding model could not be loaded in this environment")

    result = EmbeddingIncidentHistoryProvider().search(_query(min_similarity=0.0, limit=50))
    assert result.corpus_size and len(result.matches) < result.corpus_size, (
        "the semantic floor did not filter anything; a 0.0 caller floor should "
        "still be raised to AIOPS_INCIDENT_HISTORY_EMBED_FLOOR"
    )
    assert all(m.similarity_score >= embed_mod._SEMANTIC_FLOOR for m in result.matches)


def test_match_explanation_exposes_whether_overlap_supported_the_score():
    """A cosine number alone is unauditable — a reader needs to see that a 0.71
    match shares zero signatures to judge it."""
    pytest.importorskip("sentence_transformers")
    embed_mod.warm(block=True, timeout=300)
    if not embed_mod.is_ready():
        pytest.skip("embedding model could not be loaded in this environment")

    for m in EmbeddingIncidentHistoryProvider().search(_query()).matches:
        assert m.match_explanation and "cosine" in m.match_explanation
        assert m.provider == "embedding"
