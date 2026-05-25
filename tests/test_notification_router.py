"""Unit + integration tests for the Notification Router (RA-005)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents.alert_triage import AuditMetadata, TriageVerdict
from agents.notification_router import RoutingDecision, RoutingOutcome, decide, route
from aiops.tools.chatops import ChatOpsClient, Severity, get_client

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = REPO_ROOT / "agents" / "notification_router" / "evals" / "golden.json"


def _verdict(
    *,
    severity: str = "Sev-2",
    service: str = "payment",
    team: str = "Payments Team",
    engineer: str | None = "oncall@payments.example.com",
    runbook: str | None = None,
    dup: int = 1,
    summary: str = "alert summary",
    incident_id: str | None = None,
) -> TriageVerdict:
    return TriageVerdict(
        incident_id=incident_id,
        affected_service=service,
        severity=severity,  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary=summary,
        assigned_team=team,
        assigned_engineer=engineer,
        recommended_runbook=runbook,
        duplicate_alert_count=dup,
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )


# ─── decide() — pure rules ────────────────────────────────────────────────


def test_sev1_pages_oncall_even_at_night():
    d = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))

    assert d.chat_severity == Severity.P1
    assert d.channel == "incidents"
    assert "page_oncall" in d.actions
    assert "post_to_chat" in d.actions
    assert d.mentions == ["@oncall@payments.example.com"]


def test_sev2_in_business_hours_chats_team_no_page():
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC))

    assert d.chat_severity == Severity.P2
    assert d.channel == "team-payments"
    assert d.actions == ["post_to_chat"]
    assert "page_oncall" not in d.actions


def test_sev2_after_hours_pages():
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 23, 30, tzinfo=UTC))

    assert d.chat_severity == Severity.P2
    assert d.channel == "incidents"
    assert "page_oncall" in d.actions


def test_sev3_never_pages_and_never_mentions():
    d = decide(_verdict(severity="Sev-3"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC))

    assert d.chat_severity == Severity.P3
    assert d.channel == "ops-daytime"
    assert d.actions == ["post_to_chat"]
    assert d.mentions == []


def test_sev4_goes_to_noise_bucket():
    d = decide(
        _verdict(severity="Sev-4", engineer=None), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC)
    )

    assert d.chat_severity == Severity.INFO
    assert d.channel == "alerts-noise"
    assert d.mentions == []


def test_audit_trace_records_rule_applied():
    d = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))

    trace = " | ".join(d.audit_trace)
    assert "Sev-1" in trace
    assert "page" in trace
    assert "business_hours=no" in trace


def test_team_slug_strips_team_suffix_and_lowercases():
    d = decide(
        _verdict(severity="Sev-2", team="Order Experience"),
        now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
    )
    assert d.channel == "team-order-experience"


def test_missing_engineer_yields_empty_mentions():
    d = decide(
        _verdict(severity="Sev-1", engineer=None),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    assert d.mentions == []


def test_body_includes_dup_count_when_grouped():
    d = decide(_verdict(severity="Sev-3", dup=7), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC))
    assert "Duplicate alerts grouped: 7" in d.body


def test_body_omits_dup_count_when_singleton():
    d = decide(_verdict(severity="Sev-3", dup=1), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC))
    assert "Duplicate" not in d.body


# ─── route() — end-to-end through the chatops seam ────────────────────────


class _RecordingAdapter:
    def __init__(self) -> None:
        self.received: list = []

    def send(self, msg) -> None:
        self.received.append(msg)


@pytest.fixture
def _isolated_chatops():
    """Snapshot the singleton's adapters around each test that uses ``route``."""
    client = get_client()
    backup = list(client._adapters)  # type: ignore[attr-defined]
    client._adapters.clear()  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client._adapters[:] = backup  # type: ignore[attr-defined]


def test_route_sends_one_chatmessage_through_the_seam(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    outcome = route(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))

    assert len(sink.received) == 1
    msg = sink.received[0]
    assert msg.channel == "incidents"
    assert msg.severity == Severity.P1
    assert "Payment" in msg.body or "payment" in msg.body
    assert msg.service == "payment"

    assert list(outcome.deliveries) == ["_RecordingAdapter"]
    delivery = outcome.deliveries["_RecordingAdapter"]
    assert delivery.ok is True
    assert delivery.error is None


def test_route_propagates_incident_id(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    outcome = route(
        _verdict(severity="Sev-2", incident_id="INC-1234"),
        now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
    )

    assert sink.received[0].incident_id == "INC-1234"
    assert outcome.deliveries["_RecordingAdapter"].ok is True


def test_route_records_adapter_failures_and_continues(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()

    class BadAdapter:
        def send(self, msg):
            raise RuntimeError("boom")

    _isolated_chatops.register(sink)
    _isolated_chatops.register(BadAdapter())

    outcome = route(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))

    assert len(sink.received) == 1
    assert outcome.deliveries["_RecordingAdapter"].ok is True
    assert outcome.deliveries["_RecordingAdapter"].error is None
    assert outcome.deliveries["BadAdapter"].ok is False
    assert "RuntimeError: boom" in outcome.deliveries["BadAdapter"].error


def test_suppressed_verdict_does_not_reach_chatops(_isolated_chatops: ChatOpsClient):
    """A Suppressed verdict must short-circuit before any chatops emit (RA-005 DoD)."""
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    v = _verdict(severity="Sev-2")
    v = v.model_copy(update={"status": "Suppressed"})

    outcome = route(v, now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC))

    assert sink.received == []
    assert outcome.decision.actions == []
    assert outcome.decision.channel == "suppressed"
    assert outcome.deliveries == {}


# ─── golden.json regression ────────────────────────────────────────────────


def _load_golden_cases():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _load_golden_cases(), ids=lambda c: c["id"])
def test_golden_case_matches_expected(case: dict):
    verdict = TriageVerdict.model_validate(case["input"]["verdict"])
    now = datetime.fromisoformat(case["input"]["now"].replace("Z", "+00:00"))

    d: RoutingDecision = decide(verdict, now=now)

    expected = case["expected"]
    assert d.chat_severity == Severity(expected["chat_severity"])
    assert d.channel == expected["channel"]
    assert d.actions == expected["actions"]
    assert d.mentions == expected["mentions"]
