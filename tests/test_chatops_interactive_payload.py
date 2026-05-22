"""Adapter rendering of HITL interactive prompts (HITL-1, issue #77).

The chatops seam now ferries an optional :class:`InteractivePrompt` payload
alongside ``ChatMessage``.  Two assertions matter:

* The Slack adapter renders approve/deny Block Kit buttons whose ``value``
  encodes ``"<approval_id>|<verdict>"`` — Slack POSTs that back to our
  signed callback.
* The JSONL audit-log adapter writes the interactive payload as data —
  the seam guarantees every adapter sees the same wire shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiops.tools.chatops import ChatMessage, InteractivePrompt, Severity
from aiops.tools.chatops.adapters.jsonfile import JsonFileChatOpsAdapter
from aiops.tools.chatops.adapters.slack import SlackWebhookAdapter

WEBHOOK = "https://hooks.slack.com/services/T0/B0/abc"


def _interactive_msg() -> ChatMessage:
    return ChatMessage(
        channel="hitl-approvals",
        severity=Severity.P1,
        title="HITL approval requested: automation.runbook.execute",
        body="Restart product-catalog?",
        interactive=InteractivePrompt(
            approval_id="abc123",
            action="automation.runbook.execute",
            expires_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
        ),
    )


def _ok():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    return r


def test_slack_payload_includes_approve_and_deny_buttons():
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _ok()
        adapter.send(_interactive_msg())

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    actions_block = next((b for b in blocks if b["type"] == "actions"), None)
    assert actions_block is not None
    assert actions_block["block_id"] == "hitl_approval::abc123"

    buttons = {b["action_id"]: b for b in actions_block["elements"]}
    assert set(buttons) == {"hitl_approve", "hitl_deny"}
    assert buttons["hitl_approve"]["value"] == "abc123|approve"
    assert buttons["hitl_deny"]["value"] == "abc123|deny"
    # Deny button is gated behind a confirm dialog so accidental clicks
    # don't block a destructive but otherwise correct fix step.
    assert "confirm" in buttons["hitl_deny"]


def test_slack_payload_omits_actions_when_message_is_not_interactive():
    msg = ChatMessage(channel="x", severity=Severity.INFO, title="just a note")
    adapter = SlackWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.slack.httpx.post") as mock_post:
        mock_post.return_value = _ok()
        adapter.send(msg)

    blocks = mock_post.call_args.kwargs["json"]["attachments"][0]["blocks"]
    assert all(b["type"] != "actions" for b in blocks)


def test_jsonfile_adapter_writes_interactive_payload(tmp_path: Path):
    path = tmp_path / "chatops.jsonl"
    JsonFileChatOpsAdapter(path).send(_interactive_msg())

    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["interactive"] == {
        "approval_id": "abc123",
        "action": "automation.runbook.execute",
        "expires_at": "2026-05-22T12:00:00+00:00",
        "prompt_kind": "hitl_approval",
    }


def test_jsonfile_adapter_writes_null_interactive_for_plain_messages(tmp_path: Path):
    path = tmp_path / "chatops.jsonl"
    JsonFileChatOpsAdapter(path).send(
        ChatMessage(channel="x", severity=Severity.INFO, title="plain")
    )
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["interactive"] is None
