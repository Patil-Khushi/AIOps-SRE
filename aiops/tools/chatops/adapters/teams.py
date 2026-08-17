"""Microsoft Teams webhook adapter for the chatops seam.

Posts every ``ChatMessage`` as an Adaptive Card to a Teams channel via a
Power Automate Workflows webhook. The vendor coupling is contained in this
one file — agents never build Teams payloads; they emit ``ChatMessage``
and let the seam fan out to whichever adapters are registered.

Setup (one-time, ~5 min, needs a Teams channel you can add workflows to):

1. In Teams: channel → "..." → Workflows → "Post to a channel when a
   webhook request is received" (a Power Automate template) → Add.
2. Copy the generated HTTP URL (``https://...logic.azure.com/...``) into
   ``.env`` as ``AIOPS_TEAMS_WEBHOOK_URL=...``.

Legacy note: the classic Office 365 "Incoming Webhook" connector
(``https://<tenant>.webhook.office.com/webhookb2/...``) was retired by
Microsoft; URLs of that shape are still accepted here for tenants on the
extended retirement timeline, but new setups should use Workflows.

The dev server's ``_register_chatops_adapters`` hook reads the env var and
registers this adapter only when the URL is set, so the demo runs fine
without Teams configured.

Payload contract:
    The Workflows trigger only accepts the Teams message envelope —
    ``{"type": "message", "attachments": [{"contentType":
    "application/vnd.microsoft.card.adaptive", "content": {...card...}}]}``.
    A flat JSON body is rejected (HTTP 400) or silently dropped, so the
    card wrapping here is load-bearing, not cosmetic.

Mentions:
    The **assignee** (the on-call engineer RA-005 resolved) gets a real,
    native Teams @-mention: their org email doubles as their UPN in a
    single-org tenant, so ``msg.assignee_email`` feeds an
    ``msteams.entities`` mention directly — no AAD object-id lookup, no
    Graph API. Best-effort: a placeholder email (``@example.com`` seed
    data) or the bare-roster-key fallback (``"chinmay"``) skips the native
    mention and degrades to plain text. Real org emails must be provided
    via ``AIOPS_ONCALL_ROSTER_JSON`` + ``uv run python -m
    scripts.seed_oncall --force`` before the ping resolves.

    Caveat: an *unknown* org UPN fails the Power Automate flow run
    silently — the webhook trigger returns 202 before the post action
    runs, so the adapter sees success while the card never lands. If
    channel cards vanish after enabling mentions, check the flow's run
    history in Power Automate. The retired ``webhook.office.com``
    connector historically ignored ``msteams.entities`` in places, so
    mentions are best-effort on legacy URLs.

    Other ``msg.mentions`` are Slack handles (``"@chinmay"``) with no
    UPN mapping and stay plain text; a handle→UPN map (analogous to
    ``slack_users.json``) is the post-POC follow-up if multi-person
    channel pings are wanted.

Interactive prompts:
    A webhook is one-way — Teams cannot POST an Approve/Deny click back to
    us the way the Slack interactivity callback does. HITL prompts render
    as an informational line (approval id + expiry); the decision itself
    happens on the /hitl page or in Slack.

Failure handling:
    Per-message failures log + raise. ``ChatOpsClient.send`` catches
    per-adapter exceptions so one broken sink can never block the JSONL
    audit log or the WebSocket dashboard from receiving the same message.

Secret hygiene:
    The webhook URL embeds a signature (``sig=...``) that authorizes
    posting — treat it like a credential. It is never logged: ``__repr__``
    is overridden so an accidental ``logger.info("%r", adapter)`` cannot
    leak it, and ``send()`` re-raises HTTP status failures as a *sanitized*
    exception because ``str(httpx.HTTPStatusError)`` embeds the full
    request URL — letting the original escape would put the signature in
    ``ChatOpsClient``'s error log and in the ``DeliveryResult.error`` field
    that ``/api/triage`` serializes back to the dashboard.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..models import ChatMessage
from ._teams_common import (
    BODY_MAX_CHARS,
    TITLE_MAX_CHARS,
    build_adaptive_card,
    build_mention,
    is_placeholder_email,
    is_teams_webhook_url,
)

logger = logging.getLogger(__name__)

_TIMEOUT = float(os.environ.get("AIOPS_TEAMS_TIMEOUT", "5"))

# Teams truncation limits live in _teams_common alongside the card
# builder that applies them, so both Teams sinks truncate identically.
# Re-exported here because tests and callers reference them by this name.
_TITLE_MAX_CHARS = TITLE_MAX_CHARS
_BODY_MAX_CHARS = BODY_MAX_CHARS


class TeamsWebhookAdapter:
    """POST a ``ChatMessage`` as an Adaptive Card to a Teams workflow webhook.

    The target channel is bound to the workflow at creation time in Teams —
    this adapter does NOT choose a channel per message. The agent-routing
    ``msg.channel`` (e.g. ``"incidents"``, ``"team-payments"``) is surfaced
    as a card fact so the recipient can see what RA-005 *intended*, even
    when every notification lands in the same Teams channel today.
    """

    name = "teams"

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url or not is_teams_webhook_url(webhook_url):
            raise ValueError(
                "TeamsWebhookAdapter requires a Teams webhook URL "
                "(https, host under *.logic.azure.com, *.api.powerplatform.com, "
                f"or *.webhook.office.com). Got: {webhook_url[:30]!r}..."
            )
        self._webhook_url = webhook_url

    def send(self, msg: ChatMessage) -> None:
        payload = self._build_payload(msg)
        try:
            r = httpx.post(self._webhook_url, json=payload, timeout=_TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # str() of an HTTPStatusError embeds the full request URL — whose
            # query string carries the sig=... credential — so neither the
            # exception nor its message may escape as-is. Re-raise a sanitized
            # error carrying only the status code; ``from None`` so
            # ChatOpsClient's logger.exception traceback (and the
            # DeliveryResult.error it serializes into /api/triage responses)
            # can't resurrect the URL-bearing original.
            status = exc.response.status_code
            logger.error("teams adapter: post failed for %r: HTTP %s", msg.title, status)
            raise httpx.HTTPError(f"Teams webhook POST failed: HTTP {status}") from None
        except httpx.HTTPError as exc:
            # Transport errors (DNS, connect, timeout) don't embed the URL in
            # their message, so they are safe to log and propagate unchanged.
            logger.error("teams adapter: post failed for %r: %s", msg.title, exc)
            raise

    def __repr__(self) -> str:
        return "TeamsWebhookAdapter(webhook=https://***.logic.azure.com/***)"

    # ─── payload ─────────────────────────────────────────────────────────

    @staticmethod
    def _mention_for(msg: ChatMessage) -> tuple[str, dict[str, Any]] | None:
        """Native mention for the assignee, or None to fall back to plain
        text. Requires a real-looking org email: RA-005's assignee_email
        can be a bare roster key ("chinmay") or a seeded placeholder
        (@example.com), and an entity built from either would silently
        kill the Power Automate flow run."""
        email = (msg.assignee_email or "").strip()
        if not email or is_placeholder_email(email):
            return None
        return build_mention(msg.assignee_name, email)

    def _build_payload(self, msg: ChatMessage) -> dict[str, Any]:
        """Wrap the shared Adaptive Card in the Teams message envelope.

        Card construction lives in ``_teams_common`` so the channel card and
        the personal DM cannot drift apart; this adapter only supplies the
        channel-specific parts (the @-mention and the Notify line).
        """
        card = build_adaptive_card(msg, mention=self._mention_for(msg), show_notify_line=True)
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": card,
                }
            ],
        }


__all__ = ["TeamsWebhookAdapter"]
