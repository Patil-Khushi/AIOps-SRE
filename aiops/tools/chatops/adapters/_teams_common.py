"""Helpers shared by the Microsoft Teams adapters.

``TeamsWebhookAdapter`` (channel card) and ``TeamsDmAdapter`` (personal
Flow-bot chat) both post to Power Automate webhooks and both target
people by org email, so URL validation, the placeholder-email guard, and
the Adaptive Card mention builder live here — the same split as
``_slack_user_map.py`` serving the two Slack adapters.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..models import ChatMessage, Severity

# Hosts a Teams webhook can live on. Workflows URLs sit on a per-region
# subdomain of logic.azure.com, or — for environments migrated onto Power
# Platform's own infrastructure — on
# <env-id>.<region>.environment.api.powerplatform.com; legacy O365
# connector URLs on webhook.office.com. Suffix-match with a leading dot
# so a hostile "evillogic.azure.com.example.com" doesn't pass.
ALLOWED_HOST_SUFFIXES = (
    ".logic.azure.com",
    ".api.powerplatform.com",
    ".webhook.office.com",
)

# Domains that can never be a real M365 identity: the RFC-2606 reserved
# names our seed data uses (chinmay@example.com etc.). A mention entity or
# DM recipient built from one of these would make the Power Automate flow
# run fail *silently* — the webhook trigger 202s before the post action
# runs — so we skip the person-targeting instead and degrade to the plain
# behavior.
_PLACEHOLDER_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
_PLACEHOLDER_TLDS = (".test", ".invalid", ".localhost", ".example")

# Teams renders the mention only when the <at>…</at> text matches the
# entity's ``text`` byte-for-byte; keep display names short and tag-free
# so the pair can never diverge and the card can't be rejected.
_DISPLAY_NAME_MAX_CHARS = 100

# Cross-sink parity caps — the Slack adapters use the same numbers.
TITLE_MAX_CHARS = 150
BODY_MAX_CHARS = 2900

# Severity → Adaptive Card TextBlock colour. Cards only offer a named
# palette (attention / warning / good / accent / default), so our
# five-level scale collapses: P0/P1 red, P2/P3 amber, INFO muted. The
# title always carries the literal severity tag so no information is lost.
_COLOR_BY_SEVERITY: dict[Severity, str] = {
    Severity.P0: "attention",
    Severity.P1: "attention",
    Severity.P2: "warning",
    Severity.P3: "warning",
    Severity.INFO: "default",
}
_FALLBACK_COLOR = "default"


def is_teams_webhook_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(ALLOWED_HOST_SUFFIXES)


def is_placeholder_email(email: str) -> bool:
    """True when ``email`` cannot be a real org identity (no ``@``, or an
    RFC-2606 reserved domain). Note RA-005's ``_assignee_from`` falls back
    to the bare roster key (``"chinmay"``) when the on-call lookup returned
    no row — the missing-``@`` case is that fallback, not a typo guard."""
    if "@" not in email:
        return True
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return True
    return domain in _PLACEHOLDER_DOMAINS or domain.endswith(_PLACEHOLDER_TLDS)


def build_adaptive_card(
    msg: ChatMessage,
    *,
    mention: tuple[str, dict] | None = None,
    lead_text: str | None = None,
    show_notify_line: bool = True,
) -> dict[str, Any]:
    """Build the Adaptive Card ``content`` shared by both Teams sinks.

    The channel card and the personal DM render identically — same
    severity-coloured headline, same routing FactSet, same body, same
    runbook button — so an engineer reads one layout wherever the incident
    reaches them. Two deliberate differences, both driven by arguments:

    * ``mention`` / ``show_notify_line`` — only the channel card needs an
      @-ping and a "Notify:" line. A DM is already addressed to exactly one
      person, so naming them inside it is noise.
    * ``lead_text`` — the DM opens with urgency framing ("You're paged…")
      that would be redundant in a channel post.
    """
    color = _COLOR_BY_SEVERITY.get(msg.severity, _FALLBACK_COLOR)
    title = f"[{msg.severity.value.upper()}] {msg.title}"

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": title[:TITLE_MAX_CHARS],
            "weight": "bolder",
            "size": "medium",
            "color": color,
            "wrap": True,
        }
    ]

    if lead_text:
        body.append(
            {
                "type": "TextBlock",
                "text": lead_text,
                "isSubtle": True,
                "wrap": True,
                "spacing": "none",
            }
        )

    # Routing-context facts — same field set and ordering as the Slack
    # adapter so every sink tells the same story. Each is added only when
    # populated so the FactSet never renders empty rows.
    facts: list[dict[str, str]] = []
    if msg.service:
        facts.append({"title": "Application", "value": msg.service})
    if msg.category_display:
        facts.append({"title": "Sub-domain", "value": msg.category_display})
    if msg.channel:
        facts.append({"title": "Channel", "value": msg.channel})
    if msg.incident_id:
        facts.append({"title": "Incident", "value": msg.incident_id})
    if facts:
        body.append({"type": "FactSet", "facts": facts})

    if msg.body:
        body.append({"type": "TextBlock", "text": msg.body[:BODY_MAX_CHARS], "wrap": True})

    if show_notify_line:
        if mention is not None:
            at_text, _ = mention
            others = [m for m in msg.mentions if m != msg.assignee]
            notify_text = "Notify: " + " ".join([at_text, *others])
        elif msg.mentions:
            notify_text = "Notify: " + " ".join(msg.mentions)
        else:
            notify_text = None
        if notify_text is not None:
            body.append(
                {
                    "type": "TextBlock",
                    "text": notify_text,
                    "isSubtle": True,
                    "size": "small",
                    "wrap": True,
                }
            )

    # HITL prompt — informational only: a webhook has no click-callback
    # path, so the card names the approval and where to decide it rather
    # than rendering buttons.
    if msg.interactive is not None:
        body.append(
            {
                "type": "TextBlock",
                # No backtick code spans: Teams' TextBlock markdown subset
                # (bold/italic/lists/links) renders them as literal chars.
                "text": (
                    f"Approval id **{msg.interactive.approval_id}** — "
                    f"expires {msg.interactive.expires_at.isoformat()}. "
                    "Approve or deny from the HITL console."
                ),
                "isSubtle": True,
                "size": "small",
                "wrap": True,
            }
        )

    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }

    # Only attach entities when a mention exists — some Teams renderers
    # reject cards carrying an empty entities list.
    if mention is not None:
        card["msteams"]["entities"] = [mention[1]]

    # Runbook as a one-click button. Action.OpenUrl is a plain hyperlink,
    # not a submit — it needs no callback, so it works from a one-way
    # webhook where Action.Submit could not.
    rb = msg.runbook
    if rb is not None and rb.url:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": f"Open runbook ({rb.filename})", "url": rb.url}
        ]

    return card


def build_mention(name: str | None, email: str) -> tuple[str, dict]:
    """Build a Teams Adaptive Card mention as ``(at_text, entity)``.

    The display name is sanitized and capped *before* both strings are
    built, so ``entity["text"]`` always exactly equals the ``<at>…</at>``
    substring the caller embeds in a TextBlock — the invariant Teams
    requires to render a real ping instead of literal text.

    ``email`` is the person's UPN; in a single-org tenant the work email
    *is* the UPN, which is why no AAD object-id lookup is needed.
    """
    display = (name or email).strip()
    display = display.replace("<", "").replace(">", "")
    display = "".join(ch for ch in display if ch.isprintable())
    display = display[:_DISPLAY_NAME_MAX_CHARS].strip() or email[:_DISPLAY_NAME_MAX_CHARS]
    at_text = f"<at>{display}</at>"
    entity = {
        "type": "mention",
        "text": at_text,
        "mentioned": {"id": email, "name": display},
    }
    return at_text, entity
