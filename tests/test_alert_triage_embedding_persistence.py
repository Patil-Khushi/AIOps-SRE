"""Tests for persisted-embedding dedup (Bug 2) and EMA centroid (Bug 3).

These bypass the real SentenceTransformer dependency by monkeypatching
``_get_embed_model`` with a deterministic fake. That means they exercise the
agent's dedup math directly and don't need the ``embeddings`` extra installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


class _FakeEmbedModel:
    """Returns a fixed vector regardless of input. Lets dedup tests assert
    against deterministic similarity math without loading a real model."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def encode(self, text: str, convert_to_numpy: bool = True) -> Any:
        import numpy as np

        return np.asarray(self._vector, dtype=np.float32)


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    db_path = tmp_path / "test_state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db_path.as_posix()}")

    from aiops.state import init_db, reset_engine_for_tests

    reset_engine_for_tests()
    init_db()

    from agents.alert_triage.agent import reset_dedup_store

    reset_dedup_store()

    yield

    reset_engine_for_tests()


def _alert_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alert_id": "ALT-test",
        "service": "payment",
        "metric": "CPU Usage",
        "value": 90.0,
        "threshold": 80.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "labels": {"namespace": "otel-demo"},
        "annotations": {},
    }
    base.update(overrides)
    return base


def _patch_embed_model(monkeypatch, vector: list[float]) -> None:
    from agents.alert_triage import agent as agent_mod

    fake = _FakeEmbedModel(vector)
    monkeypatch.setattr(agent_mod, "_get_embed_model", lambda: fake)


# ─── Bug 2: cold-start after restart ───────────────────────────────────────


def test_embedding_dedup_survives_cache_reset(clean_state, monkeypatch):
    """Regression for Bug 2 (embedding cache cold window after restart).

    After clearing the in-memory cache — equivalent to a process restart —
    embedding-similarity dedup must still match a second alert against a
    previously-active cluster by reading the persisted centroid from SQLite.
    Before the fix, the second alert always landed as a new cluster because
    the in-memory cache was the only source of centroids.
    """
    pytest.importorskip("numpy")

    # Same vector for both alerts -> cosine = 1.0 -> guaranteed match.
    _patch_embed_model(monkeypatch, [1.0, 0.0, 0.0])

    from agents.alert_triage import run
    from agents.alert_triage.agent import reset_dedup_store

    # Alert A: distinct labels -> distinct cluster_key, forces embedding path
    # on the second alert below.
    v1 = run(_alert_input(alert_id="ALT-A", labels={"pod": "aaa"}))
    assert v1["status"] == "Active"

    # Simulate restart: in-memory cache wiped, SQLite row + persisted
    # embedding survive.
    reset_dedup_store()

    # Alert B: different labels -> different cluster_key. Must dedupe via
    # the embedding path using the persisted centroid.
    v2 = run(_alert_input(alert_id="ALT-B", labels={"pod": "bbb"}))

    assert v2["status"] == "Suppressed", v2["audit_metadata"]["decision_trace"]
    trace = v2["audit_metadata"]["decision_trace"]
    assert any("embedding" in line.lower() for line in trace), trace


# ─── Bug 3: EMA centroid anchoring ─────────────────────────────────────────


def test_ema_keeps_centroid_anchored_to_origin(clean_state, monkeypatch):
    """Regression for Bug 3 (cluster centroid drift).

    After an embedding-similarity match, the persisted centroid must be an
    EMA mix of the original and the new vector — NOT a wholesale overwrite.
    Before the fix, every match replaced the centroid with the latest vector,
    so a chain of near-matches could walk the cluster arbitrarily far from
    where it started, eventually merging unrelated alerts.
    """
    np = pytest.importorskip("numpy")

    v1 = [1.0, 0.0, 0.0]
    # Cosine(v1, v2) ~ 0.94 — comfortably above the 0.85 match threshold but
    # different enough that an EMA-vs-overwrite distinction is visible.
    v2 = [0.94, 0.34, 0.0]

    from agents.alert_triage import run
    from agents.alert_triage.agent import _DEDUP_WINDOW, reset_dedup_store
    from aiops.state import repository as state_repo

    # Seed the cluster with v1 (the "origin").
    _patch_embed_model(monkeypatch, v1)
    run(_alert_input(alert_id="ALT-EMA-1", labels={"pod": "aaa"}))

    # Drop the in-memory cache so the second alert exercises the read-through
    # path AND so we can assert against the freshly-persisted centroid.
    reset_dedup_store()
    _patch_embed_model(monkeypatch, v2)
    out = run(_alert_input(alert_id="ALT-EMA-2", labels={"pod": "bbb"}))

    assert out["status"] == "Suppressed", out["audit_metadata"]["decision_trace"]

    # Pull the persisted centroid back from SQLite.
    clusters = state_repo.list_active_clusters(_DEDUP_WINDOW)
    assert len(clusters) == 1, clusters
    centroid = np.asarray(clusters[0]["embedding"], dtype=np.float32)
    assert centroid.size > 0, "centroid not persisted"

    v1_norm = np.asarray(v1, dtype=np.float32)
    v1_norm = v1_norm / np.linalg.norm(v1_norm)
    v2_norm = np.asarray(v2, dtype=np.float32)
    v2_norm = v2_norm / np.linalg.norm(v2_norm)

    cos_to_v1 = float(np.dot(centroid, v1_norm))
    cos_to_v2 = float(np.dot(centroid, v2_norm))

    # EMA at alpha=0.2 mixes 0.8*v1 + 0.2*v2, so the centroid sits much closer
    # to v1 than to v2. The overwrite-latest bug would make centroid == v2,
    # giving cos_to_v2 = 1.0 > cos_to_v1.
    assert cos_to_v1 > cos_to_v2, (
        f"centroid drifted toward latest vector: "
        f"cos(centroid, v1)={cos_to_v1:.4f}, cos(centroid, v2)={cos_to_v2:.4f}"
    )

    # Sharper check: centroid must not be (effectively) v2 — that's the bug.
    assert not np.allclose(centroid, v2_norm, atol=1e-3), (
        "centroid is the latest vector — overwrite-latest behavior leaked back"
    )
