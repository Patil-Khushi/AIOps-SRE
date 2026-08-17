"""Tests for the Teams war-room meeting (RA-006).

Sev-1/Sev-2 incidents get a real Teams meeting with calendar invites to the
responders, replacing the Jitsi room. Two properties matter most and are
pinned here:

* the invite is a *working call*, not a broadcast — the on-call engineer is
  always on it and the attendee list is capped;
* a meeting failure must never cost the incident its war room, so every
  failure path degrades instead of raising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from agents.notification_assembler.agent import _meeting_attendees
from agents.notification_assembler.models import InvitedSME, WarRoomAssembly
from aiops.tools.chatops.models import Severity
from aiops.tools.chatops.teams_meeting import create_meeting

WEBHOOK = (
    "https://prod-1.westus.logic.azure.com/workflows/f4k3/triggers/manual"
    "/paths/invoke?api-version=1&sig=s3cr3ts1g"
)


def _sme(source: str, email: str | None, handle: str = "") -> InvitedSME:
    return InvitedSME(
        handle=handle or f"@{(email or source).split('@')[0]}",
        name=source,
        email=email,
        team="Payments Team",
        reason="test",
        source=source,
    )


def _assembly(*smes: InvitedSME) -> WarRoomAssembly:
    return WarRoomAssembly(
        assembled=True,
        channel="war-room-inc1",
        title="War room: payment down",
        chat_severity=Severity.P1,
        invited=list(smes),
        reason="Sev-1",
        assembled_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


# ─── who gets invited ──────────────────────────────────────────────────


def test_oncall_is_always_invited_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # RA-006 appends on-call first, but ranking must not depend on that.
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "2")
    a = _assembly(
        _sme("dependency_owner", "dep@zensar.com"),
        _sme("past_resolver", "past@zensar.com"),
        _sme("oncall", "oncall@zensar.com"),
    )
    assert _meeting_attendees(a)[0] == "oncall@zensar.com"


def test_attendees_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "2")
    a = _assembly(
        _sme("oncall", "oncall@zensar.com"),
        _sme("dependency_owner", "dep@zensar.com"),
        _sme("past_resolver", "past@zensar.com"),
    )
    assert _meeting_attendees(a) == ["oncall@zensar.com", "dep@zensar.com"]


def test_smes_without_email_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Handles are Slack-shaped and mean nothing to a calendar.
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "3")
    a = _assembly(
        _sme("oncall", None, handle="@chinmay"),
        _sme("dependency_owner", "dep@zensar.com"),
    )
    assert _meeting_attendees(a) == ["dep@zensar.com"]


def test_duplicate_emails_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same engineer can be both on-call and a past resolver; inviting
    # them twice would make the connector reject the event.
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "3")
    a = _assembly(
        _sme("oncall", "same@zensar.com"),
        _sme("past_resolver", "SAME@zensar.com", handle="@same2"),
    )
    assert _meeting_attendees(a) == ["same@zensar.com"]


def test_zero_cap_invites_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "0")
    assert _meeting_attendees(_assembly(_sme("oncall", "a@zensar.com"))) == []


def test_unparseable_cap_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_WAR_ROOM_MAX_ATTENDEES", "not-a-number")
    a = _assembly(_sme("oncall", "a@zensar.com"))
    assert _meeting_attendees(a) == ["a@zensar.com"]


# ─── meeting creation ──────────────────────────────────────────────────


def _ok_response() -> Any:
    class R:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

    return R()


def test_missing_webhook_is_a_clean_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", raising=False)
    res = create_meeting(subject="x", attendee_emails=["a@zensar.com"])
    assert res.ok is False
    assert "AIOPS_TEAMS_MEETING_WEBHOOK_URL" in (res.error or "")


def test_no_attendees_does_not_create_an_empty_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", WEBHOOK)
    with patch("aiops.tools.chatops.teams_meeting.httpx.post") as post:
        res = create_meeting(subject="x", attendee_emails=[])
    assert res.ok is False
    post.assert_not_called()


def test_attendees_are_semicolon_joined_and_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", WEBHOOK)
    monkeypatch.delenv("AIOPS_POWER_AUTOMATE_ENV", raising=False)
    with patch("aiops.tools.chatops.teams_meeting.httpx.post", return_value=_ok_response()) as post:
        res = create_meeting(
            subject="[Sev-1] War room",
            attendee_emails=["a@zensar.com", "A@zensar.com", "not-an-email", "b@zensar.com"],
        )
    body = post.call_args.kwargs["json"]
    assert body["attendees"] == "a@zensar.com;b@zensar.com"
    assert body["subject"] == "[Sev-1] War room"
    # The meeting was created even though the join URL could not be read.
    assert res.ok is True
    assert (res.data or {}).get("invited") == ["a@zensar.com", "b@zensar.com"]


def test_trigger_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", WEBHOOK)
    with patch(
        "aiops.tools.chatops.teams_meeting.httpx.post",
        side_effect=httpx.ConnectError("dns flap"),
    ):
        res = create_meeting(subject="x", attendee_emails=["a@zensar.com"])
    assert res.ok is False
    assert "ConnectError" in (res.error or "")


def test_http_failure_does_not_leak_the_signed_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import httpx

    monkeypatch.setenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", WEBHOOK)
    req = httpx.Request("POST", WEBHOOK)
    resp = httpx.Response(403, request=req)
    with patch("aiops.tools.chatops.teams_meeting.httpx.post", return_value=resp):
        with caplog.at_level("ERROR"):
            res = create_meeting(subject="x", attendee_emails=["a@zensar.com"])
    assert res.ok is False
    assert "403" in (res.error or "")
    assert "s3cr3ts1g" not in (res.error or "")
    for record in caplog.records:
        assert "s3cr3ts1g" not in record.getMessage()
