"""Unit + integration tests for the Notification Assembler (RA-005+006).

Covers both merged halves: the routing decision (former RA-005) and the
war-room assembly (former RA-006), plus the single combined message that
``notify`` emits — the routing notification with the war-room link folded in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents.alert_triage import AuditMetadata, TriageVerdict
from agents.notification_assembler import decide, notify
from aiops.tools.chatops import ChatOpsClient, Severity, get_client

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = REPO_ROOT / "agents" / "notification_assembler" / "evals" / "golden.json"


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
    status: str = "Active",
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
        status=status,  # type: ignore[arg-type]
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )


# ─── decide() — routing rules (former RA-005) ──────────────────────────────


def test_sev1_pages_oncall_even_at_night():
    d = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC)).decision

    assert d.chat_severity == Severity.P1
    assert d.channel == "incidents"
    assert "page_oncall" in d.actions
    assert "post_to_chat" in d.actions
    assert d.mentions == ["@oncall@payments.example.com"]


def test_sev2_in_business_hours_chats_team_no_page():
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC)).decision

    assert d.chat_severity == Severity.P2
    assert d.channel == "team-payments"
    assert d.actions == ["post_to_chat"]
    assert "page_oncall" not in d.actions


def test_sev2_after_hours_pages():
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 23, 30, tzinfo=UTC)).decision

    assert d.chat_severity == Severity.P2
    assert d.channel == "incidents"
    assert "page_oncall" in d.actions


def test_sev3_never_pages_and_never_mentions():
    d = decide(_verdict(severity="Sev-3"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC)).decision

    assert d.chat_severity == Severity.P3
    assert d.channel == "ops-daytime"
    assert d.actions == ["post_to_chat"]
    assert d.mentions == []


def test_sev4_goes_to_noise_bucket():
    d = decide(
        _verdict(severity="Sev-4", engineer=None), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC)
    ).decision

    assert d.chat_severity == Severity.INFO
    assert d.channel == "alerts-noise"
    assert d.mentions == []


def test_body_includes_dup_count_when_grouped():
    d = decide(_verdict(severity="Sev-3", dup=7), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC)).decision
    assert "Duplicate alerts grouped: 7" in d.body


# ─── decide() — war-room half (former RA-006) ──────────────────────────────


def test_sev1_assembles_war_room_named_after_incident():
    plan = decide(
        _verdict(severity="Sev-1", incident_id="INC0012345"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    assert plan.war_room is not None
    assert plan.war_room.assembled is True
    assert plan.war_room.channel == "war-room-inc0012345"
    assert plan.war_room.chat_severity == Severity.P1


def test_sev3_warrants_no_war_room():
    plan = decide(_verdict(severity="Sev-3"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC))
    assert plan.war_room is not None
    assert plan.war_room.assembled is False
    assert "below Sev-2" in plan.war_room.reason


def test_suppressed_has_no_war_room_plan():
    plan = decide(
        _verdict(severity="Sev-1", status="Suppressed"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    assert plan.decision.channel == "suppressed"
    assert plan.decision.actions == []
    assert plan.war_room is None


# ─── notify() — ONE combined message through the chatops seam ──────────────


class _RecordingAdapter:
    def __init__(self) -> None:
        self.received: list = []

    def send(self, msg) -> None:
        self.received.append(msg)


@pytest.fixture
def _isolated_chatops():
    """Snapshot the singleton's adapters around each test that uses ``notify``."""
    client = get_client()
    backup = list(client._adapters)  # type: ignore[attr-defined]
    client._adapters.clear()  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client._adapters[:] = backup  # type: ignore[attr-defined]


def test_notify_sends_exactly_one_message_with_war_room_link(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    outcome = notify(
        _verdict(severity="Sev-1", incident_id="INC0012345"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )

    # Exactly one notification — not one per former agent.
    assert len(sink.received) == 1
    msg = sink.received[0]
    assert msg.channel == "incidents"
    assert msg.severity == Severity.P1
    # The single message carries the war-room link + opens the room.
    assert "War room" in msg.body
    assert "war-room-inc0012345" in msg.body
    assert "open_war_room" in msg.actions

    assert outcome.war_room is not None and outcome.war_room.assembled is True
    assert outcome.deliveries["_RecordingAdapter"].ok is True


def test_notify_low_severity_has_no_war_room_section(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    outcome = notify(_verdict(severity="Sev-3"), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC))

    assert len(sink.received) == 1
    assert "War room" not in sink.received[0].body
    assert "open_war_room" not in sink.received[0].actions
    assert outcome.war_room is not None and outcome.war_room.assembled is False


def test_suppressed_verdict_does_not_reach_chatops(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()
    _isolated_chatops.register(sink)

    outcome = notify(
        _verdict(severity="Sev-2", status="Suppressed"),
        now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC),
    )

    assert sink.received == []
    assert outcome.decision.actions == []
    assert outcome.decision.channel == "suppressed"
    assert outcome.deliveries == {}


def test_notify_records_adapter_failures_and_continues(_isolated_chatops: ChatOpsClient):
    sink = _RecordingAdapter()

    class BadAdapter:
        def send(self, msg):
            raise RuntimeError("boom")

    _isolated_chatops.register(sink)
    _isolated_chatops.register(BadAdapter())

    outcome = notify(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))

    assert len(sink.received) == 1
    assert outcome.deliveries["_RecordingAdapter"].ok is True
    assert outcome.deliveries["BadAdapter"].ok is False
    assert "RuntimeError: boom" in outcome.deliveries["BadAdapter"].error


# ─── golden.json regression (flat run() output) ─────────────────────────────


def _load_golden_cases():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _load_golden_cases(), ids=lambda c: c["id"])
def test_golden_case_matches_expected(case: dict):
    from agents.notification_assembler.agent import run

    actual = run(case["input"])
    for key, want in case["expected"].items():
        if key.endswith("_contains"):
            field = key[: -len("_contains")]
            assert want in (actual.get(field) or ""), f"{field} should contain {want!r}"
        else:
            assert actual.get(key) == want, f"{key}: {actual.get(key)!r} != {want!r}"
