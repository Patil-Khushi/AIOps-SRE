"""Slack Bot adapter — sends Direct Messages to specific users (ON-CALL-2).

Unlike :class:`SlackWebhookAdapter` (which posts to one bound channel via
an Incoming Webhook), this adapter uses a **Bot User OAuth Token** to call
``chat.postMessage`` with a user's Slack ID as the channel argument. Slack
auto-opens (or reuses) a DM between the bot and that user. Result: the
real on-call engineer gets a personal DM that's harder to miss than a
channel mention buried in #aiops-test.

Filter contract
---------------
Only sends a DM when ``msg.actions`` contains ``"page_oncall"`` AND
``msg.mentions`` has at least one handle that maps to a known Slack
user ID. Chat-only sends (Sev-3 daytime triage, Sev-4 noise) skip DM
delivery — anti-fatigue.

Setup
-----
1. https://api.slack.com/apps → your app → **OAuth & Permissions**.
2. Under "Bot Token Scopes" add ``chat:write`` and ``im:write``.
3. Click **Install / Reinstall to Workspace** → Allow.
4. Copy the **Bot User OAuth Token** (``xoxb-...``) into ``.env`` as
   ``AIOPS_SLACK_BOT_TOKEN=xoxb-...``.

The user-id map (``slack_users.json``) is shared with the webhook adapter
— same source of truth, same handles, same IDs.

Failure handling
----------------
Per-message failures log + raise. ``ChatOpsClient.send`` catches
per-adapter exceptions so a Slack rate limit or invalid token can never
block the JSONL audit log, the WebSocket dashboard, or other adapters.

Secret hygiene
--------------
The bot token is never logged or returned in responses. ``__repr__`` is
overridden to redact the token so accidental ``logger.info("%r", adapter)``
cannot leak it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ..models import ChatMessage, Severity
from ._slack_user_map import load_slack_user_map

logger = logging.getLogger(__name__)

_SLACK_API_URL = "https://slack.com/api/chat.postMessage"
_TIMEOUT = float(os.environ.get("AIOPS_SLACK_BOT_TIMEOUT", "5"))
_BOT_TOKEN_PREFIX = "xoxb-"

# Default location of the static name→Slack-user-id map. Lives next to
# the webhook adapter so both adapters read the same source.
_DEFAULT_USER_MAP_PATH = Path(__file__).parent / "slack_users.json"

# Severity → Slack attachment color. Matches the webhook adapter palette.
_COLOR_BY_SEVERITY: dict[Severity, str] = {
    Severity.P0: "danger",
    Severity.P1: "danger",
    Severity.P2: "warning",
    Severity.P3: "#facc15",
    Severity.INFO: "#94a3b8",
}
_FALLBACK_COLOR = "#94a3b8"

# Slack hard limits (Block Kit reference).
_HEADER_MAX_CHARS = 150
_SECTION_TEXT_MAX_CHARS = 2900
_MAX_FIELDS_PER_SECTION = 10

# Routing actions that warrant a personal DM. RA-005 only includes
# "page_oncall" for severities where waking a human is the right move.
PAGE_ACTIONS = frozenset({"page_oncall"})


class SlackBotAdapter:
    """Sends a personal DM to mentioned users via Slack's chat.postMessage API.

    Composes alongside :class:`SlackWebhookAdapter`: the webhook posts to
    the shared team channel, this adapter additionally DMs the on-call
    engineer so they see it personally (distinctive sound, harder to
    miss).
    """

    name = "slack_bot"

    def __init__(
        self,
        bot_token: str,
        *,
        user_map_path: Path | None = None,
    ) -> None:
        if not bot_token or not bot_token.startswith(_BOT_TOKEN_PREFIX):
            raise ValueError(
                "SlackBotAdapter requires a Bot User OAuth Token "
                f"(must start with {_BOT_TOKEN_PREFIX!r}). "
                f"Got: {bot_token[:10]!r}..."
            )
        self._token = bot_token
        self._user_map: dict[str, str] = load_slack_user_map(
            user_map_path or _DEFAULT_USER_MAP_PATH
        )

    def send(self, msg: ChatMessage) -> None:
        # Filter: only DM when a paging action is present.
        if not (set(msg.actions) & PAGE_ACTIONS):
            return
        if not msg.mentions:
            logger.debug("slack_bot: page action set but no mentions; skipping DM")
            return

        for raw_mention in msg.mentions:
            key = raw_mention.lstrip("@")
            user_id = self._user_map.get(key)
            if not user_id:
                logger.info(
                    "slack_bot: no Slack user_id for mention %r; cannot DM",
                    raw_mention,
                )
                continue
            self._post_dm(user_id, msg)

    def _post_dm(self, user_id: str, msg: ChatMessage) -> None:
        payload = self._build_payload(user_id, msg)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            r = httpx.post(_SLACK_API_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            logger.error("slack_bot: HTTP post failed for user %s: %s", user_id, exc)
            raise

        if not data.get("ok"):
            err = data.get("error", "unknown")
            logger.error(
                "slack_bot: Slack API returned not-ok for user %s: %s",
                user_id,
                err,
            )
            raise RuntimeError(f"Slack chat.postMessage rejected: {err}")

    def __repr__(self) -> str:
        return "SlackBotAdapter(token=xoxb-***)"

    # ─── payload ─────────────────────────────────────────────────────────

    def _build_payload(self, user_id: str, msg: ChatMessage) -> dict[str, Any]:
        color = _COLOR_BY_SEVERITY.get(msg.severity, _FALLBACK_COLOR)
        fallback = f"[{msg.severity.value}] {msg.title}"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": msg.title[:_HEADER_MAX_CHARS],
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ":rotating_light: *You're paged.* "
                            "This DM is a personal copy of the on-call "
                            "alert. The same message is in the team "
                            "channel."
                        ),
                    }
                ],
            },
        ]

        fields: list[dict[str, str]] = []
        if msg.service:
            fields.append({"type": "mrkdwn", "text": f"*Application:*\n{msg.service}"})
        if msg.category_display:
            fields.append({"type": "mrkdwn", "text": f"*Sub-domain:*\n{msg.category_display}"})
        if msg.channel:
            fields.append({"type": "mrkdwn", "text": f"*Team channel:*\n{msg.channel}"})
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

        return {
            "channel": user_id,  # ← user ID as channel = DM
            "text": fallback,  # plain-text fallback for mobile push
            "attachments": [{"color": color, "blocks": blocks}],
        }


__all__ = ["SlackBotAdapter"]
