"""Notification Router agent (RA-005).

Reads a ``TriageVerdict`` from RA-001 (Alert Triage) and decides where the
notification should land — page on-call, chat the team, post to a daytime
channel, or quietly log to a noise bucket. The decision is encoded as a
``RoutingDecision`` and (for ``route``) emitted through the chatops seam.

Rules in v1 are deliberately simple and deterministic — pure functions of
(severity, time-of-day, ownership). LLM-driven body generation and
escalation ladders (on-call load, business-impact tiers) land in v2 once
the eval harness has scored the rules-only baseline.

Public surface::

    from agents.notification_router import decide, route, RoutingDecision
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from agents.alert_triage import TriageVerdict
from aiops.tools import get_registry
from aiops.tools.chatops import ChatMessage, Severity, get_client

from .models import RoutingDecision, RoutingOutcome

logger = logging.getLogger(__name__)

# UTC business-hours window. The OTel demo + evals run in UTC so we pin to
# UTC here rather than guessing local time. A future v2 will read the
# on-call's timezone from the roster.
BUSINESS_HOUR_START = 9
BUSINESS_HOUR_END = 18

TriageSev = Literal["Sev-1", "Sev-2", "Sev-3", "Sev-4"]


def _team_slug(team: str) -> str:
    """``"Payments Team"`` → ``"payments"``. Mirrors the CMDB mock helper."""
    s = team.lower().strip()
    if s.endswith(" team"):
        s = s[: -len(" team")]
    return s.replace(" ", "-").replace("&", "and") or "platform"


def _is_business_hours(now: datetime) -> bool:
    return BUSINESS_HOUR_START <= now.hour < BUSINESS_HOUR_END


def _channel_for(team_slug: str) -> str:
    return f"team-{team_slug}"


# Tokenize on alphanumerics; drop very short fragments ("a", "to", "5xx" -> "5xx"
# kept because length ≥3). Categories' keywords_csv stores lower-case canonical
# terms ("payment", "gateway", "cart", "kubernetes") so case-folding on this
# side keeps the join symmetric.
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _category_keywords_for(verdict: TriageVerdict) -> list[str]:
    """Pull candidate sub-domain keywords out of the verdict.

    Combines service name, alert summary, and recommended runbook into a
    stable de-duplicated lower-case token list. The on-call DB matches
    these against each failure-category's ``keywords_csv`` and picks the
    specialist on shift for that sub-domain (e.g. ``payment-gateway`` vs
    ``payment-database`` within the Payments Team).

    No NLP — pure regex. The match table is operator-curated in
    ``scripts/seed_oncall.py:CATEGORIES``, so any false negative is a
    one-line keyword addition there, not a model retrain.
    """
    parts: list[str] = []
    if verdict.affected_service:
        parts.append(verdict.affected_service)
    if verdict.alert_summary:
        parts.append(verdict.alert_summary)
    if verdict.recommended_runbook:
        parts.append(verdict.recommended_runbook)
    text = " ".join(parts).lower()
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _resolve_oncall(verdict: TriageVerdict) -> dict | None:
    """One round-trip to the on-call lookup; result feeds mentions + body.

    Returns the raw data dict from ``oncall.schedule.lookup`` (the active
    provider's shape), or ``None`` if the lookup failed / wasn't
    registered. Callers must treat ``None`` as "no enrichment available"
    and fall back to fields already on the verdict.

    Providers that don't accept ``category_keywords`` are handled by the
    tool registry itself: ``ToolRegistry.call`` filters kwargs against
    ``inspect.signature(fn)`` before dispatch, so the mock provider
    (which takes only ``team``) silently ignores the extra argument
    instead of raising. We therefore don't need a TypeError fallback
    here — a real TypeError from inside the provider gets converted to
    a non-ok ToolResult by the registry's own catch.
    """
    keywords = _category_keywords_for(verdict)
    try:
        result = get_registry().call(
            "oncall.schedule.lookup",
            team=verdict.assigned_team,
            category_keywords=keywords,
            # Sticky assignment: RA-001 saved its verdict before we run, so
            # this resolves to the SAME engineer the verdict names instead
            # of letting expertise scoring pick someone else mid-chain.
            service=verdict.affected_service,
        )
    except KeyError:
        return None
    if result.ok and isinstance(result.data, dict):
        return result.data
    return None


def _mentions_from(verdict: TriageVerdict, oncall: dict | None) -> list[str]:
    """Build the @-mentions list from a pre-fetched lookup result.

    Prefers the Slack handle from the on-call DB (``@chinmay``) because
    the Slack adapter rewrites those to ``<@U…>`` for a real ping. Falls
    back to the engineer's email when no Slack handle is recorded — still
    readable, just doesn't ping. Returns ``[]`` when no engineer is
    assigned at all.
    """
    if not verdict.assigned_engineer:
        return []
    if oncall is not None:
        slack_handle = (oncall.get("slack_handle") or "").strip()
        if slack_handle:
            return [slack_handle if slack_handle.startswith("@") else f"@{slack_handle}"]
    return [f"@{verdict.assigned_engineer}"]


def _response_mode(sev: TriageSev, in_hours: bool) -> str:
    """Map severity (+ business hours) to a human-response mode.

    * ``page``   — wake the on-call now: Sev-1 always; Sev-2 after hours.
    * ``notify`` — assign + personal heads-up, review when free: Sev-2 in
      hours, Sev-3.
    * ``log``    — record only, page nobody: Sev-4 noise.
    """
    if sev == "Sev-1":
        return "page"
    if sev == "Sev-2":
        return "notify" if in_hours else "page"
    if sev == "Sev-3":
        return "notify"
    return "log"


# One-line label rendered into the chat body so the reader sees the
# intended human response without decoding the severity themselves. Plain
# ASCII on purpose — this string lands in the JSONL audit log and SQLite on
# a Windows (cp1252) host, where a stray emoji can break a non-UTF-8 writer.
# The dashboard adds its own colored PAGE/NOTIFY/LOG badge.
_RESPONSE_LABEL: dict[str, str] = {
    "page": "PAGE - on-call paged now",
    "notify": "NOTIFY - assigned, review when free",
    "log": "LOG - recorded, no page",
}


def _assignee_from(
    verdict: TriageVerdict, oncall: dict | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve (slack_handle, name, email) of the owning engineer.

    Kept separate from :func:`_mentions_from` because ``mentions`` drives
    the *channel* @-ping (suppressed on low severity for anti-fatigue),
    whereas the assignee always carries the owner so the bot can DM them a
    personal copy regardless of channel policy. Prefers the on-call DB's
    Slack handle; falls back to the engineer email RA-001 stored.
    """
    name = (oncall or {}).get("engineer_name")
    email = (oncall or {}).get("engineer_email") or verdict.assigned_engineer
    handle: str | None = None
    if oncall:
        raw = (oncall.get("slack_handle") or "").strip()
        if raw:
            handle = raw if raw.startswith("@") else f"@{raw}"
    if not handle and email:
        handle = f"@{email}"
    return handle, name, email


def _render_body(
    verdict: TriageVerdict,
    reason: str,
    oncall: dict | None = None,
    response_mode: str = "notify",
) -> str:
    """Compose the human-readable body block for the chat message.

    The body is intentionally structured (one ``key: value`` per line) so
    every renderer — Slack Block Kit, the React dashboard, the JSON audit
    log, terminal `tail` — produces the same legible layout. Order
    matters: the operator's eye should land on *what failed* and *where*
    before the routing reason, which is metadata they care about second.
    """
    matched_display = (oncall or {}).get("matched_category_display") if oncall else None
    via_wildcard = bool((oncall or {}).get("via_wildcard")) if oncall else False
    _handle, specialist_name, email = _assignee_from(verdict, oncall)

    lines: list[str] = [
        f"What failed: {verdict.alert_summary}",
        f"Application: {verdict.affected_service}",
    ]
    if matched_display:
        lines.append(f"Sub-domain: {matched_display}")
    lines.append(f"Severity: {verdict.severity}")
    lines.append(f"Response: {_RESPONSE_LABEL.get(response_mode, response_mode)}")
    lines.append(f"Owning team: {verdict.assigned_team}")
    # Assignee line: show name + email together so the reader knows exactly
    # who owns it (the prior format showed only one or the other). Falls
    # back gracefully when the on-call DB has no name (mock provider, or a
    # roster row missing a display name).
    if specialist_name or email:
        if specialist_name and email:
            who = f"{specialist_name} <{email}>"
        else:
            who = specialist_name or email or "unassigned"
        # The "for X" framing describes *what* they're being paged for,
        # not what they're best at. The wildcard suffix tells the engineer
        # they're the *platform* safety net (no team owner seeded), not the
        # team's dedicated on-call — context for how to respond.
        for_clause = f" — paged for {matched_display}" if matched_display else ""
        wildcard_clause = (
            f" (platform escalation — no on-call seeded for {verdict.assigned_team})"
            if via_wildcard
            else ""
        )
        lines.append(f"On-call: {who}{for_clause}{wildcard_clause}")
    if verdict.recommended_runbook:
        lines.append(f"Runbook: {verdict.recommended_runbook}")
    if verdict.duplicate_alert_count > 1:
        lines.append(f"Duplicate alerts grouped: {verdict.duplicate_alert_count}")
    lines.append(f"Routing reason: {reason}")
    return "\n".join(lines)


def decide(verdict: TriageVerdict, *, now: datetime | None = None) -> RoutingDecision:
    """Pure routing decision — no side effects. Safe to call in tests.

    Returns a ``RoutingDecision`` describing where this notification would
    be sent. Use ``route`` to actually emit it through the chatops seam.
    """
    now = now or datetime.now(UTC)
    in_hours = _is_business_hours(now)
    sev: TriageSev = verdict.severity
    team_slug = _team_slug(verdict.assigned_team)
    title = verdict.alert_summary or f"{sev} alert on {verdict.affected_service}"

    audit: list[str] = [
        f"input: severity={sev}, service={verdict.affected_service!r}, team={verdict.assigned_team!r}",
        f"hour={now.hour:02d}Z, business_hours={'yes' if in_hours else 'no'}",
    ]

    # Resolve on-call ONCE so the same lookup result feeds mentions, body,
    # and the structured ``category_display`` field on the ChatMessage.
    oncall = _resolve_oncall(verdict)
    if oncall and oncall.get("matched_category"):
        audit.append(
            f"expertise: matched category={oncall['matched_category']!r} → "
            f"engineer={oncall.get('engineer_name')!r}"
        )

    # RA-001 marked this verdict Suppressed (duplicate of an existing cluster).
    # Routing must be a no-op: empty actions → route() skips the chatops emit.
    if verdict.status == "Suppressed":
        reason = "verdict suppressed by RA-001 — duplicate of recent alert cluster"
        audit.append("rule: status=Suppressed → no chatops emit")
        return RoutingDecision(
            chat_severity=Severity.INFO,
            channel="suppressed",
            title=title,
            body=_render_body(verdict, reason, oncall=oncall, response_mode="log"),
            mentions=[],
            actions=[],
            reason=reason,
            audit_trace=audit,
            decided_at=now,
            category_display=(oncall or {}).get("matched_category_display"),
            response_mode="log",
        )

    chat_sev: Severity
    channel: str
    actions: list[str]
    reason: str
    mentions = _mentions_from(verdict, oncall)

    if sev == "Sev-1":
        chat_sev = Severity.P1
        channel = "incidents"
        actions = ["page_oncall", "post_to_chat"]
        reason = "Sev-1 critical — page on-call regardless of hour"
        audit.append("rule: Sev-1 → P1 + page + chat (incidents)")

    elif sev == "Sev-2":
        if in_hours:
            chat_sev = Severity.P2
            channel = _channel_for(team_slug)
            actions = ["post_to_chat"]
            reason = "Sev-2 in business hours — chat the owning team"
            audit.append(f"rule: Sev-2 + business_hours → P2 chat to {channel!r}")
        else:
            chat_sev = Severity.P2
            channel = "incidents"
            actions = ["page_oncall", "post_to_chat"]
            reason = "Sev-2 after hours — page on-call"
            audit.append("rule: Sev-2 + after_hours → P2 + page + chat (incidents)")

    elif sev == "Sev-3":
        chat_sev = Severity.P3
        channel = "ops-daytime"
        actions = ["post_to_chat"]
        mentions = []  # no human ping on Sev-3 — anti-fatigue
        reason = "Sev-3 minor — daytime channel for morning triage"
        audit.append("rule: Sev-3 → P3 chat only (ops-daytime), no mention")

    else:  # Sev-4
        chat_sev = Severity.INFO
        channel = "alerts-noise"
        actions = ["post_to_chat"]
        mentions = []
        reason = "Sev-4 noise — quiet log, no human attention required"
        audit.append("rule: Sev-4 → INFO chat only (alerts-noise)")

    mode = _response_mode(sev, in_hours)
    # The owning engineer always travels on the decision (so the bot can DM
    # them) except in log mode, where nobody is meant to act.
    if mode == "log":
        a_handle, a_name, a_email = None, None, None
    else:
        a_handle, a_name, a_email = _assignee_from(verdict, oncall)
    body = _render_body(verdict, reason, oncall=oncall, response_mode=mode)

    return RoutingDecision(
        chat_severity=chat_sev,
        channel=channel,
        title=title,
        body=body,
        mentions=mentions,
        actions=actions,
        reason=reason,
        audit_trace=audit,
        decided_at=now,
        category_display=(oncall or {}).get("matched_category_display"),
        response_mode=mode,
        assignee=a_handle,
        assignee_name=a_name,
        assignee_email=a_email,
    )


def _decision_to_chat_message(
    verdict: TriageVerdict,
    decision: RoutingDecision,
) -> ChatMessage:
    return ChatMessage(
        channel=decision.channel,
        severity=decision.chat_severity,
        title=decision.title,
        body=decision.body,
        incident_id=verdict.incident_id,
        service=verdict.affected_service,
        category_display=decision.category_display,
        mentions=list(decision.mentions),
        # CHAT-5 prep: the PagerDuty adapter filters on this list (only
        # acts when "page_oncall" is present). Other adapters can also
        # inspect it to react differently for paging vs. chat-only sends.
        actions=list(decision.actions),
        response_mode=decision.response_mode,
        assignee=decision.assignee,
        assignee_name=decision.assignee_name,
        assignee_email=decision.assignee_email,
        timestamp=decision.decided_at,
    )


def route(verdict: TriageVerdict, *, now: datetime | None = None) -> RoutingOutcome:
    """Decide and emit. Returns the decision plus per-adapter deliveries.

    Side effect: drops a ``ChatMessage`` into the chatops seam, which fans
    it out to every registered adapter (WebSocket dashboard, JSON audit
    log, future Slack/Teams/PagerDuty). Empty ``actions`` (e.g. Suppressed
    verdicts) short-circuit the emit so suppressed alerts can't reach
    chatops sinks.
    """
    decision = decide(verdict, now=now)
    if not decision.actions:
        logger.info(
            "RA-005: skipped routing for %s on %s (%s)",
            verdict.severity,
            verdict.affected_service,
            decision.reason,
        )
        return RoutingOutcome(decision=decision, deliveries={})
    msg = _decision_to_chat_message(verdict, decision)
    deliveries = get_client().send(msg)
    logger.info(
        "RA-005: routed %s on %s -> %s (%s)",
        verdict.severity,
        verdict.affected_service,
        decision.channel,
        decision.chat_severity,
    )
    return RoutingOutcome(decision=decision, deliveries=deliveries)


def run(input_payload: dict) -> dict:
    """Entry point for the eval harness.

    Accepts a dict shaped like ``{"verdict": {...}, "now": "ISO8601"}`` and
    returns the ``RoutingDecision`` as a JSON-friendly dict. Pure — does
    not emit through the chatops seam (so evals don't pollute the audit
    log or trigger live notifications).
    """
    verdict = TriageVerdict.model_validate(input_payload["verdict"])
    now_raw = input_payload.get("now")
    now: datetime | None = None
    if isinstance(now_raw, str):
        now = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
    return decide(verdict, now=now).model_dump(mode="json")
