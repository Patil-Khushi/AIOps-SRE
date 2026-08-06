"""Tests for historical incident retrieval (Phase 7).

The brief's constraint is the sharp part: retrieve, do not infer. So a large share
of these tests assert what the output must *not* contain — no probable cause for
the current incident, no recommended action, no ranked hypothesis. A retrieval
layer that quietly recommends is worse than none, because the inference stops
being attributable.

The other emphasis is distinguishing "searched and found nothing" from "could not
search". An unconfigured vector store reporting an empty history would read as
"this has never happened before", a far stronger claim than the data supports.
"""

from __future__ import annotations

import pytest

from aiops.tools.incident_history import (
    IncidentMatch,
    ResolutionMetadata,
    RetrievalQuery,
    RetrievalStatus,
    jaccard,
    overlap,
    reset_for_tests,
    search_similar,
)
from aiops.tools.incident_history.base import token_jaccard, tokenize
from aiops.tools.incident_history.providers.backends import (
    ElasticIncidentHistoryProvider,
    PostgresIncidentHistoryProvider,
    VectorIncidentHistoryProvider,
)
from aiops.tools.incident_history.providers.mock import MockIncidentHistoryProvider


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _query(**kw) -> RetrievalQuery:
    base = {
        "service": "payment",
        "signatures": ["PaymentErrorRateHigh alert firing"],
        "services_involved": ["payment"],
        "topology": ["currency"],
    }
    base.update(kw)
    return RetrievalQuery(**base)


# ─── retrieval only: no inference, no decisions ──────────────────────────────


def test_match_has_no_field_asserting_the_current_cause():
    """Structural guarantee: there is nowhere to put a claim about *this*
    incident, so retrieval cannot become inference by accident."""
    fields = set(IncidentMatch.model_fields)
    for forbidden in (
        "probable_cause",
        "root_cause",
        "recommended_action",
        "suggested_fix",
        "hypothesis",
        "ranked_hypotheses",
        "verdict",
    ):
        assert forbidden not in fields, f"{forbidden} would turn retrieval into inference"


def test_resolution_metadata_is_history_not_advice():
    fields = set(ResolutionMetadata.model_fields)
    assert "recorded_cause" in fields, "a past incident's cause is a historical fact"
    for forbidden in ("recommended_action", "suggested_fix", "next_steps", "apply_fix"):
        assert forbidden not in fields


def test_recorded_cause_is_named_to_avoid_being_read_as_a_verdict():
    """``root_cause`` on a retrieval result invites a consumer to treat a past
    finding as the current answer."""
    assert "root_cause" not in ResolutionMetadata.model_fields
    assert "recorded_cause" in ResolutionMetadata.model_fields


# ─── similarity scoring ──────────────────────────────────────────────────────


def test_jaccard_normalises_by_union():
    """A raw shared count would let an incident with fifty recorded signatures
    outscore a precise two-signature match."""
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert jaccard(["a"], ["a", "b", "c", "d"]) == 0.25
    assert jaccard(["a"], ["z"]) == 0.0
    assert jaccard([], ["a"]) == 0.0


def test_overlap_reports_which_items_matched():
    assert overlap(["A", "b"], ["a", "c"]) == ["a"]


def test_tokenize_drops_stopwords_and_short_tokens():
    """Without this, "service" and "error" alone make every incident look similar
    to every other."""
    tokens = tokenize(["The payment service error rate is high"])
    assert "payment" in tokens
    for noise in ("the", "service", "error", "rate", "high"):
        assert noise not in tokens


def test_token_jaccard_matches_differently_worded_incidents():
    """Why token scoring exists: these describe the same event with no shared
    string, so exact matching scores zero."""
    a = ["Payment charge failed: payment service unavailable"]
    b = ["PaymentErrorRateHigh alert firing"]
    assert jaccard(a, b) == 0.0
    assert token_jaccard(a, b) > 0.0


# ─── mock provider over the real truth-file corpus ───────────────────────────


def test_mock_provider_searches_the_truth_files():
    result = MockIncidentHistoryProvider().search(_query())
    assert result.status in (RetrievalStatus.MATCHED, RetrievalStatus.EMPTY)
    assert result.corpus_size and result.corpus_size > 5, "truth files should be loaded"


def test_matches_report_why_they_matched():
    """A score with no explanation cannot be judged, only trusted."""
    result = MockIncidentHistoryProvider().search(_query())
    for m in result.matches:
        assert m.match_explanation
        assert "signatures=" in m.match_explanation


def test_every_required_field_is_present():
    result = MockIncidentHistoryProvider().search(_query(min_similarity=0.0))
    assert result.matches, "corpus should yield at least one scored match"
    m = result.matches[0]
    assert m.incident_id
    assert 0.0 <= m.similarity_score <= 1.0
    assert isinstance(m.matching_signatures, list)
    assert isinstance(m.matching_services, list)
    assert isinstance(m.matching_topology, list)
    assert m.resolution is not None


def test_results_are_ordered_by_score_and_deterministic():
    """Equal scores must not reorder between runs, or the same query renders
    differently each time."""
    first = MockIncidentHistoryProvider().search(_query(limit=10, min_similarity=0.0))
    scores = [m.similarity_score for m in first.matches]
    assert scores == sorted(scores, reverse=True)

    again = MockIncidentHistoryProvider().search(_query(limit=10, min_similarity=0.0))
    assert [m.incident_id for m in again.matches] == [m.incident_id for m in first.matches]


def test_limit_is_respected():
    result = MockIncidentHistoryProvider().search(_query(limit=2, min_similarity=0.0))
    assert len(result.matches) <= 2


def test_min_similarity_filters_noise():
    loose = MockIncidentHistoryProvider().search(_query(min_similarity=0.0, limit=50))
    strict = MockIncidentHistoryProvider().search(_query(min_similarity=0.9, limit=50))
    assert len(strict.matches) <= len(loose.matches)


def test_empty_result_reports_the_corpus_size():
    """A similarity score is uninterpretable without knowing what was searched."""
    result = MockIncidentHistoryProvider().search(
        _query(
            service="nonexistent-xyz",
            signatures=["zzzz"],
            services_involved=[],
            topology=[],
            min_similarity=0.99,
        )
    )
    assert result.status is RetrievalStatus.EMPTY
    assert result.corpus_size is not None
    assert "corpus" in (result.note or "")


# ─── the three real backends: unconfigured is not empty ──────────────────────


@pytest.mark.parametrize(
    ("provider", "env"),
    [
        (VectorIncidentHistoryProvider(), "AIOPS_VECTOR_DB_URL"),
        (ElasticIncidentHistoryProvider(), "AIOPS_ELASTIC_URL"),
        (PostgresIncidentHistoryProvider(), "AIOPS_INCIDENT_DB_URL"),
    ],
)
def test_unconfigured_backend_is_unavailable_never_empty(provider, env, monkeypatch):
    """The distinction that matters most: an empty history read off a database
    that was never connected would claim "this has never happened before"."""
    monkeypatch.delenv(env, raising=False)
    result = provider.search(_query())

    assert result.status is RetrievalStatus.UNAVAILABLE
    assert result.note
    healthy, detail = provider.health()
    assert healthy is False
    assert env in detail


def test_elastic_query_uses_should_not_must(monkeypatch):
    """Requiring every signature would return nothing on real data."""
    monkeypatch.setenv("AIOPS_ELASTIC_URL", "http://es:9200")
    body = ElasticIncidentHistoryProvider().build_query(_query())
    assert "should" in body["query"]["bool"]
    assert "must" not in body["query"]["bool"]


def test_postgres_query_is_parameterised(monkeypatch):
    """Signatures come from log text; interpolation here would be an injection
    path from a log line straight into the database."""
    monkeypatch.setenv("AIOPS_INCIDENT_DB_URL", "postgresql://x/y")
    sql, params = PostgresIncidentHistoryProvider().build_query(_query())
    assert "%(signatures)s" in sql
    assert "%(service)s" in sql
    assert params["service"] == "payment"
    assert "PaymentErrorRateHigh" not in sql, "no query text interpolated into SQL"


# ─── chain behaviour ─────────────────────────────────────────────────────────


def test_default_chain_is_mock_only():
    """Real backends must be opt-in: a default that reaches for an absent
    database adds latency to every correlation to learn nothing."""
    from aiops.tools.incident_history import retriever

    assert retriever._chain() == (["mock"], [])


def test_chain_falls_through_unavailable_tiers(monkeypatch):
    monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "vector,elastic,mock")
    monkeypatch.delenv("AIOPS_VECTOR_DB_URL", raising=False)
    monkeypatch.delenv("AIOPS_ELASTIC_URL", raising=False)

    attempts = search_similar(_query(min_similarity=0.0))

    assert [a.provider for a in attempts] == ["vector", "elastic", "mock"]
    assert attempts[0].status is RetrievalStatus.UNAVAILABLE
    assert attempts[1].status is RetrievalStatus.UNAVAILABLE


def test_all_attempts_are_returned_not_just_the_winner():
    """A caller must distinguish "the vector store was down and the static corpus
    answered" from "the vector store answered"."""
    attempts = search_similar(_query(min_similarity=0.0))
    assert isinstance(attempts, list)
    assert all(a.provider for a in attempts)


def test_unknown_provider_name_is_recorded_not_silently_dropped(monkeypatch):
    """An unrecognised name is a coverage hole, so it reaches the caller.

    Previously it was dropped with only a log warning. This seam did not
    mis-report as a result — its fallback keys off the absence of ``EMPTY``
    attempts rather than a completeness flag — but that safety was incidental to
    how the caller happened to be written. Surfacing the name makes it structural,
    and matches ``change_context``, where the same silent drop produced an
    authoritative "nothing changed" from a chain that asked nobody.
    """
    monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "bogus,mock")
    attempts = search_similar(_query(min_similarity=0.0))

    assert [a.provider for a in attempts] == ["bogus", "mock"]
    bogus = attempts[0]
    assert bogus.status is RetrievalStatus.UNAVAILABLE
    assert "unknown provider name" in (bogus.note or "")
    assert bogus.matches == [], "an unknown provider contributes no evidence"


def test_provider_exception_is_contained_and_chain_continues(monkeypatch):
    from aiops.tools.incident_history import retriever

    class _Boom:
        name = "boom"

        def health(self):
            return True, "ok"

        def search(self, query):
            raise RuntimeError("exploded")

    retriever.register_provider(_Boom())
    monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "boom,mock")

    attempts = search_similar(_query(min_similarity=0.0))
    boom = next(a for a in attempts if a.provider == "boom")

    assert boom.status is RetrievalStatus.FAILED
    assert "RuntimeError" in (boom.error or "")
    assert any(a.provider == "mock" for a in attempts), "chain continues past a failure"


def test_health_check_exception_is_treated_as_unavailable(monkeypatch):
    from aiops.tools.incident_history import retriever

    class _BadHealth:
        name = "badhealth"

        def health(self):
            raise RuntimeError("probe failed")

        def search(self, query):
            raise AssertionError("must not be searched when unhealthy")

    retriever.register_provider(_BadHealth())
    monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "badhealth,mock")

    attempts = search_similar(_query(min_similarity=0.0))
    bad = next(a for a in attempts if a.provider == "badhealth")
    assert bad.status is RetrievalStatus.UNAVAILABLE


# ─── models ──────────────────────────────────────────────────────────────────


def test_match_is_immutable():
    m = IncidentMatch(incident_id="x", similarity_score=0.5)
    with pytest.raises(Exception):
        m.similarity_score = 0.9


def test_similarity_score_is_bounded():
    with pytest.raises(Exception):
        IncidentMatch(incident_id="x", similarity_score=1.5)


# ─── agent integration ───────────────────────────────────────────────────────


def test_retrieval_is_opt_in(monkeypatch):
    """Disabled means ``None`` — not attempted — which is distinct from an empty
    match list meaning searched-and-found-nothing."""
    from agents.log_correlation import history

    monkeypatch.setattr(history, "_ENABLED", False)
    assert history.retrieve_similar("payment", ["sig"], ["currency"]) is None


def test_enabled_retrieval_returns_provenance(monkeypatch):
    from agents.log_correlation import history

    monkeypatch.setattr(history, "_ENABLED", True)
    monkeypatch.setattr(history, "_MIN_SIMILARITY", 0.0)
    result = history.retrieve_similar(
        "payment", ["PaymentErrorRateHigh alert firing"], ["currency"]
    )

    assert result is not None
    assert result.providers_attempted == ["mock"]
    assert result.corpus_size


def test_retrieval_failure_is_reported_not_swallowed(monkeypatch):
    from agents.log_correlation import history

    def _boom(_query):
        raise RuntimeError("retrieval exploded")

    monkeypatch.setattr(history, "_ENABLED", True)
    monkeypatch.setattr(history, "search_similar", _boom)
    result = history.retrieve_similar("payment", ["sig"], [])

    assert result is not None
    assert result.matches == []
    assert "RuntimeError" in (result.coverage_note or "")


def test_existing_outputs_unchanged_with_history_enabled(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from agents.log_correlation import CorrelationInput, correlate, history

    monkeypatch.setattr(history, "_ENABLED", True)
    end = datetime.now(UTC)
    r = correlate(
        CorrelationInput(
            service="checkout",
            window={"start": (end - timedelta(minutes=15)).isoformat(), "end": end.isoformat()},
        ),
        force_synthetic=True,
    )

    assert r.confidence == 0.9, "the eval-asserted score must not move"
    assert r.suspected_dependencies == ["payment"]
    assert len(r.timeline) == 3
    assert r.evidence
