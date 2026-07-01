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
from agents.notification_assembler import agent as na_agent
from agents.notification_assembler import decide, notify
from aiops.tools import ToolResult
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
    # 06:00 UTC = 11:30 IST — business hours on the India-based rotation (#199).
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 6, 0, tzinfo=UTC)).decision

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
    d = decide(
        _verdict(severity="Sev-3", dup=7), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC)
    ).decision
    assert "Duplicate alerts grouped: 7" in d.body


def test_body_omits_dup_count_when_singleton():
    d = decide(
        _verdict(severity="Sev-3", dup=1), now=datetime(2026, 5, 13, 11, 0, tzinfo=UTC)
    ).decision
    assert "Duplicate" not in d.body


def test_audit_trace_records_rule_applied():
    d = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC)).decision

    trace = " | ".join(d.audit_trace)
    assert "Sev-1" in trace
    assert "page" in trace
    assert "business_hours=no" in trace


def test_team_slug_strips_team_suffix_and_lowercases():
    # The slug → channel conversion drives RA-005 channel routing; pin it so a
    # slug regression can't silently degrade channel selection.
    d = decide(
        _verdict(severity="Sev-2", team="Order Experience"),
        now=datetime(2026, 5, 13, 6, 0, tzinfo=UTC),  # 11:30 IST — business hours
    ).decision
    assert d.channel == "team-order-experience"


def test_missing_engineer_yields_empty_mentions():
    d = decide(
        _verdict(severity="Sev-1", engineer=None),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    ).decision
    assert d.mentions == []


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


# ─── #199 business hours default to India Standard Time ────────────────────


def test_business_hours_default_is_india_time():
    # 04:00 UTC = 09:30 IST (in hours) though UTC is after-hours → default IST.
    assert na_agent._in_business_hours_for(datetime(2026, 5, 13, 4, 0, tzinfo=UTC), None) is True
    # 14:00 UTC = 19:30 IST → after hours on the India clock.
    assert na_agent._in_business_hours_for(datetime(2026, 5, 13, 14, 0, tzinfo=UTC), None) is False
    # An unresolvable tz name falls back to IST (not UTC).
    assert (
        na_agent._in_business_hours_for(datetime(2026, 5, 13, 4, 0, tzinfo=UTC), "Not/AZone")
        is True
    )


def test_sev2_routing_defaults_to_india_business_hours(monkeypatch):
    # Mock on-call carries no timezone → IST default. 04:00 UTC = 09:30 IST
    # (in hours) → chat the team, no page (a UTC window would have paged).
    monkeypatch.setattr(na_agent, "_resolve_oncall", lambda v: {"engineer_email": "x@example.com"})
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 4, 0, tzinfo=UTC)).decision
    assert d.channel == "team-payments"
    assert "page_oncall" not in d.actions
    assert d.response_mode == "notify"


def test_sev2_routing_after_hours_in_india_pages(monkeypatch):
    # 14:00 UTC = 19:30 IST → after hours → page the on-call.
    monkeypatch.setattr(na_agent, "_resolve_oncall", lambda v: {"engineer_email": "x@example.com"})
    d = decide(_verdict(severity="Sev-2"), now=datetime(2026, 5, 13, 14, 0, tzinfo=UTC)).decision
    assert d.channel == "incidents"
    assert "page_oncall" in d.actions


# ─── #197 dependency-owner SME invites (war-room half) ─────────────────────


class _FakeRegistry:
    """Canned tool registry for deterministic SME-selection tests.

    Dispatches the three capabilities ``_dependency_owner_smes`` /
    ``_resolve_oncall`` use; everything else (observability) returns not-ok so
    the context pack degrades to 'unavailable' without affecting the SME list.
    """

    def __init__(self, deps, teams, oncall_by_team):
        self._deps = deps
        self._teams = teams
        self._oncall = oncall_by_team

    def call(self, capability, **kw):
        if capability == "itsm.cmdb.dependencies":
            return ToolResult(ok=True, data={"dependencies": self._deps.get(kw.get("service"), [])})
        if capability == "itsm.cmdb.lookup":
            team = self._teams.get(kw.get("service"))
            return ToolResult(ok=bool(team), data={"team": team} if team else None)
        if capability == "oncall.schedule.lookup":
            data = self._oncall.get(kw.get("team"))
            return ToolResult(ok=bool(data), data=data)
        return ToolResult(ok=False, error="not wired in fake")


def test_war_room_invites_dependency_owners(monkeypatch):
    fake = _FakeRegistry(
        deps={"payment": ["currency", "fraud-detection"]},
        teams={"currency": "Pricing Team", "fraud-detection": "Trust and Safety"},
        oncall_by_team={
            "Payments Team": {"engineer_email": "chinmay@example.com", "slack_handle": "@chinmay"},
            "Pricing Team": {"engineer_email": "riya@example.com", "slack_handle": "@riya"},
            "Trust and Safety": {"engineer_email": "arjun@example.com", "slack_handle": "@arjun"},
        },
    )
    monkeypatch.setattr(na_agent, "get_registry", lambda: fake)
    wr = na_agent.decide_war_room(
        _verdict(severity="Sev-1", service="payment", team="Payments Team", incident_id="INC1"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    handles = [s.handle for s in wr.invited]
    sources = {s.handle: s.source for s in wr.invited}
    assert handles == ["@chinmay", "@riya", "@arjun"]
    assert sources["@chinmay"] == "oncall"
    assert sources["@riya"] == "dependency_owner"
    assert sources["@arjun"] == "dependency_owner"


def test_war_room_dependency_owner_deduped_against_oncall(monkeypatch):
    # A dependency whose on-call is the SAME person as the primary on-call is
    # invited only once.
    fake = _FakeRegistry(
        deps={"payment": ["currency"]},
        teams={"currency": "Pricing Team"},
        oncall_by_team={
            "Payments Team": {"engineer_email": "chinmay@example.com", "slack_handle": "@chinmay"},
            "Pricing Team": {"engineer_email": "chinmay@example.com", "slack_handle": "@chinmay"},
        },
    )
    monkeypatch.setattr(na_agent, "get_registry", lambda: fake)
    wr = na_agent.decide_war_room(
        _verdict(severity="Sev-1", service="payment", team="Payments Team"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    assert [s.handle for s in wr.invited] == ["@chinmay"]


def test_war_room_dependency_lookup_failure_is_skipped(monkeypatch):
    # An unresolvable dependency (no CMDB team) is skipped; on-call still invited.
    fake = _FakeRegistry(
        deps={"payment": ["ghost-service"]},
        teams={},
        oncall_by_team={
            "Payments Team": {"engineer_email": "chinmay@example.com", "slack_handle": "@chinmay"},
        },
    )
    monkeypatch.setattr(na_agent, "get_registry", lambda: fake)
    wr = na_agent.decide_war_room(
        _verdict(severity="Sev-1", service="payment", team="Payments Team"),
        now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC),
    )
    assert [s.handle for s in wr.invited] == ["@chinmay"]


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
