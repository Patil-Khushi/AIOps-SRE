"""Unit tests for the alert_triage dedup state machine.

Stateful behavior — covered here rather than in the eval harness goldens,
which are stateless single-shot fixtures. See A8 for the goldens; this
file is its A10 follow-up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Isolate dedup state per test: fresh SQLite DB + reset embedding cache.

    The agent's dedup uses two stores: a per-process embedding cache and a
    SQLite ``ClusterRow`` table. Both must be reset for a deterministic
    starting point.
    """
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


def test_first_alert_creates_new_cluster(clean_state):
    from agents.alert_triage import run

    v = run(_alert_input(alert_id="ALT-1"))

    assert v["status"] == "Active"
    assert v["duplicate_alert_count"] == 1
    trace = v["audit_metadata"]["decision_trace"]
    assert any("new alert cluster" in line for line in trace)


def test_exact_cluster_key_match_suppresses_second_alert(clean_state):
    """Same service + metric + labels → same cluster_key → second alert is suppressed."""
    from agents.alert_triage import run

    v1 = run(_alert_input(alert_id="ALT-A"))
    v2 = run(_alert_input(alert_id="ALT-B"))

    assert v1["status"] == "Active"
    assert v1["duplicate_alert_count"] == 1

    assert v2["status"] == "Suppressed"
    assert v2["duplicate_alert_count"] == 2
    trace = v2["audit_metadata"]["decision_trace"]
    assert any("exact" in line.lower() for line in trace), trace


def test_distinct_metrics_dont_collide(clean_state):
    """Same service, different metric → different cluster_key → both Active."""
    from agents.alert_triage import run

    v1 = run(_alert_input(alert_id="ALT-X", metric="CPU Usage"))
    v2 = run(_alert_input(alert_id="ALT-Y", metric="Memory Usage"))

    assert v1["status"] == "Active"
    assert v2["status"] == "Active"
    assert v1["duplicate_alert_count"] == 1
    assert v2["duplicate_alert_count"] == 1


def test_embedding_similarity_match_suppresses_paraphrase(clean_state):
    """Different cluster_key (labels differ) but identical embedding text →
    cosine similarity = 1.0 → embedding path suppresses the second alert.

    Requires the ``embeddings`` extra (sentence-transformers); skipped otherwise.
    """
    pytest.importorskip("sentence_transformers")
    from agents.alert_triage import run

    desc = "Payment service CPU usage above 80% threshold"

    # Same service+metric+value+description -> identical embedding text.
    # Different label sets -> different cluster_keys, forcing the embedding path.
    v1 = run(_alert_input(
        alert_id="ALT-E1",
        labels={"pod": "payment-aaa"},
        annotations={"description": desc},
    ))
    v2 = run(_alert_input(
        alert_id="ALT-E2",
        labels={"pod": "payment-bbb"},
        annotations={"description": desc},
    ))

    assert v1["status"] == "Active"
    assert v2["status"] == "Suppressed"
    trace = v2["audit_metadata"]["decision_trace"]
    assert any("embedding" in line.lower() for line in trace), trace
