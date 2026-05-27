"""Unit tests for the alert_triage dedup state machine.

Stateful behavior — covered here rather than in the eval harness goldens,
which are stateless single-shot fixtures. See A8 for the goldens; this
file is its A10 follow-up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def test_delayed_alert_still_dedupes_within_wall_clock_window(clean_state):
    """An alert whose ``timestamp`` is older than the dedup window (e.g. a
    backfilled or out-of-order delivery) must still dedupe against a second
    copy that arrives moments later. Regression for the seen_at=alert.timestamp
    bug: it caused the freshly-created cluster's last_seen to land outside the
    wall-clock window, so evict_expired_clusters wiped it before the second
    alert could match.
    """
    from agents.alert_triage import run

    delayed_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    v1 = run(_alert_input(alert_id="ALT-DLY-1", timestamp=delayed_ts))
    v2 = run(_alert_input(alert_id="ALT-DLY-2", timestamp=delayed_ts))

    assert v1["status"] == "Active"
    assert v2["status"] == "Suppressed", v2["audit_metadata"]["decision_trace"]
    assert v2["duplicate_alert_count"] == 2


def test_embedding_similarity_match_suppresses_paraphrase(clean_state, monkeypatch):
    """Different cluster_key (labels differ) but identical embedding text →
    cosine similarity = 1.0 → embedding path suppresses the second alert.

    Uses a deterministic fake embedding model (same pattern as
    ``test_alert_triage_embedding_persistence``) so the test doesn't pay
    the 80MB sentence-transformers model load cost (#113). The
    ``tests/conftest.py`` autouse disables embeddings globally; this
    test reinstalls a fake so the embedding path is exercised end-to-end
    without the real model.

    Skips when ``numpy`` isn't installed (the ``embeddings`` extra carries
    it transitively via sentence-transformers; CI installs only ``dev`` +
    ``ui`` and so legitimately doesn't have it).  Matches the gating
    pattern in ``test_alert_triage_embedding_persistence``.
    """
    np = pytest.importorskip("numpy")

    from agents.alert_triage import agent as agent_mod
    from agents.alert_triage import run

    class _FakeEmbedModel:
        @staticmethod
        def encode(text: str, convert_to_numpy: bool = True):
            # Deterministic by text — identical text → identical vector.
            # Cosine sim = 1.0 on identical input, which is what the test
            # asserts. Padded to 32 dims so the cosine math has enough
            # numeric body to be stable.
            raw = [float(ord(c)) for c in (text * 4)[:32]]
            return np.asarray(raw, dtype=np.float32)

    monkeypatch.setattr(agent_mod, "_get_embed_model", lambda: _FakeEmbedModel())

    desc = "Payment service CPU usage above 80% threshold"

    # Same service+metric+value+description -> identical embedding text.
    # Different label sets -> different cluster_keys, forcing the embedding path.
    v1 = run(
        _alert_input(
            alert_id="ALT-E1",
            labels={"pod": "payment-aaa"},
            annotations={"description": desc},
        )
    )
    v2 = run(
        _alert_input(
            alert_id="ALT-E2",
            labels={"pod": "payment-bbb"},
            annotations={"description": desc},
        )
    )

    assert v1["status"] == "Active"
    assert v2["status"] == "Suppressed"
    trace = v2["audit_metadata"]["decision_trace"]
    assert any("embedding" in line.lower() for line in trace), trace


def test_triage_writes_exactly_one_verdict_row_per_call(clean_state):
    """#61 regression: each ``triage()`` call must produce exactly one
    row in ``verdicts``. Previously the agent saved once and the route
    handler saved again to capture an id, doubling the count.

    Asserts both that the returned id matches the persisted row id and
    that ``MAX(id)`` after N distinct calls equals N.
    """
    from agents.alert_triage import Alert
    from agents.alert_triage.agent import triage
    from aiops.state import repository as state_repo

    ids: list[int | None] = []
    for i in range(10):
        verdict, verdict_id = triage(Alert(**_alert_input(alert_id=f"ALT-COUNT-{i}")))
        assert verdict_id is not None, "save_verdict must succeed under clean_state"
        ids.append(verdict_id)

    # Each id should be unique and the highest should equal len(ids) since
    # the DB is fresh and nothing else has written.
    assert len(set(ids)) == 10
    assert max(ids) == 10  # type: ignore[type-var]

    # And the returned id must point at a real row.
    persisted = state_repo.get_verdict(ids[-1])  # type: ignore[arg-type]
    assert persisted is not None
    assert persisted["affected_service"] == verdict.affected_service


def test_idempotent_triage_returns_same_verdict_id(clean_state):
    """#61 + idempotency: a re-delivery of the same ``alert_id`` within
    the idempotency window must return the SAME ``verdict_id`` as the
    first call (not a fresh save)."""
    from agents.alert_triage import Alert
    from agents.alert_triage.agent import triage

    v1, id1 = triage(Alert(**_alert_input(alert_id="ALT-IDEMP")))
    v2, id2 = triage(Alert(**_alert_input(alert_id="ALT-IDEMP")))

    assert id1 == id2, "duplicate delivery should reuse the cached verdict's row id"
    assert v1.affected_service == v2.affected_service
