"""Regression for #84: the /api/triage handler in demo/ui/server.py must
unwrap the ``RoutingOutcome`` returned by ``notification_router.route()``
before handing the decision to ``save_notification`` and shaping the
response. Without unwrapping, ``save_notification`` blew up with an
``AttributeError`` (silently swallowed by the surrounding try/except,
killing structured-notification persistence) and the response's
``notifications`` field had the wrong nested shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.alert_triage.models import AuditMetadata, TriageVerdict
from agents.notification_router.models import RoutingDecision, RoutingOutcome
from aiops import state as state_pkg
from aiops.tools.chatops import DeliveryResult, Severity
from demo.ui import server as srv


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _alert_payload() -> dict[str, Any]:
    return {
        "alert_id": "ALT-OUTCOME-1",
        "service": "payment",
        "metric": "ErrorRateHigh",
        "value": 0.42,
        "threshold": 0.1,
        "timestamp": "2026-05-26T10:00:00Z",
        "source": "Prometheus",
        "labels": {},
        "annotations": {},
    }


def _verdict() -> TriageVerdict:
    return TriageVerdict(
        affected_service="payment",
        severity="Sev-1",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="payment unhealthy",
        assigned_team="Payments Team",
        assigned_engineer="oncall@payments.example.com",
        recommended_runbook=None,
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime.now(UTC),
            source_alerts=["ALT-OUTCOME-1"],
            decision_trace=["received"],
        ),
    )


def _decision() -> RoutingDecision:
    return RoutingDecision(
        chat_severity=Severity.P1,
        channel="incidents-payments",
        title="[Sev-1] payment unhealthy",
        body="Service: payment\nSeverity: Sev-1",
        mentions=["@payments-oncall"],
        actions=[],
        reason="Sev-1 → page + chat",
        audit_trace=["status=Active → emit"],
    )


def _stub_pipeline(monkeypatch, *, outcome: RoutingOutcome) -> dict[str, Any]:
    """Stub triage/classify/auto_ticket and capture what save_notification
    receives. Returns the capture dict so tests can assert on it."""
    captured: dict[str, Any] = {}

    monkeypatch.setattr(srv, "triage", lambda _alert: _verdict())

    class _StubClassification:
        def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
            return {"category": "infra"}

    monkeypatch.setattr(srv, "classify", lambda _input: _StubClassification())

    class _StubTicket:
        def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
            return {"id": "INC-1"}

    monkeypatch.setattr(
        srv,
        "auto_ticket",
        lambda _verdict, classification=None: _StubTicket(),
    )

    monkeypatch.setattr(srv, "route_notification", lambda _verdict: outcome)

    # Stub upstream persistence calls so the test doesn't depend on the
    # exact shapes of Classification / Ticket — those have their own tests.
    monkeypatch.setattr(srv.state_repo, "save_verdict", lambda _v, cluster_key: 1)
    monkeypatch.setattr(srv.state_repo, "save_classification", lambda _c, verdict_id: 2)

    real_save_notification = srv.state_repo.save_notification

    def _capturing_save_notification(decision, *, verdict_id: int) -> int:
        captured["decision"] = decision
        captured["verdict_id"] = verdict_id
        return real_save_notification(decision, verdict_id=verdict_id)

    monkeypatch.setattr(srv.state_repo, "save_notification", _capturing_save_notification)
    return captured


def test_triage_alert_unwraps_routing_outcome_for_persistence_and_response(monkeypatch):
    decision = _decision()
    outcome = RoutingOutcome(
        decision=decision,
        deliveries={
            "jsonfile": DeliveryResult(adapter="jsonfile", ok=True, latency_ms=2),
            "websocket": DeliveryResult(
                adapter="websocket",
                ok=False,
                error="RuntimeError: hub closed",
                latency_ms=1,
            ),
        },
    )
    captured = _stub_pipeline(monkeypatch, outcome=outcome)

    response = srv.triage_alert(srv.TriageRequest(alert=_alert_payload()))

    # save_notification got the flat RoutingDecision, not the RoutingOutcome.
    # If the handler stops unwrapping, this assert is what fails first.
    assert isinstance(captured["decision"], RoutingDecision)
    assert captured["decision"].channel == "incidents-payments"

    # Persistence actually succeeded — notification_id is non-null. Before the
    # fix this came back null because save_notification raised AttributeError
    # inside the swallowed try/except.
    assert response["persisted"]["notification_id"] is not None

    # Response shape: notifications stays a flat decision dump (no nested
    # ``decision``/``deliveries`` keys, which would silently break consumers).
    notifications = response["notifications"]
    assert notifications is not None
    assert notifications["channel"] == "incidents-payments"
    assert notifications["chat_severity"] == "p1"
    assert "decision" not in notifications
    assert "deliveries" not in notifications

    # Per-adapter deliveries surface as a sibling top-level key.
    deliveries = response["deliveries"]
    assert set(deliveries) == {"jsonfile", "websocket"}
    assert deliveries["jsonfile"]["ok"] is True
    assert deliveries["websocket"]["ok"] is False
    assert deliveries["websocket"]["error"] == "RuntimeError: hub closed"


def test_triage_alert_exposes_empty_deliveries_when_outcome_suppressed(monkeypatch):
    """Suppressed verdicts return a RoutingOutcome with an empty
    ``deliveries`` dict (no chatops emit). The handler must still surface
    the decision and an empty ``deliveries`` mapping rather than ``None``.
    """
    suppressed_decision = _decision().model_copy(
        update={
            "channel": "suppressed",
            "reason": "status=Suppressed → no emit",
            "audit_trace": ["status=Suppressed → suppress"],
        }
    )
    outcome = RoutingOutcome(decision=suppressed_decision, deliveries={})
    _stub_pipeline(monkeypatch, outcome=outcome)

    response = srv.triage_alert(srv.TriageRequest(alert=_alert_payload()))

    assert response["notifications"]["channel"] == "suppressed"
    assert response["deliveries"] == {}
