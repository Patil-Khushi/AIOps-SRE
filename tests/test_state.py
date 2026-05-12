"""Smoke tests for aiops.state — the persistent state seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def _make_verdict(severity: str = "Sev-2", service: str = "checkout"):
    from agents.alert_triage.models import AuditMetadata, TriageVerdict

    return TriageVerdict(
        affected_service=service,
        severity=severity,  # type: ignore[arg-type]
        confidence_score=0.8,
        alert_summary=f"{service} unhealthy",
        assigned_team="Payments Team",
        assigned_engineer="oncall@example.com",
        recommended_runbook=None,
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime.now(UTC),
            source_alerts=["ALERT-1"],
            decision_trace=["received", "new alert cluster"],
        ),
    )


def test_save_and_list_verdict_roundtrip():
    verdict = _make_verdict()
    vid = repo.save_verdict(verdict, cluster_key="abc123")
    assert vid > 0

    rows = repo.list_verdicts()
    assert len(rows) == 1
    row = rows[0]
    assert row["affected_service"] == "checkout"
    assert row["severity"] == "Sev-2"
    assert row["audit_metadata"]["source_alerts"] == ["ALERT-1"]
    assert row["audit_metadata"]["decision_trace"] == ["received", "new alert cluster"]


def test_list_verdicts_filters_by_service_and_severity():
    repo.save_verdict(_make_verdict(severity="Sev-1", service="payment"), cluster_key="k1")
    repo.save_verdict(_make_verdict(severity="Sev-3", service="payment"), cluster_key="k2")
    repo.save_verdict(_make_verdict(severity="Sev-1", service="checkout"), cluster_key="k3")

    by_service = repo.list_verdicts(service="payment")
    assert {r["severity"] for r in by_service} == {"Sev-1", "Sev-3"}

    by_severity = repo.list_verdicts(severity="Sev-1")
    assert {r["affected_service"] for r in by_severity} == {"payment", "checkout"}


def test_upsert_cluster_appends_alert_ids():
    now = datetime.now(UTC)
    repo.upsert_cluster(
        cluster_key="ck", service="ad", metric="cpu", alert_id="A1", seen_at=now,
    )
    second = repo.upsert_cluster(
        cluster_key="ck", service="ad", metric="cpu", alert_id="A2", seen_at=now,
    )
    assert second["alert_count"] == 2
    assert second["source_alerts"] == ["A1", "A2"]


def test_find_active_cluster_respects_window():
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    repo.upsert_cluster(
        cluster_key="old", service="ad", metric="cpu", alert_id="A1", seen_at=long_ago,
    )
    hit = repo.find_active_cluster("old", window=timedelta(minutes=5))
    assert hit is None  # outside the window

    fresh = datetime.now(UTC)
    repo.upsert_cluster(
        cluster_key="new", service="ad", metric="cpu", alert_id="A1", seen_at=fresh,
    )
    assert repo.find_active_cluster("new", window=timedelta(minutes=5)) is not None


def test_evict_expired_clusters_removes_only_stale_rows():
    now = datetime.now(UTC)
    repo.upsert_cluster(
        cluster_key="fresh", service="ad", metric="cpu", alert_id="A1", seen_at=now,
    )
    repo.upsert_cluster(
        cluster_key="stale",
        service="ad",
        metric="cpu",
        alert_id="A2",
        seen_at=now - timedelta(hours=1),
    )
    removed = repo.evict_expired_clusters(timedelta(minutes=5))
    assert removed == 1
    assert {c["cluster_key"] for c in repo.list_active_clusters(timedelta(minutes=5))} == {"fresh"}
