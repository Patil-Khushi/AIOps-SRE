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

Mention rewriting (CHAT-6, issue #86):
    RA-005 emits vendor-neutral mentions like ``"@chinmay"`` or
    ``"@oncall@payments.example.com"``. Slack renders those as literal
    text — the real user does NOT get a native Slack notification.
    Only the ``<@U12345>`` form pings someone for real.

    This adapter loads a static JSON mapping from ``slack_users.json``
    (next to this file) at construction and rewrites mapped mentions to
    ``<@U_ID>`` form before posting. Unmapped names fall back to plain
    text — the message still lands, the person just doesn't get pinged.

    The rewrite happens only in the Slack-bound payload; the canonical
    ``msg.mentions`` is untouched so other adapters (JSON file, WebSocket,
    PagerDuty) still see the vendor-neutral form. Option B in the issue
    (live ``users.lookupByEmail`` lookup) would need a bot token and is
    deferred until [HITL-1] needs the bot install path anyway.

Failure handling:
    Per-message failures log + raise. ``ChatOpsClient.send`` catches
    per-adapter exceptions so one broken sink (Slack rate-limited, DNS
    flap, expired webhook) can never block the JSONL audit log or the
    WebSocket dashboard from receiving the same message.

    The user-map loader is intentionally permissive: a missing or
    malformed ``slack_users.json`` degrades to "everyone unmapped" rather
    than refusing to start. The cost of dropping a Slack notification is
    higher than the cost of one un-mention-pinged message.

Secret hygiene:
    The webhook URL is never logged or returned in responses. ``__repr__``
    is overridden to redact the URL host path so accidental ``logger.info(%r)``
    of the adapter object can't leak it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
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

# Default location of the static name→Slack-user-id map. Lives next to
# this adapter so the JSON is colocated with the only code that reads it.
_DEFAULT_USER_MAP_PATH = Path(__file__).parent / "slack_users.json"

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

    def __init__(
        self,
        webhook_url: str,
        *,
        user_map_path: Path | None = None,
    ) -> None:
        if not webhook_url or not webhook_url.startswith(_WEBHOOK_PREFIX):
            raise ValueError(
                "SlackWebhookAdapter requires a Slack incoming webhook URL "
                f"(must start with {_WEBHOOK_PREFIX!r}). "
                f"Got: {webhook_url[:30]!r}..."
            )
        self._webhook_url = webhook_url
        # Load the static @name → U_ID map at construction so a malformed
        # file fails fast at startup, not on the first Sev-1 alert. The
        # loader is permissive (missing/bad file → empty map) so the demo
        # still runs without grooming the map first — unmapped names just
        # fall back to plain text per the issue's done-when criteria.
        self._user_map: dict[str, str] = self._load_user_map(
            user_map_path or _DEFAULT_USER_MAP_PATH
        )

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

    # ─── mention rewriting (CHAT-6) ──────────────────────────────────────

    @staticmethod
    def _load_user_map(path: Path) -> dict[str, str]:
        """Read the static name→Slack-user-id map.

        Returns an empty dict if the file is missing or malformed — Slack
        notifications will still land, mentions just won't ping anyone.
        That trade-off is intentional: demo continuity beats hard failure
        on a config file most of the team doesn't touch.
        """
        if not path.exists():
            logger.info(
                "slack adapter: user map %s not found; mentions will render as plain text",
                path,
            )
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "slack adapter: user map %s unreadable (%s); falling back to plain text mentions",
                path,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "slack adapter: user map %s must be a JSON object (got %s); using empty map",
                path,
                type(data).__name__,
            )
            return {}
        # Filter out the documentation key + any non-string entries so
        # downstream lookups stay total.
        return {
            k: v
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")
        }

    def _format_mention(self, raw: str) -> str:
        """Rewrite ``"@chinmay"`` → ``"<@U01ABC123>"`` if mapped.

        Slack's mention syntax is ``<@U_ID>``; anything else is rendered
        as literal text and does not trigger a notification. We strip the
        leading ``@`` before lookup so the JSON keys can be written
        without it (more natural for hand-editing).

        Unmapped names fall back to the raw string — the message still
        sends, the recipient just doesn't get a native Slack ping. This
        is the explicit done-when behavior from issue #86.
        """
        key = raw.lstrip("@")
        slack_id = self._user_map.get(key)
        if slack_id:
            return f"<@{slack_id}>"
        return raw

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
            rendered = [self._format_mention(m) for m in msg.mentions]
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Notify: " + " ".join(rendered),
                        }
                    ],
                }
            )

        # HITL-1: interactive approval prompt — renders two buttons whose
        # ``value`` field encodes "<approval_id>|<verdict>".  Slack POSTs
        # the user's click to the workspace's Interactivity URL
        # (configured in api.slack.com → Interactivity & Shortcuts), which
        # in this POC is ``/api/approvals/slack/callback`` on the demo
        # server (signature-verified, see demo/ui/server.py).
        if msg.interactive is not None:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"Approval id `{msg.interactive.approval_id}` — "
                                f"expires {msg.interactive.expires_at.isoformat()}"
                            ),
                        }
                    ],
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"hitl_approval::{msg.interactive.approval_id}",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "action_id": "hitl_approve",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "value": f"{msg.interactive.approval_id}|approve",
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "action_id": "hitl_deny",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "value": f"{msg.interactive.approval_id}|deny",
                            "confirm": {
                                "title": {"type": "plain_text", "text": "Deny this action?"},
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"This will block `{msg.interactive.action}`.",
                                },
                                "confirm": {"type": "plain_text", "text": "Deny"},
                                "deny": {"type": "plain_text", "text": "Cancel"},
                            },
                        },
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
