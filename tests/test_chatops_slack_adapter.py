"""Smoke tests for the Slack webhook adapter (CHAT-1, issue #81).

Mocks ``httpx.post`` so tests never hit the network. Asserts:

- Construction validates the webhook URL (only Slack hosts accepted)
- The payload shape is Block Kit (header + fields + body + context)
- Severity → color mapping is the canonical RA-005 set
- Long titles / bodies truncate instead of failing
- HTTP failures surface as exceptions so ``ChatOpsClient`` can log them
- ``__repr__`` does NOT leak the webhook URL
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aiops.tools.chatops import ChatMessage, Severity
from aiops.tools.chatops.adapters.slack import SlackWebhookAdapter

WEBHOOK = "https://hooks.slack.com/services/T0000FAKE/B0000FAKE/abcdef"


def _msg(**overrides: Any) -> ChatMessage:
    defaults: dict[str, Any] = {
        "channel": "incidents",
        "severity": Severity.P1,
        "title": "Sev-1: PaymentErrorRateHigh",
        "body": "Service: payment\nSeverity: Sev-1\nOn-call: chinmay",
        "incident_id": "INC0010099",
        "service": "payment",
        "mentions": ["@chinmay"],
        "timestamp": datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return ChatMessage(**defaults)


def _mock_ok_response() -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    return r


# ─── construction ──────────────────────────────────────────────────────


def test_constructor_rejects_non_slack_url() -> None:
    with pytest.raises(ValueError) as ei:
        SlackWebhookAdapter("https://example.com/webhook")
    assert "Slack incoming webhook" in str(ei.value)


def test_constructor_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        SlackWebhookAdapter("")


def test_repr_does_not_leak_webhook_url() -> None:
    a = SlackWebhookAdapter(WEBHOOK)
    text = repr(a)
    assert "T0000FAKE" not in text
    assert "B0000FAKE" not in text
    assert "abcdef" not in text


def test_adapter_name_is_stable() -> None:
    # CHAT-4 will key adapter results by this name; lock it down.
    assert SlackWebhookAdapter.name == "slack"


# ─── payload shape ─────────────────────────────────────────────────────


def test_send_posts_to_webhook_url_with_block_kit_payload() -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg())

    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == WEBHOOK

    payload = kwargs["json"]
    # Top-level fallback for mobile push / screen readers
    assert "Sev-1" in payload["text"]
    assert "PaymentErrorRateHigh" in payload["text"]

    # Exactly one attachment with color + blocks
    assert len(payload["attachments"]) == 1
    att = payload["attachments"][0]
    assert att["color"] == "danger"  # P1 → red

    blocks = att["blocks"]
    block_types = [b["type"] for b in blocks]
    # Header → fields-section → body-section → context (mentions)
    assert block_types[0] == "header"
    assert "section" in block_types
    assert "context" in block_types
    assert "PaymentErrorRateHigh" in blocks[0]["text"]["text"]


def test_send_includes_service_channel_incident_fields_when_present() -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg())

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    fields_section = next((b for b in blocks if b["type"] == "section" and "fields" in b), None)
    assert fields_section is not None
    field_texts = " ".join(f["text"] for f in fields_section["fields"])
    assert "Service:" in field_texts and "payment" in field_texts
    assert "Channel:" in field_texts and "incidents" in field_texts
    assert "Incident:" in field_texts and "INC0010099" in field_texts


def test_send_omits_fields_section_when_routing_context_is_empty() -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(channel="", service=None, incident_id=None))

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    fields_sections = [b for b in blocks if b["type"] == "section" and "fields" in b]
    assert fields_sections == []


def test_send_omits_context_when_mentions_empty() -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=[]))

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    assert all(b["type"] != "context" for b in blocks)


# ─── severity → color mapping (the canonical RA-005 mapping) ───────────


@pytest.mark.parametrize(
    "severity, expected_color",
    [
        (Severity.P0, "danger"),
        (Severity.P1, "danger"),
        (Severity.P2, "warning"),
        (Severity.P3, "#facc15"),
        (Severity.INFO, "#94a3b8"),
    ],
)
def test_color_mapping_by_severity(severity: Severity, expected_color: str) -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(severity=severity))
    color = mock_post.call_args.kwargs["json"]["attachments"][0]["color"]
    assert color == expected_color


# ─── truncation (Slack hard limits) ────────────────────────────────────


def test_long_title_truncated_to_slack_header_limit() -> None:
    long_title = "X" * 500
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(title=long_title))

    header_text = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"][0]["text"]["text"]
    assert len(header_text) <= 150


def test_long_body_truncated_to_slack_section_limit() -> None:
    long_body = "Y" * 5000
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(body=long_body))

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    body_section = next(
        b for b in blocks if b["type"] == "section" and "text" in b and "fields" not in b
    )
    assert len(body_section["text"]["text"]) <= 2900


# ─── failure handling ──────────────────────────────────────────────────


def test_send_raises_on_http_failure() -> None:
    adapter = SlackWebhookAdapter(WEBHOOK)
    failing = MagicMock()
    failing.raise_for_status = MagicMock(side_effect=httpx.HTTPError("boom"))
    with patch("aiops.tools.chatops.adapters.slack.httpx.post", return_value=failing):
        with pytest.raises(httpx.HTTPError):
            adapter.send(_msg())
