"""Tests for the PagerDuty Events API v2 adapter (CHAT-5, issue #85).

Covers the four explicit Done-when checks from #85:

1. A page-flagged ChatMessage triggers exactly one HTTP POST to PD with
   the right payload shape.
2. A non-page ChatMessage (no ``page_oncall`` action) triggers zero POSTs.
3. Two messages for the same incident_id collide on the same dedup_key.
4. Empty / invalid integration key raises at construction (so misconfig
   is caught at server startup, not on the first Sev-1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from aiops.tools.chatops import ChatMessage, ChatOpsClient, Severity
from aiops.tools.chatops.adapters.pagerduty import (
    PAGE_ACTIONS,
    PagerDutyAdapter,
)

_FAKE_KEY = "y" * 32
_FIXED_TIME = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)


def _msg(
    *,
    severity: Severity = Severity.P1,
    actions: list[str] | None = None,
    incident_id: str | None = None,
    service: str | None = "payment",
    title: str = "Payment service down",
) -> ChatMessage:
    return ChatMessage(
        channel="incidents",
        severity=severity,
        title=title,
        body="100% 5xx for the last 2 minutes",
        incident_id=incident_id,
        service=service,
        mentions=["@oncall@payments.example.com"],
        actions=actions if actions is not None else ["page_oncall", "post_to_chat"],
        timestamp=_FIXED_TIME,
    )


# ─── construction ──────────────────────────────────────────────────────────


def test_empty_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("")


def test_too_short_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("short")


def test_repr_does_not_leak_integration_key():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    representation = repr(adapter)
    assert _FAKE_KEY not in representation
    assert "***" in representation


# ─── send() filter behaviour ───────────────────────────────────────────────


def test_send_skips_when_actions_lacks_page_oncall():
    """Chat-only message (Sev-3 daytime) must not create a PD incident."""
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        adapter.send(_msg(severity=Severity.P3, actions=["post_to_chat"]))
        mock_post.assert_not_called()


def test_send_skips_when_actions_empty():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        adapter.send(_msg(actions=[]))
        mock_post.assert_not_called()


def test_send_fires_when_actions_contains_page_oncall():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    with patch(
        "aiops.tools.chatops.adapters.pagerduty.httpx.post",
        return_value=mock_response,
    ) as mock_post:
        adapter.send(_msg())
        assert mock_post.call_count == 1


# ─── payload shape ─────────────────────────────────────────────────────────


def test_payload_includes_required_pd_fields():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg())

        kwargs = mock_post.call_args.kwargs
        payload = kwargs["json"]

        assert payload["routing_key"] == _FAKE_KEY
        assert payload["event_action"] == "trigger"
        assert "dedup_key" in payload
        body = payload["payload"]
        assert body["summary"] == "Payment service down"
        assert body["source"] == "payment"
        assert body["severity"] == "critical"  # P1 → critical
        assert body["component"] == "payment"
        assert body["group"] == "incidents"
        assert body["custom_details"]["chat_severity"] == "p1"
        assert "page_oncall" in body["custom_details"]["actions"]


def test_severity_maps_correctly_to_pd_levels():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    cases = [
        (Severity.P0, "critical"),
        (Severity.P1, "critical"),
        (Severity.P2, "error"),
        (Severity.P3, "warning"),
        (Severity.INFO, "info"),
    ]
    for chat_sev, expected_pd in cases:
        with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            adapter.send(_msg(severity=chat_sev, actions=["page_oncall"]))
            sent = mock_post.call_args.kwargs["json"]["payload"]["severity"]
            assert sent == expected_pd, f"{chat_sev} should map to {expected_pd}, got {sent}"


def test_falls_back_to_source_string_when_service_missing():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg(service=None))
        body = mock_post.call_args.kwargs["json"]["payload"]
        assert body["source"] == "adaptive-aiops/RA-005"
        assert body["component"] == "unknown"


# ─── dedup ─────────────────────────────────────────────────────────────────


def test_same_incident_id_produces_same_dedup_key():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    keys = []
    for _ in range(2):
        with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            adapter.send(_msg(incident_id="INC-1234"))
            keys.append(mock_post.call_args.kwargs["json"]["dedup_key"])

    assert keys[0] == keys[1] == "aiops:incident:INC-1234"


def test_no_incident_id_falls_back_to_service_title_hash():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg(incident_id=None, service="payment", title="A"))
        key_a = mock_post.call_args.kwargs["json"]["dedup_key"]
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg(incident_id=None, service="payment", title="A"))
        key_a_again = mock_post.call_args.kwargs["json"]["dedup_key"]
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg(incident_id=None, service="payment", title="B"))
        key_b = mock_post.call_args.kwargs["json"]["dedup_key"]

    assert key_a == key_a_again, "same service+title must dedup"
    assert key_a != key_b, "different titles must not collide"
    assert key_a.startswith("aiops:hash:")


# ─── error path ────────────────────────────────────────────────────────────


def test_http_error_is_raised_so_chatops_client_can_isolate():
    import httpx

    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("dns is down")
        with pytest.raises(httpx.HTTPError):
            adapter.send(_msg())


# ─── seam integration ──────────────────────────────────────────────────────


def test_plays_nicely_alongside_other_adapters():
    """When registered alongside a non-page sink, both receive the message
    but only the PagerDuty adapter actually POSTs (other sinks are not
    bound by PAGE_ACTIONS — they log everything)."""

    class _Recorder:
        def __init__(self) -> None:
            self.received: list[ChatMessage] = []

        def send(self, m: ChatMessage) -> None:
            self.received.append(m)

    recorder = _Recorder()
    pd = PagerDutyAdapter(_FAKE_KEY)
    client = ChatOpsClient()
    client.register(recorder)
    client.register(pd)

    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        # Sev-1 page-worthy message
        client.send(_msg())
        # Sev-3 chat-only
        client.send(_msg(severity=Severity.P3, actions=["post_to_chat"]))

    assert len(recorder.received) == 2, "recorder sees every message"
    assert mock_post.call_count == 1, "PD only fires on the page-worthy one"


def test_page_actions_set_is_extensible():
    """v2 escalation actions (e.g. 'page_backup') should be addable
    without touching call-site code — they just go in PAGE_ACTIONS."""
    assert "page_oncall" in PAGE_ACTIONS
    assert isinstance(PAGE_ACTIONS, frozenset)  # immutable on purpose
