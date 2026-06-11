"""Tests for the Slack Bot adapter (ON-CALL-2).

The adapter sends Direct Messages to mentioned users via Slack's
``chat.postMessage`` API. It only fires when ``page_oncall`` is in
the message's ``actions`` list — anti-spam for low-severity alerts.

All HTTP calls are mocked via ``respx`` / ``httpx.MockTransport``-style
patching of ``httpx.post``. No real Slack API calls are made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from aiops.tools.chatops import ChatMessage, Severity
from aiops.tools.chatops.adapters.slack_bot import SlackBotAdapter

# ─── Fixtures + helpers ───────────────────────────────────────────────────

VALID_TOKEN = "xoxb-FAKE-TEST-TOKEN-DO-NOT-USE"

USER_MAP = {
    "_comment": "test fixture",
    "chinmay-kotkar": "U0CHINMAY",
    "khushi-patil": "U0KHUSHI",
    "shravani-joshi": "U0SHRAVANI",
    "chinmay.kotkar@example.com": "U0CHINMAY",
    "khushi.patil@example.com": "U0KHUSHI",
}


@pytest.fixture
def user_map_file(tmp_path: Path) -> Path:
    path = tmp_path / "slack_users.json"
    path.write_text(json.dumps(USER_MAP), encoding="utf-8")
    return path


def _msg(
    *,
    actions: list[str],
    mentions: list[str],
    severity: Severity = Severity.P1,
    title: str = "payment service producing errors",
    service: str = "payment",
    channel: str = "incidents",
    body: str = "Service: payment\nSeverity: Sev-1",
    incident_id: str | None = "INC-1234",
) -> ChatMessage:
    return ChatMessage(
        channel=channel,
        severity=severity,
        title=title,
        body=body,
        incident_id=incident_id,
        service=service,
        mentions=mentions,
        actions=actions,
        timestamp=datetime(2026, 6, 9, 7, 30, tzinfo=UTC),
    )


_FAKE_REQUEST = httpx.Request("POST", "https://slack.com/api/chat.postMessage")


def _fake_ok_response() -> httpx.Response:
    """Simulate a 200 OK from Slack chat.postMessage.

    A bare ``httpx.Response`` cannot run ``raise_for_status`` without a
    request attached, so we set one. The actual URL is irrelevant — it
    just satisfies httpx's internal invariants.
    """
    return httpx.Response(200, json={"ok": True, "ts": "1717920000.000100"}, request=_FAKE_REQUEST)


def _fake_error_response(error_code: str = "channel_not_found") -> httpx.Response:
    """Simulate a 200 from Slack but with ``ok: false`` (Slack's idiom)."""
    return httpx.Response(200, json={"ok": False, "error": error_code}, request=_FAKE_REQUEST)


# ─── Constructor validation ───────────────────────────────────────────────


def test_constructor_rejects_missing_token(user_map_file: Path):
    with pytest.raises(ValueError, match="xoxb-"):
        SlackBotAdapter("", user_map_path=user_map_file)


def test_constructor_rejects_user_token(user_map_file: Path):
    """User OAuth tokens (xoxp-) should be rejected — they're not bot tokens."""
    with pytest.raises(ValueError, match="xoxb-"):
        SlackBotAdapter("xoxp-fake-user-token", user_map_path=user_map_file)


def test_constructor_accepts_valid_bot_token(user_map_file: Path):
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    assert a is not None


def test_repr_does_not_leak_token(user_map_file: Path):
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    assert VALID_TOKEN not in repr(a)
    assert "***" in repr(a)


# ─── send() filter rules ──────────────────────────────────────────────────


def test_send_skips_when_no_page_oncall_action(user_map_file: Path):
    """Chat-only messages (Sev-3 daytime, Sev-4 noise) must NOT trigger a DM."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["post_to_chat"], mentions=["@khushi-patil"])

    with patch("aiops.tools.chatops.adapters.slack_bot.httpx.post") as p:
        a.send(msg)
        p.assert_not_called()


def test_send_skips_when_no_mentions(user_map_file: Path):
    """Page action without a mention has nobody to DM."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=[])

    with patch("aiops.tools.chatops.adapters.slack_bot.httpx.post") as p:
        a.send(msg)
        p.assert_not_called()


def test_send_skips_unmapped_mentions_silently(user_map_file: Path):
    """Mention that has no Slack user_id → log + skip, no HTTP call."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@unknown-person"])

    with patch("aiops.tools.chatops.adapters.slack_bot.httpx.post") as p:
        a.send(msg)
        p.assert_not_called()


def test_send_dms_each_mapped_mention(user_map_file: Path):
    """One mention → one chat.postMessage call against the resolved user_id."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    p.assert_called_once()
    args, kwargs = p.call_args
    payload = kwargs.get("json") or args[1]
    assert payload["channel"] == "U0KHUSHI"


def test_send_dms_multiple_mentions(user_map_file: Path):
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(
        actions=["page_oncall"],
        mentions=["@khushi-patil", "@chinmay-kotkar"],
    )

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    assert p.call_count == 2
    channels = [c.kwargs["json"]["channel"] for c in p.call_args_list]
    assert set(channels) == {"U0KHUSHI", "U0CHINMAY"}


def test_send_strips_leading_at_from_mention(user_map_file: Path):
    """Lookup should work for both ``"@khushi-patil"`` and ``"khushi-patil"``."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["khushi-patil"])

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    p.assert_called_once()
    assert p.call_args.kwargs["json"]["channel"] == "U0KHUSHI"


def test_send_accepts_email_keyed_mention(user_map_file: Path):
    """The user map keys both handle and email; either works."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(
        actions=["page_oncall"],
        mentions=["@khushi.patil@example.com"],
    )

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    p.assert_called_once()
    assert p.call_args.kwargs["json"]["channel"] == "U0KHUSHI"


# ─── Payload shape ─────────────────────────────────────────────────────────


def test_payload_includes_authorization_header(user_map_file: Path):
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    headers = p.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {VALID_TOKEN}"
    assert "application/json" in headers["Content-Type"]


def test_payload_contains_fallback_text_and_blocks(user_map_file: Path):
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"], severity=Severity.P1)

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    payload = p.call_args.kwargs["json"]
    assert payload["text"].startswith("[p1]")
    assert "attachments" in payload
    assert payload["attachments"][0]["color"] == "danger"  # P1 → danger
    block_types = [b["type"] for b in payload["attachments"][0]["blocks"]]
    assert "header" in block_types
    assert "section" in block_types  # fields / body


def test_payload_targets_user_id_as_channel(user_map_file: Path):
    """The CRUCIAL thing: channel must be the user's ID, not a channel name.

    chat.postMessage with channel=<U_ID> auto-opens a DM. Anything else
    posts to a channel.
    """
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_ok_response(),
    ) as p:
        a.send(msg)

    assert p.call_args.kwargs["json"]["channel"] == "U0KHUSHI"
    # NOT "incidents" or "khushi-patil" or anything else


# ─── Error handling ────────────────────────────────────────────────────────


def test_send_raises_when_slack_returns_ok_false(user_map_file: Path):
    """Slack's ``ok: false`` response should be treated as failure."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=_fake_error_response("invalid_auth"),
    ):
        with pytest.raises(RuntimeError, match="invalid_auth"):
            a.send(msg)


def test_send_raises_on_http_error(user_map_file: Path):
    """Network / 5xx errors should propagate (ChatOpsClient catches per-adapter)."""
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=user_map_file)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    def _raise_http(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectTimeout("simulated network timeout")

    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        side_effect=_raise_http,
    ):
        with pytest.raises(httpx.HTTPError):
            a.send(msg)


# ─── User-map permissiveness ─────────────────────────────────────────────


def test_missing_user_map_file_is_tolerated(tmp_path: Path):
    """A missing user-map file degrades gracefully: no DMs, no crash."""
    missing = tmp_path / "does-not-exist.json"
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=missing)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch("aiops.tools.chatops.adapters.slack_bot.httpx.post") as p:
        a.send(msg)
        p.assert_not_called()  # nobody to look up → no API call


def test_malformed_user_map_is_tolerated(tmp_path: Path):
    """A corrupt JSON user map degrades to empty map, not crash."""
    bad = tmp_path / "slack_users.json"
    bad.write_text("{this is not json}", encoding="utf-8")
    a = SlackBotAdapter(VALID_TOKEN, user_map_path=bad)
    msg = _msg(actions=["page_oncall"], mentions=["@khushi-patil"])

    with patch("aiops.tools.chatops.adapters.slack_bot.httpx.post") as p:
        a.send(msg)
        p.assert_not_called()
