"""Tests for the Reactive-Active orchestrator seam (INFRA-2, issue #74).

The chain RA-001 → RA-002 → RA-003 → RA-005 moved out of the demo server's
``/api/triage`` handler into ``aiops.runtime.orchestrator.run_reactive_flow``.
These tests own the pipeline behaviors that used to be asserted at the
endpoint:

- #84 regression — ``route()`` returns a ``RoutingOutcome``; the orchestrator
  must persist the *flat* ``RoutingDecision`` (not the outcome wrapper) and
  surface per-adapter deliveries as a sibling mapping in ``to_api_dict``.
- The three-state ``routing``/``deliveries`` contract: happy path, Suppressed
  (empty deliveries), and a routing exception (both ``None``).
- FK guards — when ``triage`` could not persist its verdict (``verdict_id`` is
  ``None``), the classification / notification persistence is skipped, not
  crashed.

Agents are stubbed at the orchestrator module boundary with *real* model
instances, because ``ReactiveFlowResult`` validates its fields against the
canonical models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.alert_triage.classifier_models import AuditMetadata as ClassAudit
from agents.alert_triage.classifier_models import Classification
from agents.alert_triage.models import Alert, TriageVerdict
from agents.alert_triage.models import AuditMetadata as TriageAudit
from agents.auto_ticketing.models import TicketRecord
from agents.notification_assembler.models import NotificationOutcome, RoutingDecision
from aiops import state as state_pkg
from aiops.runtime import orchestrator as orch
from aiops.tools.chatops import DeliveryResult, Severity


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _alert() -> Alert:
    return Alert(
        alert_id="ALT-OUTCOME-1",
        service="payment",
        metric="ErrorRateHigh",
        value=0.42,
        threshold=0.1,
        timestamp="2026-05-26T10:00:00Z",
        source="Prometheus",
    )


def _verdict() -> TriageVerdict:
    return TriageVerdict(
        affected_service="payment",
        severity="Sev-1",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="payment unhealthy",
        assigned_team="Payments Team",
        assigned_engineer="oncall@payments.example.com",
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=TriageAudit(
            created_at=datetime.now(UTC),
            source_alerts=["ALT-OUTCOME-1"],
            decision_trace=["received"],
        ),
    )


def _classification() -> Classification:
    return Classification(
        incident_type="application",  # type: ignore[arg-type]
        confidence=0.8,
        rationale="stubbed",
        probable_root_cause="stub cause",
        routing_team="Payments Team",
        audit_metadata=ClassAudit(created_at=datetime.now(UTC)),
    )


def _ticket() -> TicketRecord:
    return TicketRecord(created=True, ticket_id="INC-1", system="mock", urgency=1)


def _decision(**overrides: Any) -> RoutingDecision:
    base = {
        "chat_severity": Severity.P1,
        "channel": "incidents-payments",
        "title": "[Sev-1] payment unhealthy",
        "body": "Service: payment\nSeverity: Sev-1",
        "mentions": ["@payments-oncall"],
        "actions": ["page_oncall", "post_to_chat"],
        "reason": "Sev-1 → page + chat",
        "audit_trace": ["status=Active → emit"],
    }
    base.update(overrides)
    return RoutingDecision(**base)  # type: ignore[arg-type]


def _stub_agents(
    monkeypatch,
    *,
    outcome: NotificationOutcome,
    verdict_id: int | None = 1,
) -> dict[str, Any]:
    """Stub the four agents at the orchestrator boundary with real models and
    capture what ``save_notification`` receives. Returns the capture dict."""
    captured: dict[str, Any] = {}

    monkeypatch.setattr(orch, "triage", lambda _alert: (_verdict(), verdict_id))
    monkeypatch.setattr(orch, "classify", lambda _input: _classification())
    monkeypatch.setattr(
        orch,
        "auto_ticket",
        lambda _verdict, classification=None, **_kw: _ticket(),
    )
    monkeypatch.setattr(orch, "notify_incident", lambda _verdict: outcome)

    sc_calls: list[Any] = []

    def _save_classification(_c, *, verdict_id: int) -> int:
        sc_calls.append(verdict_id)
        return 2

    monkeypatch.setattr(orch.state_repo, "save_classification", _save_classification)
    captured["save_classification_calls"] = sc_calls

    real_save_notification = orch.state_repo.save_notification

    def _capturing_save_notification(decision, *, verdict_id: int) -> int:
        captured["decision"] = decision
        captured["verdict_id"] = verdict_id
        return real_save_notification(decision, verdict_id=verdict_id)

    monkeypatch.setattr(orch.state_repo, "save_notification", _capturing_save_notification)
    return captured


def test_unwraps_routing_outcome_for_persistence_and_response(monkeypatch):
    """#84: save_notification gets the flat RoutingDecision; to_api_dict keeps
    notifications flat and surfaces per-adapter deliveries as a sibling key."""
    outcome = NotificationOutcome(
        decision=_decision(),
        deliveries={
            "jsonfile": DeliveryResult(adapter="jsonfile", ok=True, latency_ms=2),
            "websocket": DeliveryResult(
                adapter="websocket", ok=False, error="RuntimeError: hub closed", latency_ms=1
            ),
        },
    )
    captured = _stub_agents(monkeypatch, outcome=outcome)

    result = orch.run_reactive_flow(_alert())

    # Flat decision persisted — not the RoutingOutcome wrapper.
    assert isinstance(captured["decision"], RoutingDecision)
    assert captured["decision"].channel == "incidents-payments"
    assert result.notification_id is not None

    api = result.to_api_dict()
    notifications = api["notifications"]
    assert notifications is not None
    assert notifications["channel"] == "incidents-payments"
    assert notifications["chat_severity"] == "p1"
    assert "decision" not in notifications
    assert "deliveries" not in notifications

    deliveries = api["deliveries"]
    assert set(deliveries) == {"jsonfile", "websocket"}
    assert deliveries["jsonfile"]["ok"] is True
    assert deliveries["websocket"]["ok"] is False
    assert deliveries["websocket"]["error"] == "RuntimeError: hub closed"


def test_suppressed_outcome_exposes_empty_deliveries(monkeypatch):
    """Suppressed verdict → route returns empty deliveries; the decision is
    still surfaced and deliveries is ``{}`` (not ``None``)."""
    outcome = NotificationOutcome(
        decision=_decision(channel="suppressed", reason="status=Suppressed → no emit"),
        deliveries={},
    )
    _stub_agents(monkeypatch, outcome=outcome)

    api = orch.run_reactive_flow(_alert()).to_api_dict()

    assert api["notifications"]["channel"] == "suppressed"
    assert api["deliveries"] == {}


def test_routing_exception_is_contained(monkeypatch):
    """A routing failure must not break the pipeline: routing/deliveries come
    back ``None`` and the rest of the result is still populated."""
    _stub_agents(monkeypatch, outcome=NotificationOutcome(decision=_decision(), deliveries={}))

    def _boom(_verdict):
        raise RuntimeError("router exploded")

    monkeypatch.setattr(orch, "notify_incident", _boom)

    result = orch.run_reactive_flow(_alert())
    api = result.to_api_dict()

    assert result.routing is None
    assert result.deliveries is None
    assert result.notification_id is None
    assert api["notifications"] is None
    assert api["deliveries"] is None
    # Everything upstream of routing still populated.
    assert api["verdict"]["affected_service"] == "payment"
    assert api["ticket"]["created"] is True
    assert api["classification"]["incident_type"] == "application"


def test_fk_guard_skips_persistence_when_verdict_unpersisted(monkeypatch):
    """When triage could not persist (verdict_id is None), classification and
    notification persistence are skipped rather than crashing on the FK."""
    outcome = NotificationOutcome(decision=_decision(), deliveries={})
    captured = _stub_agents(monkeypatch, outcome=outcome, verdict_id=None)

    result = orch.run_reactive_flow(_alert())

    assert result.verdict_id is None
    assert result.classification_id is None
    assert result.notification_id is None
    assert captured["save_classification_calls"] == []
    assert "decision" not in captured  # save_notification never invoked
