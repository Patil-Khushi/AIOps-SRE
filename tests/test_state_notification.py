"""Tests for save_notification — RA-005 RoutingDecision persistence (CHAT-2 #82).

Mirrors ``tests/test_state_classification.py`` structure: in-memory SQLite per
test, build a minimal verdict + decision, round-trip through the repository
and assert the rendered dict shape matches what ``/api/notifications`` returns.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aiops import state as state_pkg
from aiops.state import repository as repo
from aiops.tools.chatops import Severity


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


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


def _make_routing_decision(**overrides):
    from agents.notification_router.models import RoutingDecision

    base = dict(
        chat_severity=Severity.P1,
        channel="incidents-payments",
        title="[Sev-1] payment unhealthy",
        body="Service: payment\nSeverity: Sev-1\nReason: error rate spiked",
        mentions=["@payments-oncall"],
        actions=[],  # CHAT-3 (#83) will populate; until then keep empty
        reason="Sev-1 → page + chat",
        audit_trace=["status=Active → emit", "Sev-1 → P1 channel"],
    )
    base.update(overrides)
    return RoutingDecision(**base)


def test_save_notification_roundtrip():
    vid = repo.save_verdict(_make_triage_verdict(), cluster_key="payment-oom-ck")
    nid = repo.save_notification(_make_routing_decision(), verdict_id=vid)
    assert nid > 0

    rows = repo.list_notifications(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == nid
    assert row["verdict_id"] == vid
    assert row["channel"] == "incidents-payments"
    assert row["chat_severity"] == "p1"  # Severity.P1.value
    assert row["title"].startswith("[Sev-1]")
    assert "payment" in row["body"]
    # service is looked up from the upstream verdict, not the decision
    assert row["service"] == "payment"
    assert row["reason"] == "Sev-1 → page + chat"
    assert row["audit_trace"] == ["status=Active → emit", "Sev-1 → P1 channel"]
    # actions: empty list from agent → stored as NULL, surfaced as [].
    # CHAT-3 (#83) will replace this with the real action vocabulary.
    assert row["actions"] == []
    # routed_at survives the SQLite roundtrip with tz info
    assert row["routed_at"] is not None
    assert row["routed_at"].endswith("+00:00") or "T" in row["routed_at"]


def test_save_notification_actions_populated_when_present():
    """When CHAT-3 lands and the agent emits real actions, the column should
    persist them verbatim rather than coercing back to NULL."""
    vid = repo.save_verdict(_make_triage_verdict(), cluster_key="ck-actions")
    decision = _make_routing_decision(actions=["page_oncall", "post_to_chat"])
    repo.save_notification(decision, verdict_id=vid)

    row = repo.list_notifications(limit=1)[0]
    assert row["actions"] == ["page_oncall", "post_to_chat"]


def test_list_notifications_filters_by_service_and_orders_newest_first():
    vid_pay = repo.save_verdict(_make_triage_verdict(service="payment"), cluster_key="ck-pay")
    vid_cart = repo.save_verdict(_make_triage_verdict(service="cart"), cluster_key="ck-cart")
    # Two notifications on payment, one on cart. list_notifications must
    # return newest-first overall and filter by service when asked.
    repo.save_notification(_make_routing_decision(title="pay-1"), verdict_id=vid_pay)
    repo.save_notification(_make_routing_decision(title="cart-1"), verdict_id=vid_cart)
    repo.save_notification(_make_routing_decision(title="pay-2"), verdict_id=vid_pay)

    all_rows = repo.list_notifications(limit=10)
    assert [r["title"] for r in all_rows] == ["pay-2", "cart-1", "pay-1"]

    pay_rows = repo.list_notifications(limit=10, service="payment")
    assert [r["title"] for r in pay_rows] == ["pay-2", "pay-1"]
    assert all(r["service"] == "payment" for r in pay_rows)


def test_count_notifications():
    assert repo.count_notifications() == 0
    vid = repo.save_verdict(_make_triage_verdict(), cluster_key="ck")
    repo.save_notification(_make_routing_decision(), verdict_id=vid)
    repo.save_notification(_make_routing_decision(title="second"), verdict_id=vid)
    assert repo.count_notifications() == 2


def test_save_notification_requires_existing_verdict_for_service_lookup():
    """``save_notification`` reads ``service`` from the linked verdict row.
    If verdict_id doesn't exist, the row still persists — ``service`` stays
    None rather than raising. That mirrors the rest of the repository's
    "be permissive at write time, validate on read" stance."""
    nid = repo.save_notification(_make_routing_decision(), verdict_id=99999)
    assert nid > 0
    rows = repo.list_notifications(limit=1)
    assert rows[0]["service"] is None
