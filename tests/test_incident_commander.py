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

import pytest

from agents.alert_triage.classifier_models import AuditMetadata as ClassAudit
from agents.alert_triage.classifier_models import Classification
from agents.alert_triage.models import Alert, Severity, TriageVerdict
from agents.alert_triage.models import AuditMetadata as TriageAudit
from agents.auto_ticketing.models import TicketRecord
from agents.incident_commander import agent as ic
from agents.incident_commander import command
from agents.notification_assembler.models import RoutingDecision
from aiops import state as state_pkg
from aiops.runtime.orchestrator import ReactiveFlowResult
from aiops.tools.chatops import ChatMessage, DeliveryResult
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
        service="user-service",
        metric="latency_p95",
        value=5.2,
        threshold=1.0,
        timestamp="2026-05-21T10:00:00Z",
        source="Prometheus",
    )


def _flow_result(severity: Severity) -> ReactiveFlowResult:
    verdict = TriageVerdict(
        affected_service="user-service",
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
    """Fake chatops client capturing what RA-008 sends.

    Stands in for a working sink: ``send`` reports one successful delivery so
    the agent's delivery-derived ``handoff_requested`` resolves to True (an
    empty dict would mean "no sinks reached" — see _FailingClient)."""

    def __init__(self) -> None:
        self.sent: list[ChatMessage] = []

    def send(self, msg: ChatMessage) -> dict[str, DeliveryResult]:
        self.sent.append(msg)
        return {"capturing": DeliveryResult(adapter="capturing", ok=True, latency_ms=0)}


def _stub_flow(monkeypatch, severity: Severity):
    monkeypatch.setattr(ic, "run_reactive_flow", lambda _alert: _flow_result(severity))


def test_sev1_engages_runs_rca_and_emits_comms(monkeypatch):
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="user_service_mysql_down")

    assert result.engaged is True
    assert result.severity == "Sev-1"
    assert result.affected_service == "user-service"
    assert result.handoff_requested is True

    # RCA ran (read-only) and produced a verdict bundle.
    assert result.rca is not None
    assert "root_cause" in result.rca
    assert result.rca["ranked_fix_steps"]  # at least one step
    # Deterministic fallback for the locked scenario names the injected flag.
    # Prose root cause; the failure key is on the fix step, not in the sentence.
    assert "MySQL" in result.rca["root_cause"]

    # Postmortem seed pre-filled with facts.
    seed = result.postmortem_seed
    assert seed is not None
    assert seed.affected_service == "user-service"
    assert seed.ticket_id == "INC-42"
    assert seed.root_cause and "MySQL" in seed.root_cause
    assert seed.contributing_signals  # RA-001 trace carried over

    # Timeline scribed across stages, including the RA-007 correlate step.
    stages = {e.stage for e in result.timeline}
    assert {"triage", "classify", "correlate", "rca", "handoff"} <= stages

    # Comms: one IC context pack + handoff emitted through the seam.
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert msg.channel == "incidents"
    assert "handoff_human_ic" in msg.actions
    assert "incident_command" in msg.actions
    # incident_id carries the filed ticket id (not the never-populated
    # verdict.incident_id) so adapters render the real incident reference.
    assert msg.incident_id == "INC-42"


def test_engaged_path_runs_ra007_and_feeds_rca(monkeypatch):
    """On the engaged path RA-008 runs RA-007 Log Correlation and feeds its
    evidence pack into RCA (the catalog chain RA-003 → RA-007 → RCA)."""
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    # Spy on the RCA call to capture the correlation evidence RA-008 passes in.
    captured = {}
    real_analyze = ic.rca_analyze

    def _spy(triage_verdict, **kwargs):
        captured["correlation"] = kwargs.get("correlation")
        return real_analyze(triage_verdict, **kwargs)

    monkeypatch.setattr(ic, "rca_analyze", _spy)

    result = command(_alert(), scenario_id="user_service_mysql_down", emit_comms=False)

    # RCA received a correlation evidence pack (dict), not None.
    assert isinstance(captured["correlation"], dict)
    assert "suspected_dependencies" in captured["correlation"]
    assert "timeline" in captured["correlation"]
    # The correlate timeline beat reflects real RA-007 output, not a placeholder.
    correlate_entries = [e for e in result.timeline if e.stage == "correlate"]
    assert correlate_entries
    assert "RA-007" in correlate_entries[0].detail
    assert "skipped" not in correlate_entries[0].detail.lower()
    # RCA still produced its verdict via the locked-scenario fallback.
    # The root cause reads as prose; the machine-readable failure key lives on
    # the fix step's `flag` field, not embedded in the sentence.
    assert result.rca and "MySQL" in result.rca["root_cause"]
    assert any(
        step.get("flag") == "user_service.mysql_down"
        for step in result.rca.get("ranked_fix_steps", [])
    )


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
    # RA-007 correlation runs on the engaged path only — not for Sev-4.
    assert "correlate" not in {e.stage for e in result.timeline}
    # Reactive pipeline still ran and is surfaced.
    assert result.reactive["verdict"]["affected_service"] == "user-service"
    # Metrics still derived for the reactive stages, but no handoff happened.
    assert result.metrics is not None
    assert result.metrics.time_to_triage_seconds is not None
    assert result.metrics.time_to_handoff_seconds is None


def test_emit_comms_false_suppresses_send(monkeypatch):
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="user_service_mysql_down", emit_comms=False)

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
            "scenario_id": "user_service_mysql_down",
            "alert": {
                "alert_id": "ALT-IC-run-1",
                "service": "user-service",
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


def test_all_adapters_failing_marks_handoff_not_delivered(monkeypatch):
    """If every chatops sink fails, the IC must not claim a delivered handoff."""
    _stub_flow(monkeypatch, "Sev-1")

    # Pin RA-007's behaviour explicitly so the assertion can't pass for the
    # wrong reason: without this the test would lean on the observability URLs
    # being unreachable to make correlate() fail. We don't care which way it
    # goes here — only that delivery fails — so make it unavailable cleanly.
    def _correlate_unavailable(_ci):
        raise RuntimeError("RA-007 unavailable (test)")

    monkeypatch.setattr(ic, "correlate", _correlate_unavailable)

    class _FailingClient:
        def send(self, msg: ChatMessage) -> dict[str, DeliveryResult]:
            return {"jsonfile": DeliveryResult(adapter="jsonfile", ok=False, error="boom")}

    monkeypatch.setattr(ic, "get_client", lambda: _FailingClient())

    result = command(_alert(), scenario_id="user_service_mysql_down")

    assert result.engaged is True
    # Delivery failed on every sink → don't claim a handoff we couldn't deliver.
    assert result.handoff_requested is False
    # The failure is surfaced in the decision trace, not swallowed.
    assert any("NOT delivered" in t for t in result.audit_metadata.decision_trace)
    # No "handoff" timeline beat when nothing reached a human surface.
    assert "handoff" not in {e.stage for e in result.timeline}


def test_timeline_uses_real_event_timestamps(monkeypatch):
    """Timeline reactive stages carry their own recorded event times (triage
    created_at, RA-005 decided_at), not one collapsed reconstruction time."""
    flow = _flow_result("Sev-1")
    # Pin distinct, ordered event times so the assertion doesn't depend on the
    # platform clock resolution (Windows now() can collapse sub-16ms calls).
    t_triage = datetime(2026, 5, 21, 10, 0, 0, tzinfo=UTC)
    t_notify = datetime(2026, 5, 21, 10, 0, 30, tzinfo=UTC)
    flow.verdict.audit_metadata.created_at = t_triage
    flow.routing.decided_at = t_notify
    monkeypatch.setattr(ic, "run_reactive_flow", lambda _alert: flow)
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="user_service_mysql_down")

    by_stage = {e.stage: e for e in result.timeline}
    # Reactive stages reflect their own recorded times, read off the flow...
    assert by_stage["triage"].ts == t_triage
    assert by_stage["notify"].ts == t_notify
    # ...and the IC-driven RCA stage is stamped after the reactive events.
    assert by_stage["rca"].ts >= t_notify
    # The reconstruction does not collapse triage onto the notify time.
    assert by_stage["triage"].ts != by_stage["notify"].ts


def test_timeline_has_detected_anchor_sorted_and_metrics(monkeypatch):
    """The timeline opens with a 'detected' T0 entry at the alert's own fire
    time, stays chronologically sorted, and the result carries derived
    MTTA/MTTR-style metrics anchored at detection — mirrored on the seed."""
    _stub_flow(monkeypatch, "Sev-1")
    client = _CapturingClient()
    monkeypatch.setattr(ic, "get_client", lambda: client)

    result = command(_alert(), scenario_id="user_service_mysql_down")

    # T0 anchor: first beat is detection, stamped at the alert's timestamp.
    assert result.timeline[0].stage == "detected"
    assert result.timeline[0].ts == datetime(2026, 5, 21, 10, 0, tzinfo=UTC)

    # Entries are non-decreasing by ts regardless of which clock stamped each.
    stamps = [e.ts for e in result.timeline]
    assert stamps == sorted(stamps)

    # Derived metrics present, anchored at detection, with a handoff time on the
    # engaged path — and the seed carries the same metrics for self-containment.
    m = result.metrics
    assert m is not None
    assert m.detected_at == datetime(2026, 5, 21, 10, 0, tzinfo=UTC)
    assert m.time_to_triage_seconds is not None
    assert m.time_to_notify_seconds is not None  # MTTA (on-call paged)
    assert m.time_to_handoff_seconds is not None
    assert m.total_coordination_seconds is not None
    assert result.postmortem_seed is not None
    assert result.postmortem_seed.metrics == m


def test_correlation_lookback_env_override(monkeypatch):
    """RA-007's evidence window is tunable via env; bad/empty/non-positive
    values fall back to the 15-minute default rather than breaking the pull."""
    from datetime import timedelta

    monkeypatch.delenv("AIOPS_IC_CORRELATION_LOOKBACK_MINUTES", raising=False)
    assert ic._correlation_lookback() == timedelta(minutes=15)

    monkeypatch.setenv("AIOPS_IC_CORRELATION_LOOKBACK_MINUTES", "30")
    assert ic._correlation_lookback() == timedelta(minutes=30)

    for bad in ("garbage", "0", "-5", ""):
        monkeypatch.setenv("AIOPS_IC_CORRELATION_LOOKBACK_MINUTES", bad)
        assert ic._correlation_lookback() == timedelta(minutes=15)


def test_reset_state_is_callable():
    # Cascades to RA-001 / RA-002 resets; must not raise.
    ic.reset_state()
