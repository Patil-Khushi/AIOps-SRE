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
from datetime import UTC, datetime
from typing import Literal

from agents.alert_triage import TriageVerdict
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


def _mentions_for(verdict: TriageVerdict) -> list[str]:
    if verdict.assigned_engineer:
        return [f"@{verdict.assigned_engineer}"]
    return []


def _render_body(verdict: TriageVerdict, reason: str) -> str:
    lines = [
        f"Service: {verdict.affected_service}",
        f"Severity: {verdict.severity}",
        f"Owning team: {verdict.assigned_team}",
    ]
    if verdict.assigned_engineer:
        lines.append(f"On-call: {verdict.assigned_engineer}")
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

    # RA-001 marked this verdict Suppressed (duplicate of an existing cluster).
    # Routing must be a no-op: empty actions → route() skips the chatops emit.
    if verdict.status == "Suppressed":
        reason = "verdict suppressed by RA-001 — duplicate of recent alert cluster"
        audit.append("rule: status=Suppressed → no chatops emit")
        return RoutingDecision(
            chat_severity=Severity.INFO,
            channel="suppressed",
            title=title,
            body=_render_body(verdict, reason),
            mentions=[],
            actions=[],
            reason=reason,
            audit_trace=audit,
            decided_at=now,
        )

    chat_sev: Severity
    channel: str
    actions: list[str]
    reason: str
    mentions = _mentions_for(verdict)

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

    body = _render_body(verdict, reason)

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
        mentions=list(decision.mentions),
        # CHAT-5 prep: the PagerDuty adapter filters on this list (only
        # acts when "page_oncall" is present). Other adapters can also
        # inspect it to react differently for paging vs. chat-only sends.
        actions=list(decision.actions),
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
