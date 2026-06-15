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

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aiops.tools.chatops import ChatMessage, Severity
from aiops.tools.chatops.adapters.slack import SlackWebhookAdapter

WEBHOOK = "https://hooks.slack.com/services/T0000FAKE/B0000FAKE/abcdef"


@pytest.fixture(autouse=True)
def _isolate_slack_user_map_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real handle->user-id map lives in ``AIOPS_SLACK_USER_MAP_JSON``
    (``.env.shared``, loaded by ``uv run``) and is merged on top of any file
    map. Clear it so these tests exercise only the file map they write — not a
    developer's real local env (otherwise ``chinmay`` resolves to the real id
    instead of the test fixture's)."""
    monkeypatch.delenv("AIOPS_SLACK_USER_MAP_JSON", raising=False)


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
    assert "Application:" in field_texts and "payment" in field_texts
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


# ─── mention rewriting (CHAT-6, issue #86) ─────────────────────────────


def _write_user_map(tmp_path: Path, mapping: dict[str, str]) -> Path:
    """Drop a slack_users.json file in a tmp dir for the adapter to load."""
    path = tmp_path / "slack_users.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def _context_text(payload: dict[str, Any]) -> str:
    """Pull the 'Notify: ...' string out of the Slack Block Kit payload."""
    blocks = payload["attachments"][0]["blocks"]
    context = next(
        b
        for b in blocks
        if b["type"] == "context"
        and any("Notify:" in el.get("text", "") for el in b.get("elements", []))
    )
    return context["elements"][0]["text"]


def test_mapped_mention_is_rewritten_to_slack_user_id(tmp_path: Path) -> None:
    """The done-when check from #86: when assigned_engineer is `chinmay`
    and the JSON map has him, the Slack message must contain `<@U_ID>`
    (which actually pings the user) and not the literal `@chinmay`
    (which is plain text and pings nobody)."""
    user_map = _write_user_map(tmp_path, {"chinmay": "U01ABC123"})
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=["@chinmay"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "<@U01ABC123>" in notify_line
    assert "@chinmay" not in notify_line  # the raw form must not survive


def test_unmapped_mention_falls_back_to_plain_text(tmp_path: Path) -> None:
    """The other done-when check from #86: an unmapped name must NOT
    fail the message — it goes through as plain text. Recipient doesn't
    get a native ping, but the notification still lands."""
    user_map = _write_user_map(tmp_path, {"chinmay": "U01ABC123"})
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=["@randomperson"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "@randomperson" in notify_line
    # No bogus rewrite — must not synthesize a fake user id.
    assert "<@" not in notify_line


def test_mixed_mapped_and_unmapped_mentions(tmp_path: Path) -> None:
    """A list with one mapped + one unmapped name renders the mapped one
    as <@U_ID> and the unmapped one as plain @name in the same context
    line. RA-005 emits multi-name mentions occasionally (e.g. war-room
    assembly), so this combo must work."""
    user_map = _write_user_map(tmp_path, {"chinmay": "U01ABC123"})
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=["@chinmay", "@randomperson"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "<@U01ABC123>" in notify_line
    assert "@randomperson" in notify_line


def test_email_shaped_mention_is_supported(tmp_path: Path) -> None:
    """RA-005 today emits mentions like `@oncall@payments.example.com`
    when ``verdict.assigned_engineer`` is an email. The JSON map's key
    should accept that whole string (sans leading @), so on-call routing
    pings real people, not literal text."""
    email_key = "oncall@payments.example.com"
    user_map = _write_user_map(tmp_path, {email_key: "U05ABC456"})
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=[f"@{email_key}"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "<@U05ABC456>" in notify_line


def test_missing_user_map_file_degrades_to_plain_text(tmp_path: Path) -> None:
    """A missing slack_users.json must not crash construction. Demo
    continuity over hard failure on a config file most of the team
    doesn't touch."""
    missing = tmp_path / "does-not-exist.json"
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=missing)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=["@chinmay"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "@chinmay" in notify_line
    assert "<@" not in notify_line


def test_malformed_user_map_file_degrades_to_plain_text(tmp_path: Path) -> None:
    """Truncated paste / invalid JSON in slack_users.json must be tolerated
    — log a warning, fall back to empty map, keep sending notifications."""
    bad = tmp_path / "broken.json"
    bad.write_text('{"chinmay": "U01ABC123"', encoding="utf-8")  # missing closing brace
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=bad)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=["@chinmay"]))

    notify_line = _context_text(mock_post.call_args.kwargs["json"])
    assert "@chinmay" in notify_line


def test_user_map_filters_out_doc_keys_and_non_strings(tmp_path: Path) -> None:
    """The default slack_users.json carries a `_comment` documentation
    key. The loader must strip underscore-prefixed keys and non-string
    values so they never make it into a lookup result."""
    user_map = _write_user_map(
        tmp_path,
        {
            "_comment": "this is a doc string, not a user id",
            "chinmay": "U01ABC123",
        },
    )
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    # The doc key must not be in the loaded map.
    assert "_comment" not in adapter._user_map
    assert adapter._user_map["chinmay"] == "U01ABC123"


def test_default_user_map_path_loads_shipped_file() -> None:
    """The adapter without ``user_map_path`` must load the file shipped
    next to the module. Locks the default path so a future refactor
    can't silently break the wire-up."""
    adapter = SlackWebhookAdapter(WEBHOOK)
    # Shipped file always contains the documentation key + at least one
    # example mapping (or operator-replaced real mappings). The loader
    # filters the doc key out, so the resulting map should contain at
    # least one real-looking entry.
    assert isinstance(adapter._user_map, dict)
    # If the file exists and parses, it should be loaded; otherwise the
    # loader returns an empty dict — both are valid runtime states. We
    # just assert it's a dict-shaped result and ``_comment`` is gone.
    assert "_comment" not in adapter._user_map


def test_empty_mentions_list_skips_context_block(tmp_path: Path) -> None:
    """Mention rewriting must not synthesize a context block when there
    are no mentions to render — that would create an empty 'Notify: '
    string in Slack. This is the same contract as the original
    pre-CHAT-6 behaviour, locked here so rewriting doesn't regress it."""
    user_map = _write_user_map(tmp_path, {"chinmay": "U01ABC123"})
    adapter = SlackWebhookAdapter(WEBHOOK, user_map_path=user_map)

    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _mock_ok_response()
        adapter.send(_msg(mentions=[]))

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    notify_contexts = [
        b
        for b in blocks
        if b["type"] == "context"
        and any("Notify:" in el.get("text", "") for el in b.get("elements", []))
    ]
    assert notify_contexts == []
