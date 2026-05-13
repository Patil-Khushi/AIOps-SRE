"""Tests for save_classification — RA-002 output persistence to aiops.state."""

from __future__ import annotations

from datetime import UTC, datetime

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


def _make_classification(**overrides):
    from agents.incident_classifier.models import AuditMetadata, Classification

    base = dict(
        incident_type="infrastructure",
        confidence=0.85,
        rationale="High-similarity match to past OOM incidents.",
        tags=["oom", "memory", "capacity"],
        probable_root_cause="memory limit too low; OOM-kill loop",
        routing_team="Payments Team",
        on_call_engineer="oncall@payments.example.com",
        recommended_runbook="https://runbooks.example.com/payment-oom",
        dependencies=["currency", "fraud-detection"],
        similar_incident_ids=["SEED-INF-001", "SEED-INF-002"],
        audit_metadata=AuditMetadata(
            created_at=datetime.now(UTC),
            created_by="RA-002",
            decision_trace=["embedded text", "Tier-2 LLM with evidence", "CMDB lookup"],
            similar_incidents=[
                {
                    "incident_key": "SEED-INF-001",
                    "incident_type": "infrastructure",
                    "similarity": 0.95,
                },
                {
                    "incident_key": "SEED-INF-002",
                    "incident_type": "infrastructure",
                    "similarity": 0.87,
                },
            ],
        ),
    )
    base.update(overrides)
    return Classification(**base)


def _make_triage_verdict(service: str = "payment", severity: str = "Sev-1"):
    from agents.alert_triage.models import AuditMetadata as TriageAudit
    from agents.alert_triage.models import TriageVerdict

    return TriageVerdict(
        affected_service=service,
        severity=severity,  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary=f"{service} unhealthy",
        assigned_team="Payments Team",
        assigned_engineer="oncall@payments.example.com",
        recommended_runbook=None,
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=TriageAudit(
            created_at=datetime.now(UTC),
            source_alerts=["ALERT-1"],
            decision_trace=["received", "new alert cluster"],
        ),
    )


def test_save_classification_roundtrip():
    c = _make_classification()
    cid = repo.save_classification(c)
    assert cid > 0

    row = repo.get_classification(cid)
    assert row is not None
    assert row["incident_type"] == "infrastructure"
    assert row["confidence"] == 0.85
    assert row["rationale"].startswith("High-similarity")
    assert row["tags"] == ["oom", "memory", "capacity"]
    assert row["probable_root_cause"] == "memory limit too low; OOM-kill loop"
    assert row["routing_team"] == "Payments Team"
    assert row["on_call_engineer"] == "oncall@payments.example.com"
    assert row["recommended_runbook"] == "https://runbooks.example.com/payment-oom"
    assert row["dependencies"] == ["currency", "fraud-detection"]
    assert row["similar_incident_ids"] == ["SEED-INF-001", "SEED-INF-002"]
    assert row["audit_metadata"]["created_by"] == "RA-002"
    assert row["audit_metadata"]["decision_trace"] == [
        "embedded text",
        "Tier-2 LLM with evidence",
        "CMDB lookup",
    ]
    assert row["audit_metadata"]["similar_incidents"][0]["incident_key"] == "SEED-INF-001"
    assert row["audit_metadata"]["similar_incidents"][0]["similarity"] == 0.95


def test_save_classification_links_to_upstream_verdict():
    """A classification persisted with a verdict_id should link back to the
    originating RA-001 verdict — so the dashboard can show the full chain."""
    vid = repo.save_verdict(_make_triage_verdict(), cluster_key="payment-oom-ck")
    assert vid > 0

    cid = repo.save_classification(_make_classification(), verdict_id=vid)
    row = repo.get_classification(cid)
    assert row is not None
    assert row["verdict_id"] == vid


def test_save_classification_standalone_without_verdict():
    """RA-002 must be usable without an upstream verdict in state
    (CLAUDE.md principle #2 — individually sellable agents)."""
    cid = repo.save_classification(_make_classification())
    row = repo.get_classification(cid)
    assert row is not None
    assert row["verdict_id"] is None


def test_get_classification_returns_none_for_unknown_id():
    assert repo.get_classification(99999) is None


def test_two_classifications_get_distinct_ids():
    cid1 = repo.save_classification(_make_classification())
    cid2 = repo.save_classification(_make_classification(incident_type="application"))
    assert cid1 != cid2

    row2 = repo.get_classification(cid2)
    assert row2 is not None
    assert row2["incident_type"] == "application"
