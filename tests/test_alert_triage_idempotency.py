"""Tests for transport-layer idempotency (Fragile #6).

The idempotency check is orthogonal to Stage 3 dedup:
- dedup handles multiple *different* alerts about the same condition (different
  alert_ids, same cluster_key) — those produce one Active + N Suppressed verdicts.
- idempotency handles the *same* alert delivered twice by the transport layer
  (webhook redelivery, Alertmanager retry) — that produces exactly one verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


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
        "alert_id": "ALT-ID-1",
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


def test_same_alert_id_returns_cached_verdict(clean_state):
    """Identical alert_id within the idempotency window: the second triage
    call must return the exact verdict the first one produced — same
    decision_trace, same status, same duplicate_alert_count. The pipeline
    must not re-run."""
    from agents.alert_triage import run
    from aiops.state import repository as state_repo

    v1 = run(_alert_input(alert_id="ALT-DUP"))
    v2 = run(_alert_input(alert_id="ALT-DUP"))

    # Verdicts must be byte-identical apart from None equality on optional fields.
    assert v1["status"] == v2["status"]
    assert v1["severity"] == v2["severity"]
    assert v1["confidence_score"] == v2["confidence_score"]
    assert v1["alert_summary"] == v2["alert_summary"]
    assert v1["duplicate_alert_count"] == v2["duplicate_alert_count"] == 1, (
        "second delivery must NOT bump duplicate_alert_count — that's the "
        "dedup counter, not the idempotency counter"
    )
    assert v1["audit_metadata"]["decision_trace"] == v2["audit_metadata"]["decision_trace"]

    # Only one verdict row should exist in the database.
    verdicts = state_repo.list_verdicts(limit=10)
    assert len(verdicts) == 1, [v["id"] for v in verdicts]


def test_different_alert_ids_do_not_share_idempotency(clean_state):
    """Different alert_ids about the same condition: idempotency must not
    short-circuit. The agent must process both. Dedup will mark the second
    as Suppressed; that's a separate concern."""
    from agents.alert_triage import run

    v1 = run(_alert_input(alert_id="ALT-ONE"))
    v2 = run(_alert_input(alert_id="ALT-TWO"))

    # First is Active (new cluster); second is Suppressed (dedup match).
    assert v1["status"] == "Active"
    assert v2["status"] == "Suppressed"
    # Decision traces must differ — idempotency did NOT fire.
    assert v1["audit_metadata"]["decision_trace"] != v2["audit_metadata"]["decision_trace"]


def test_idempotency_window_expires(clean_state, monkeypatch):
    """Idempotency window must respect time. After the window has elapsed,
    a second delivery of the same alert_id triggers a fresh pipeline run."""
    from agents.alert_triage import run

    v1 = run(_alert_input(alert_id="ALT-EXPIRE"))

    # Shrink the window to effectively zero so the next call falls outside it.
    from agents.alert_triage import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_IDEMPOTENCY_WINDOW", timedelta(seconds=0))

    v2 = run(_alert_input(alert_id="ALT-EXPIRE"))

    # The second call ran the full pipeline rather than short-circuiting on
    # idempotency. Dedup found the existing cluster and marked it Suppressed.
    # ``duplicate_alert_count`` stays at 1 because cluster.source_alerts
    # dedupes on alert_id — that's a separate property of dedup, not what
    # this test asserts.
    assert v2["status"] == "Suppressed", v2["audit_metadata"]["decision_trace"]
    trace = v2["audit_metadata"]["decision_trace"]
    assert any("matched duplicate alert cluster" in line for line in trace), trace
    assert v1["audit_metadata"]["decision_trace"] != trace


def test_reset_state_clears_idempotency_cache(clean_state):
    """The eval harness calls ``reset_state()`` between golden cases. After
    this call, a repeat alert_id MUST trigger a fresh pipeline run rather
    than returning the prior cached verdict — otherwise two cases sharing
    an alert_id silently false-pass."""
    from agents.alert_triage import run
    from agents.alert_triage.agent import reset_state
    from aiops.state import repository as state_repo

    v1 = run(_alert_input(alert_id="ALT-RESET"))
    assert v1["status"] == "Active"
    assert len(state_repo.list_verdicts(limit=10)) == 1

    reset_state()
    assert len(state_repo.list_verdicts(limit=10)) == 0, (
        "reset_state must wipe verdicts; idempotency layer reads from them"
    )

    v2 = run(_alert_input(alert_id="ALT-RESET"))

    # The second run ran the full pipeline against a fully wiped slate, so
    # status is Active again (not Suppressed, which would mean the cluster
    # row survived; not the cached v1, which would mean verdicts survived).
    assert v2["status"] == "Active"
    assert len(state_repo.list_verdicts(limit=10)) == 1


def test_duplicate_alert_count_tracks_deliveries_not_distinct_ids(clean_state, monkeypatch):
    """duplicate_alert_count must increment on every genuine delivery that
    reaches dedup. Three deliveries of the same alert_id (with idempotency
    bypassed to simulate genuine refires beyond the retry window) → count = 3.
    ``source_alerts`` stays a set of one distinct id."""
    from datetime import timedelta

    from agents.alert_triage import agent as agent_mod
    from agents.alert_triage import run

    # Disable idempotency so each delivery reaches dedup — that's the only
    # layer that should be counting deliveries here.
    monkeypatch.setattr(agent_mod, "_IDEMPOTENCY_WINDOW", timedelta(seconds=0))

    v1 = run(_alert_input(alert_id="ALT-REFIRE"))
    v2 = run(_alert_input(alert_id="ALT-REFIRE"))
    v3 = run(_alert_input(alert_id="ALT-REFIRE"))

    assert v1["duplicate_alert_count"] == 1
    assert v2["duplicate_alert_count"] == 2, (
        "second delivery must bump count — that's the field's contract"
    )
    assert v3["duplicate_alert_count"] == 3

    # source_alerts stays a set: one distinct id seen across three deliveries.
    sources = v3["audit_metadata"]["source_alerts"]
    assert sources == ["ALT-REFIRE"], (
        f"source_alerts must remain a set of distinct ids; got {sources}"
    )


def test_duplicate_alert_count_with_mixed_distinct_and_repeated_ids(clean_state, monkeypatch):
    """When a cluster receives a mix of distinct and repeated alert_ids,
    duplicate_alert_count counts every delivery; source_alerts records each
    distinct id once."""
    from datetime import timedelta

    from agents.alert_triage import agent as agent_mod
    from agents.alert_triage import run

    monkeypatch.setattr(agent_mod, "_IDEMPOTENCY_WINDOW", timedelta(seconds=0))

    # 4 deliveries, 2 distinct alert_ids.
    run(_alert_input(alert_id="ALT-A"))
    run(_alert_input(alert_id="ALT-A"))
    run(_alert_input(alert_id="ALT-B"))
    final = run(_alert_input(alert_id="ALT-A"))

    assert final["duplicate_alert_count"] == 4, "4 deliveries → count = 4"
    sources = final["audit_metadata"]["source_alerts"]
    assert sorted(sources) == ["ALT-A", "ALT-B"], (
        f"source_alerts must record each distinct id exactly once; got {sources}"
    )


def test_idempotency_does_not_bump_duplicate_count(clean_state):
    """A duplicate delivery within the idempotency window is a transport
    artifact (Alertmanager retry), not a refire. duplicate_alert_count must
    NOT bump — it would inflate the metric on every network blip."""
    from agents.alert_triage import run

    v1 = run(_alert_input(alert_id="ALT-IDEM"))
    v2 = run(_alert_input(alert_id="ALT-IDEM"))

    assert v1["duplicate_alert_count"] == 1
    assert v2["duplicate_alert_count"] == 1, (
        "idempotency caught the duplicate transport delivery; count must stay at 1"
    )


def test_empty_alert_id_is_rejected_at_construction(clean_state):
    """Empty alert_id is now rejected at the Stage 1 (Pydantic) boundary
    rather than reaching the agent at all. This used to be a defense-in-depth
    check at the idempotency layer; the validator above makes the situation
    impossible by construction."""
    from pydantic import ValidationError

    from agents.alert_triage import run

    with pytest.raises(ValidationError):
        run(_alert_input(alert_id=""))


def test_idempotency_lookup_still_rejects_empty_alert_id(clean_state):
    """Belt-and-suspenders: even if a future code path bypasses Pydantic
    (e.g., direct repository call), the idempotency lookup itself must
    refuse to false-match on an empty alert_id."""
    from datetime import timedelta

    from aiops.state import repository as state_repo

    # No alerts processed, lookup with empty id must return None — not
    # find some unrelated verdict.
    assert state_repo.find_recent_verdict_by_alert_id("", window=timedelta(seconds=60)) is None
