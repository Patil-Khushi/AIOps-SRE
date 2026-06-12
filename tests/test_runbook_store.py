"""Tests for the file-backed runbook library seam (aiops.runbooks).

Each test gets its own empty library directory via ``AIOPS_RUNBOOKS_DIR`` →
``tmp_path`` (same env-swap idiom as the state-DB tests in ``test_state.py``),
so the suite never touches the real ``data/runbooks`` and stays hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops import runbooks as rb
from aiops.runbooks import ReviewStatus, Runbook

# The shipped baseline lives in a tracked seed dir under the agent package.
REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = REPO_ROOT / "agents" / "knowledge_synthesizer" / "seed_runbooks"


@pytest.fixture(autouse=True)
def _hermetic_library(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path / "runbooks"))
    yield


def _make(runbook_id: str = "rb-test", service: str = "payment", **kw) -> Runbook:
    base = dict(
        id=runbook_id,
        title="Test runbook",
        service=service,
        tags=["latency", "flagd"],
        severity="Sev-2",
        status=ReviewStatus.PUBLISHED,
        body="## Symptoms\nThing is slow.\n",
    )
    base.update(kw)
    return Runbook(**base)


# ─── round-trip ──────────────────────────────────────────────────────────────


def test_save_and_get_roundtrip_preserves_all_fields():
    rb.save_runbook(_make(source_incident="INC-1", related_kb="kb-1"))
    got = rb.get_runbook("rb-test")
    assert got is not None
    assert got.id == "rb-test"
    assert got.title == "Test runbook"
    assert got.service == "payment"
    assert got.tags == ["latency", "flagd"]
    assert got.severity == "Sev-2"
    assert got.status is ReviewStatus.PUBLISHED
    assert got.source_incident == "INC-1"
    assert got.related_kb == "kb-1"
    assert "Thing is slow." in got.body


def test_get_missing_returns_none():
    assert rb.get_runbook("does-not-exist") is None


def test_status_string_in_frontmatter_coerces_to_enum(tmp_path):
    # Write a file by hand to prove the loader coerces a plain string status.
    lib = Path(rb.store._library_dir())  # type: ignore[attr-defined]
    (lib / "rb-hand.md").write_text(
        "---\nid: rb-hand\ntitle: Hand written\nservice: cart\n"
        "status: published\ntags: [a, b]\n---\n\n## Body\ntext\n",
        encoding="utf-8",
    )
    got = rb.get_runbook("rb-hand")
    assert got is not None
    assert got.status is ReviewStatus.PUBLISHED
    assert got.tags == ["a", "b"]


# ─── list / search ───────────────────────────────────────────────────────────


def test_list_is_sorted_by_id():
    rb.save_runbook(_make("rb-zeta"))
    rb.save_runbook(_make("rb-alpha"))
    ids = [r.id for r in rb.list_runbooks()]
    assert ids == ["rb-alpha", "rb-zeta"]


def test_search_by_service_normalizes_spelling():
    rb.save_runbook(_make("rb-pay", service="payment"))
    rb.save_runbook(_make("rb-cart", service="cart"))
    # "payment-service" / "paymentservice" must match "payment".
    hits = rb.search_runbooks(service="payment-service")
    assert [r.id for r in hits] == ["rb-pay"]


def test_search_by_query_matches_title_tags_body():
    rb.save_runbook(_make("rb-1", title="Cache miss latency", tags=["cache"]))
    rb.save_runbook(_make("rb-2", title="Error rate spike", tags=["errors"]))
    assert {r.id for r in rb.search_runbooks(query="cache")} == {"rb-1"}
    assert {r.id for r in rb.search_runbooks(query="slow")} == {"rb-1", "rb-2"}  # body


def test_search_by_status():
    rb.save_runbook(_make("rb-pub", status=ReviewStatus.PUBLISHED))
    rb.save_runbook(_make("rb-draft", status=ReviewStatus.DRAFT))
    assert {r.id for r in rb.search_runbooks(status="published")} == {"rb-pub"}
    assert {r.id for r in rb.search_runbooks(status=ReviewStatus.DRAFT)} == {"rb-draft"}


# ─── versioning ──────────────────────────────────────────────────────────────


def test_save_overwrites_same_id_without_duplicating():
    rb.save_runbook(_make("rb-x", title="v1"))
    rb.save_runbook(_make("rb-x", title="v2"))
    assert len(rb.list_runbooks()) == 1
    assert rb.get_runbook("rb-x").title == "v2"


def test_bump_version_increments_on_update():
    rb.save_runbook(_make("rb-x", version=1))
    saved = rb.save_runbook(_make("rb-x"), bump_version=True)
    assert saved.version == 2
    assert rb.get_runbook("rb-x").version == 2


def test_bump_version_on_new_runbook_keeps_its_version():
    saved = rb.save_runbook(_make("rb-new", version=1), bump_version=True)
    assert saved.version == 1  # nothing to bump from


# ─── id safety ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../escape", "a/b", "with space", ""])
def test_invalid_id_is_rejected(bad):
    with pytest.raises(ValueError):
        rb.save_runbook(_make(bad))


# ─── seeding ─────────────────────────────────────────────────────────────────


def test_seed_from_dir_is_idempotent():
    rb.save_runbook(_make("rb-existing"))
    first = rb.seed_from_dir(SEED_DIR)
    second = rb.seed_from_dir(SEED_DIR)
    assert first == 5  # the five shipped seed runbooks
    assert second == 0  # already present — nothing re-written
    # The pre-existing runbook is untouched, plus the five seeds.
    assert len(rb.list_runbooks()) == 6


def test_ensure_seeded_only_seeds_empty_library():
    assert rb.ensure_seeded(SEED_DIR) == 5
    assert rb.ensure_seeded(SEED_DIR) == 0  # no longer empty


def test_seeded_runbooks_are_grounded_and_published():
    rb.seed_from_dir(SEED_DIR)
    by_id = {r.id: r for r in rb.list_runbooks()}
    assert {
        "rb-product-catalog-latency",
        "rb-payment-failure",
        "rb-cart-failure",
        "rb-recommendation-cache-failure",
        "rb-ad-failure",
    } <= set(by_id)
    pay = by_id["rb-payment-failure"]
    assert pay.status is ReviewStatus.PUBLISHED
    assert pay.source == "seed"
    assert pay.service == "payment"
    assert "paymentFailure" in pay.body  # real flagd flag, not invented


def test_delete_runbook():
    rb.save_runbook(_make("rb-del"))
    assert rb.delete_runbook("rb-del") is True
    assert rb.get_runbook("rb-del") is None
    assert rb.delete_runbook("rb-del") is False


def test_delete_all_runbooks():
    rb.save_runbook(_make("rb-1"))
    rb.save_runbook(_make("rb-2"))
    assert rb.delete_all_runbooks() == 2
    assert rb.list_runbooks() == []
