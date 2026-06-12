"""Tests for the KB-article state + dedup/RAG retrieval (PRS-007).

Covers the persistence layer (save/get/list/status transitions, the
incident-id idempotency guard) and the cosine nearest-K used for both dedup
("is a near-identical article already here?") and v0 RAG retrieval.
"""

from __future__ import annotations

import math

import pytest

from aiops import state as state_pkg
from aiops.state import repository as repo


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _unit(vec: list[float]) -> list[float]:
    """L2-normalize so the stored vectors satisfy the dot-product==cosine
    contract that nearest_kb_articles relies on."""
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


# ─── save / get / idempotency ────────────────────────────────────────────────


def test_save_and_get_roundtrip():
    aid = repo.save_kb_article(
        title="Product catalog latency",
        body="redacted body",
        incident_id="INC-1",
        summary="latency",
        service="productcatalogservice",
        tags=["latency", "flagd"],
        quality_score=0.8,
        related_runbook_id="rb-product-catalog-latency",
    )
    assert aid > 0
    got = repo.get_kb_article(aid)
    assert got["title"] == "Product catalog latency"
    assert got["incident_id"] == "INC-1"
    assert got["tags"] == ["latency", "flagd"]
    assert got["status"] == "pending_review"  # default
    assert got["related_runbook_id"] == "rb-product-catalog-latency"


def test_find_kb_by_incident_id_returns_most_recent():
    repo.save_kb_article(title="v1", body="b", incident_id="INC-9")
    repo.save_kb_article(title="v2", body="b", incident_id="INC-9")
    found = repo.find_kb_by_incident_id("INC-9")
    assert found is not None
    assert found["title"] == "v2"  # newest


def test_find_kb_by_incident_id_none_when_absent_or_empty():
    assert repo.find_kb_by_incident_id("nope") is None
    assert repo.find_kb_by_incident_id("") is None


def test_list_filters_by_status_and_service():
    repo.save_kb_article(title="a", body="b", service="payment", status="published")
    repo.save_kb_article(title="c", body="b", service="payment", status="pending_review")
    repo.save_kb_article(title="d", body="b", service="cart", status="published")
    assert len(repo.list_kb_articles()) == 3
    assert len(repo.list_kb_articles(status="published")) == 2
    assert len(repo.list_kb_articles(service="payment")) == 2
    assert len(repo.list_kb_articles(status="published", service="payment")) == 1


def test_count_kb_articles():
    assert repo.count_kb_articles() == 0
    repo.save_kb_article(title="a", body="b")
    assert repo.count_kb_articles() == 1


# ─── status transitions (HITL review workflow) ───────────────────────────────


def test_update_kb_status_publishes_with_approval():
    aid = repo.save_kb_article(title="a", body="b", status="pending_review")
    before = repo.get_kb_article(aid)
    updated = repo.update_kb_status(
        aid, "published", approval_id="appr-123", approved_by="alice@x.io"
    )
    assert updated["status"] == "published"
    assert updated["approval_id"] == "appr-123"
    assert updated["approved_by"] == "alice@x.io"
    # updated_at advanced (or at least did not go backwards).
    assert updated["updated_at"] >= before["updated_at"]


def test_update_kb_status_missing_returns_none():
    assert repo.update_kb_status(99999, "published") is None


# ─── dedup / RAG nearest-K ───────────────────────────────────────────────────


def test_nearest_finds_near_duplicate_above_threshold():
    base = _unit([1.0, 0.1, 0.0])
    near = _unit([1.0, 0.12, 0.0])  # cosine ~1.0 with base
    far = _unit([0.0, 0.0, 1.0])  # orthogonal-ish

    repo.save_kb_article(title="orig", body="b", embedding=base, incident_id="INC-A")
    repo.save_kb_article(title="far", body="b", embedding=far, incident_id="INC-B")

    hits = repo.nearest_kb_articles(embedding=near, k=5, min_similarity=0.9)
    assert len(hits) == 1
    assert hits[0]["title"] == "orig"
    assert hits[0]["similarity"] > 0.9


def test_nearest_orders_by_similarity_desc():
    q = _unit([1.0, 0.0, 0.0])
    repo.save_kb_article(title="closest", body="b", embedding=_unit([1.0, 0.05, 0.0]))
    repo.save_kb_article(title="middle", body="b", embedding=_unit([1.0, 0.6, 0.0]))
    hits = repo.nearest_kb_articles(embedding=q, k=5, min_similarity=0.0)
    titles = [h["title"] for h in hits]
    assert titles.index("closest") < titles.index("middle")


def test_nearest_respects_min_similarity_and_status_filter():
    q = _unit([1.0, 0.0, 0.0])
    repo.save_kb_article(title="pub", body="b", embedding=q, status="published")
    repo.save_kb_article(title="rejected", body="b", embedding=q, status="rejected")
    # Restrict the candidate pool to published — rejected drafts excluded.
    hits = repo.nearest_kb_articles(embedding=q, min_similarity=0.99, statuses={"published"})
    assert [h["title"] for h in hits] == ["pub"]


def test_nearest_exclude_id_skips_self():
    q = _unit([1.0, 0.0, 0.0])
    aid = repo.save_kb_article(title="self", body="b", embedding=q)
    repo.save_kb_article(title="other", body="b", embedding=q)
    hits = repo.nearest_kb_articles(embedding=q, min_similarity=0.99, exclude_id=aid)
    assert [h["title"] for h in hits] == ["other"]


def test_nearest_empty_embedding_returns_empty():
    repo.save_kb_article(title="a", body="b", embedding=_unit([1.0, 0.0]))
    assert repo.nearest_kb_articles(embedding=[]) == []


def test_delete_all_kb_articles():
    repo.save_kb_article(title="a", body="b")
    repo.save_kb_article(title="c", body="b")
    assert repo.delete_all_kb_articles() == 2
    assert repo.count_kb_articles() == 0
