"""Microsoft Teams personal-DM adapter — "chat with Flow bot".

Unlike :class:`TeamsWebhookAdapter` (which posts an Adaptive Card to one
bound channel), this adapter POSTs a flat JSON body to a *second* Power
Automate flow that opens (or reuses) a private Flow-bot chat with the
on-call engineer, addressed by their org email. Result: the person gets
a personal Teams message that's harder to miss than a channel mention —
the same split as :class:`SlackWebhookAdapter` / :class:`SlackBotAdapter`.

Filter contract
---------------
Mirrors the Slack bot adapter:

* ``page`` (``page_oncall`` in actions, or ``response_mode == "page"``)
  and ``notify`` (``response_mode == "notify"``) send a DM.
* anything else (``log`` / suppressed) is silently skipped — anti-fatigue.
* No DM without a real-looking org email: RA-005's ``assignee_email``
  can be a bare roster key (``"chinmay"``) or a seeded placeholder
  (``@example.com``); both are skipped without an HTTP call. The POC
  targets the **assignee only** — ``msg.mentions`` are Slack handles with
  no email mapping (post-POC follow-up).

Setup (one-time, ~10 min)
-------------------------
1. https://make.powerautomate.com → Create → **Automated cloud flow** →
   trigger **"When a Teams webhook request is received"** (Microsoft
   Teams connector — standard, not premium). Set *Who can trigger the
   flow* = **Anyone** — possession of the signed URL is the credential,
   the same posture as the channel webhook.
2. Add action **"Post message in a chat or channel"** (Microsoft Teams):
   *Post as* = **Flow bot**, *Post in* = **Chat with Flow bot**,
   *Recipient* = expression ``triggerBody()?['recipient_email']``,
   *Message* = expression ``triggerBody()?['html']``.
3. Save, copy the generated HTTP URL into ``.env`` as
   ``AIOPS_TEAMS_DM_WEBHOOK_URL=...``.
4. Smoke-test once (CI can't): POST a sample body with your own email
   and confirm the Flow bot chat arrives. Delivery failures (unknown
   recipient, throttling) happen *after* the trigger has returned 202,
   so this adapter can never see them — check the flow's **run history**
   in Power Automate when a DM goes missing.

The trigger hands the raw posted JSON to the flow as ``triggerBody()``.
If a tenant's trigger proves otherwise during the smoke test, the
fallback is the generic "When an HTTP request is received" trigger
(premium in some license tiers, hence not the default recommendation).

Failure handling
----------------
Per-message failures log + raise. ``ChatOpsClient.send`` catches
per-adapter exceptions so a throttled flow can never block the JSONL
audit log, the WebSocket dashboard, or other adapters.

Secret hygiene
--------------
The webhook URL embeds a signature (``sig=...``) — treat it like a
credential. ``__repr__`` is redacted, and HTTP status failures re-raise
*sanitized* because ``str(httpx.HTTPStatusError)`` embeds the full
request URL (see :mod:`.teams` for the full rationale).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..models import PAGE_ACTIONS, ChatMessage
from ._teams_common import (
    TITLE_MAX_CHARS,
    build_adaptive_card,
    is_placeholder_email,
    is_teams_webhook_url,
)

logger = logging.getLogger(__name__)

_TIMEOUT = float(os.environ.get("AIOPS_TEAMS_DM_TIMEOUT", "5"))

# Cross-sink parity caps (the channel adapter uses the same numbers).
_TITLE_MAX_CHARS = TITLE_MAX_CHARS
_TEXT_MAX_CHARS = 2900

# Urgency framing, mirroring SlackBotAdapter._build_payload: a page
# demands immediate attention; a notify is an FYI reviewed when free.
_PAGE_FRAMING = (
    "You're paged. This is a personal copy of the on-call alert — the "
    "same message is in the team channel. Please acknowledge now."
)
_NOTIFY_FRAMING = (
    "Assigned to you — review when free. This is a heads-up, not a page; "
    "no immediate action required. The same message is in the team channel."
)


class TeamsDmAdapter:
    """POST a flat, flow-friendly JSON body to a Power Automate DM flow.

    The flow (not this adapter) resolves ``recipient_email`` to a person
    and posts the ``html`` field into a private Flow-bot chat. Keeping the
    payload flat means the flow needs exactly two dynamic-content
    expressions and zero parsing steps.
    """

    name = "teams_dm"

    def __init__(self, webhook_url: str) -> None:
        if not webhook_url or not is_teams_webhook_url(webhook_url):
            raise ValueError(
                "TeamsDmAdapter requires a Teams webhook URL "
                "(https, host under *.logic.azure.com, *.api.powerplatform.com, "
                f"or *.webhook.office.com). Got: {webhook_url[:30]!r}..."
            )
        self._webhook_url = webhook_url

    def send(self, msg: ChatMessage) -> None:
        is_page = bool(set(msg.actions) & PAGE_ACTIONS) or msg.response_mode == "page"
        is_notify = msg.response_mode == "notify"
        if not (is_page or is_notify):
            return  # log / suppressed → no DM, anti-fatigue

        email = (msg.assignee_email or "").strip()
        if not email or is_placeholder_email(email):
            logger.debug(
                "teams_dm: assignee email missing or placeholder for %r; skipping DM",
                msg.title,
            )
            return

        payload = self._build_payload(msg, email, paged=is_page)
        try:
            r = httpx.post(self._webhook_url, json=payload, timeout=_TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # str() of an HTTPStatusError embeds the full request URL — whose
            # query string carries the sig=... credential — so neither the
            # exception nor its message may escape as-is (see .teams).
            status = exc.response.status_code
            logger.error("teams_dm adapter: post failed for %r: HTTP %s", msg.title, status)
            raise httpx.HTTPError(f"Teams DM webhook POST failed: HTTP {status}") from None
        except httpx.HTTPError as exc:
            # Transport errors don't embed the URL; safe to propagate.
            logger.error("teams_dm adapter: post failed for %r: %s", msg.title, exc)
            raise

    def __repr__(self) -> str:
        return "TeamsDmAdapter(webhook=https://***.logic.azure.com/***)"

    # ─── payload ─────────────────────────────────────────────────────────

    def _build_payload(self, msg: ChatMessage, email: str, *, paged: bool) -> dict[str, Any]:
        """Flat body for the flow, carrying the Adaptive Card to post.

        The card comes from the same builder as the channel post, so the DM
        is visually identical — severity-coloured headline, routing
        FactSet, runbook button. Two differences, both intentional: the DM
        opens with urgency framing, and it carries no @-mention or "Notify:"
        line, because a DM is already addressed to exactly one person.

        Every scalar is a string, never None: the Power Automate trigger
        validates the body against its declared schema and rejects the whole
        request with HTTP 400 ("TriggerInputSchemaMismatch: Expected String
        but got Null") when an optional field arrives null — which happens
        routinely, e.g. incident_id before a ticket is cut.
        """
        framing = _PAGE_FRAMING if paged else _NOTIFY_FRAMING
        title = f"[{msg.severity.value.upper()}] {msg.title}"[:_TITLE_MAX_CHARS]
        card = build_adaptive_card(msg, mention=None, lead_text=framing, show_notify_line=False)
        rb = msg.runbook
        return {
            "recipient_email": email,
            "urgency": "page" if paged else "notify",
            "severity": msg.severity.value,
            "title": title,
            # Plain-text fallback for the mobile push preview, which shows
            # the notification text rather than rendering the card.
            "text": f"{framing}\n\n{msg.body}".strip()[:_TEXT_MAX_CHARS],
            "card": card,
            "incident_id": msg.incident_id or "",
            "service": msg.service or "",
            "channel": msg.channel or "",
            "runbook_filename": (rb.filename if rb else ""),
            "runbook_title": (rb.title if rb else ""),
            "runbook_url": (rb.url if rb and rb.url else ""),
        }


__all__ = ["TeamsDmAdapter"]
