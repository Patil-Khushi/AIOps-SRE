"""Slack webhook adapter for the chatops seam (CHAT-1, issue #81).

Posts every ``ChatMessage`` as a colored Block Kit message to a Slack
Incoming Webhook URL. The vendor coupling is contained in this one file —
agents never import ``slack_sdk``; they emit ``ChatMessage`` and let the
seam fan out to whichever adapters are registered.

Setup (one-time, ~5 min, free):

1. Create a free Slack workspace at slack.com (or reuse an existing one).
2. ``api.slack.com/apps`` → "Create New App" → "From scratch" → pick workspace.
3. Enable "Incoming Webhooks" → "Add New Webhook to Workspace" → pick a channel.
4. Copy the webhook URL into ``.env`` as
   ``AIOPS_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...``.

The dev server's ``_register_chatops_adapters`` hook reads the env var and
registers this adapter only when the URL is set, so the demo runs fine
without Slack configured.

Failure handling:
    Per-message failures log + raise. ``ChatOpsClient.send`` catches
    per-adapter exceptions so one broken sink (Slack rate-limited, DNS
    flap, expired webhook) can never block the JSONL audit log or the
    WebSocket dashboard from receiving the same message.

Secret hygiene:
    The webhook URL is never logged or returned in responses. ``__repr__``
    is overridden to redact the URL host path so accidental ``logger.info(%r)``
    of the adapter object can't leak it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..models import ChatMessage, Severity

logger = logging.getLogger(__name__)

# Severity → Slack attachment color. P0/P1 use Slack's named ``danger``
# (red) and P2 uses ``warning`` (orange) so they integrate with the
# recipient's theme; P3 and INFO use hex because Slack has no named
# yellow/grey that matches our muted-priority intent.
_COLOR_BY_SEVERITY: dict[Severity, str] = {
    Severity.P0: "danger",  # red — critical
    Severity.P1: "danger",  # red — page-worthy
    Severity.P2: "warning",  # orange — needs attention
    Severity.P3: "#facc15",  # yellow — daytime triage
    Severity.INFO: "#94a3b8",  # slate-grey — quiet log
}

_FALLBACK_COLOR = "#94a3b8"
_WEBHOOK_PREFIX = "https://hooks.slack.com/"
_TIMEOUT = float(os.environ.get("AIOPS_SLACK_TIMEOUT", "5"))

# Slack hard limits (current as of 2026 Block Kit reference). We truncate
# instead of failing because dropping a message is worse than dropping
# trailing context.
_HEADER_MAX_CHARS = 150
_SECTION_TEXT_MAX_CHARS = 2900
_MAX_FIELDS_PER_SECTION = 10


class SlackWebhookAdapter:
    """POST a ``ChatMessage`` as a colored Block Kit payload to a Slack webhook.

    The Slack channel is bound to the webhook at creation time in the
    Slack admin UI — this adapter does NOT choose a channel per message.
    The agent-routing ``msg.channel`` (e.g. ``"incidents"``,
    ``"team-payments"``) is surfaced as a Block Kit field so the recipient
    can see what RA-005 *intended*, even when every notification lands
    in the same Slack channel today.
    """

    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url or not webhook_url.startswith(_WEBHOOK_PREFIX):
            raise ValueError(
                "SlackWebhookAdapter requires a Slack incoming webhook URL "
                f"(must start with {_WEBHOOK_PREFIX!r}). "
                f"Got: {webhook_url[:30]!r}..."
            )
        self._webhook_url = webhook_url

    def send(self, msg: ChatMessage) -> None:
        payload = self._build_payload(msg)
        try:
            r = httpx.post(self._webhook_url, json=payload, timeout=_TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            # Don't log the URL (secret). ChatOpsClient re-logs with adapter
            # context anyway.
            logger.error("slack adapter: post failed for %r: %s", msg.title, exc)
            raise

    def __repr__(self) -> str:
        return "SlackWebhookAdapter(webhook=https://hooks.slack.com/services/***)"

    # ─── payload ─────────────────────────────────────────────────────────

    def _build_payload(self, msg: ChatMessage) -> dict[str, Any]:
        color = _COLOR_BY_SEVERITY.get(msg.severity, _FALLBACK_COLOR)
        fallback = f"[{msg.severity.value}] {msg.title}"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": msg.title[:_HEADER_MAX_CHARS],
                },
            }
        ]

        # Routing-context fields (service / channel / incident). Each is
        # only added if populated so the section doesn't render empty rows.
        fields: list[dict[str, str]] = []
        if msg.service:
            fields.append({"type": "mrkdwn", "text": f"*Service:*\n{msg.service}"})
        if msg.channel:
            fields.append({"type": "mrkdwn", "text": f"*Channel:*\n{msg.channel}"})
        if msg.incident_id:
            fields.append({"type": "mrkdwn", "text": f"*Incident:*\n{msg.incident_id}"})
        if fields:
            blocks.append({"type": "section", "fields": fields[:_MAX_FIELDS_PER_SECTION]})

        if msg.body:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": msg.body[:_SECTION_TEXT_MAX_CHARS],
                    },
                }
            )

        if msg.mentions:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Notify: " + " ".join(msg.mentions),
                        }
                    ],
                }
            )

        return {
            # Top-level `text` is the fallback used by Slack's mobile push
            # notifications, screen-readers, and Slack-bot notifications.
            # Without it, recipients see "<bot> sent a message" with no preview.
            "text": fallback,
            "attachments": [{"color": color, "blocks": blocks}],
        }
