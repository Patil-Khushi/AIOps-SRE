"""Tests for the Incident Commander agent (RA-008, SRE).

RA-008 chains the Reactive-Active flow + RCA into one coordinated response on
top of the orchestrator seam (#74). These tests pin its own behavior:

- Severity gate: engages only for Sev-1/Sev-2.
- Engaged path: runs RCA (read-only), scribes a timeline, seeds a postmortem,
  and emits an IC context pack / handoff through the chatops seam.
- Non-engaged path: reactive pipeline still ran, but no RCA / postmortem / comms.
- Eval contract: ``run`` is dict-in / dict-out and suppresses comms.

The orchestrator is stubbed with a real ``ReactiveFlowResult`` so these tests
exercise RA-008's logic, not RA-001..005 (which have their own suites). RCA
runs for real against the stub LLM, hitting its deterministic fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.alert_triage.models import Alert, Severity, TriageVerdict
from agents.alert_triage.models import AuditMetadata as TriageAudit
from agents.auto_ticketing.models import TicketRecord
from agents.incident_classifier.models import AuditMetadata as ClassAudit
from agents.incident_classifier.models import Classification
from agents.incident_commander import agent as ic
from agents.incident_commander import command
from agents.notification_assembler.models import RoutingDecision
from aiops import state as state_pkg
from aiops.runtime.orchestrator import ReactiveFlowResult
from aiops.tools.chatops import ChatMessage
from aiops.tools.chatops import Severity as ChatSeverity


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    # RCA reads its provider into a module constant at import time and defaults
    # to "anthropic". Pin it to the stub so analyze() takes its deterministic
    # fallback path (no network, no dependency on .env creds) — the locked
    # slow-product-catalog verdict names the productCatalogFailure flag.
    monkeypatch.setattr("agents.rca_agent.agent._RCA_PROVIDER", "stub")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _alert() -> Alert:
    return Alert(
        alert_id="ALT-IC-1",
        service="product-catalog",
        metric="latency_p95",
        value=5.2,
        threshold=1.0,
        timestamp="2026-05-21T10:00:00Z",
        source="Prometheus",
    )


def _flow_result(severity: Severity) -> ReactiveFlowResult:
    verdict = TriageVerdict(
        affected_service="product-catalog",
        severity=severity,
        confidence_score=0.9,
        alert_summary="product-catalog p95 latency 5.2s above 1.0s",
        assigned_team="Platform On-Call",
        assigned_engineer="oncall@platform.example.com",
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=TriageAudit(
            created_at=datetime.now(UTC),
            source_alerts=["ALT-IC-1"],
            decision_trace=["received", "new alert cluster", "severity rule-based"],
        ),
    )
    classification = Classification(
        incident_type="application",  # type: ignore[arg-type]
        confidence=0.8,
        rationale="stub",
        probable_root_cause="stub",
        routing_team="Platform On-Call",
        audit_metadata=ClassAudit(created_at=datetime.now(UTC)),
    )
    ticket = TicketRecord(created=True, ticket_id="INC-42", system="mock", urgency=1)
    decision = RoutingDecision(
        chat_severity=ChatSeverity.P1,
        channel="incidents",
        title="t",
        body="b",
        actions=["page_oncall"],
        reason="r",
    )
    return ReactiveFlowResult(
        verdict=verdict,
        verdict_id=1,
        classification=classification,
        classification_id=2,
        ticket=ticket,
        routing=decision,
        deliveries={},
        notification_id=3,
    )


class _CapturingClient:
    """Fake chatops client capturing what RA-008 sends."""

    def __init__(self) -> None:
        self.sent: list[ChatMessage] = []

    def send(self, msg: ChatMessage) -> dict[str, Any]:
        self.sent.append(msg)
        return {}


def _stub_flow(monkeypatch, severity: Severity):
    monkeypatch.setattr(ic, "run_reactive_flow", lambda _alert: _flow_result(severity))


def test_sev1_engages_runs_rca_and_emits_comms(monkeypatch):
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="slow-product-catalog")

    assert result.engaged is True
    assert result.severity == "Sev-1"
    assert result.affected_service == "product-catalog"
    assert result.handoff_requested is True

    # RCA ran (read-only) and produced a verdict bundle.
    assert result.rca is not None
    assert "root_cause" in result.rca
    assert result.rca["ranked_fix_steps"]  # at least one step
    # Deterministic fallback for the locked scenario names the injected flag.
    assert "productCatalogFailure" in result.rca["root_cause"]

    # Postmortem seed pre-filled with facts.
    seed = result.postmortem_seed
    assert seed is not None
    assert seed.affected_service == "product-catalog"
    assert seed.ticket_id == "INC-42"
    assert seed.root_cause and "productCatalogFailure" in seed.root_cause
    assert seed.contributing_signals  # RA-001 trace carried over

    # Timeline scribed across stages, including the correlate placeholder.
    stages = {e.stage for e in result.timeline}
    assert {"triage", "classify", "correlate", "rca", "handoff"} <= stages

    # Comms: one IC context pack + handoff emitted through the seam.
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert msg.channel == "incidents"
    assert "handoff_human_ic" in msg.actions
    assert "incident_command" in msg.actions


def test_sev4_does_not_engage(monkeypatch):
    _stub_flow(monkeypatch, "Sev-4")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert())

    assert result.engaged is False
    assert result.severity == "Sev-4"
    assert result.rca is None
    assert result.postmortem_seed is None
    assert result.handoff_requested is False
    # No coordination comms for a non-engaged incident.
    assert client.sent == []
    # Reactive pipeline still ran and is surfaced.
    assert result.reactive["verdict"]["affected_service"] == "product-catalog"


def test_emit_comms_false_suppresses_send(monkeypatch):
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="slow-product-catalog", emit_comms=False)

    assert result.engaged is True
    assert result.handoff_requested is True  # decided, just not emitted
    assert client.sent == []  # comms suppressed


def test_run_eval_contract(monkeypatch):
    """``run`` is dict-in / dict-out and suppresses comms (no audit pollution)."""
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    out = ic.run(
        {
            "scenario_id": "slow-product-catalog",
            "alert": {
                "alert_id": "ALT-IC-run-1",
                "service": "product-catalog",
                "metric": "latency_p95",
                "value": 5.2,
                "threshold": 1.0,
                "timestamp": "2026-05-21T10:00:00Z",
                "source": "Prometheus",
            },
        }
    )

    assert isinstance(out, dict)
    assert out["engaged"] is True
    assert out["severity"] == "Sev-1"
    assert out["handoff_requested"] is True
    assert client.sent == []  # run() forces emit_comms=False


def test_reset_state_is_callable():
    # Cascades to RA-001 / RA-002 resets; must not raise.
    ic.reset_state()
