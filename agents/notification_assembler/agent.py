"""Notification Assembler agent (RA-005+006) — Reactive-Active phase.

Merges the former Notification Router (RA-005) and War-Room Assembler (RA-006)
into one agent. For every ``TriageVerdict`` from RA-001 it:

1. **Decides the notification route** — page on-call, chat the team, post to a
   daytime channel, or quietly log to a noise bucket (severity + time-of-day +
   ownership).
2. **Stands up the war room on Sev-1/Sev-2** — a dedicated chatops channel, the
   on-call SME, a live context pack, and a seed timeline for RCA.
3. **Emits exactly one chatops message** — the routing notification, with the
   war-room join link + SMEs folded inline when a room was created. Lower
   severities (Sev-3/Sev-4) get the plain notification and no room.

Public surface::

    from agents.notification_assembler import decide, notify, run
    from agents.notification_assembler import NotificationAssembly, NotificationOutcome
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

from agents.alert_triage import TriageVerdict
from aiops.tools import get_registry
from aiops.tools.chatops import ChatMessage, Severity, get_client

from .models import (
    ContextPackItem,
    InvitedSME,
    NotificationAssembly,
    NotificationOutcome,
    RoutingDecision,
    RoutingOutcome,
    TimelineEvent,
    WarRoomAssembly,
    WarRoomOutcome,
)

logger = logging.getLogger(__name__)

# UTC business-hours window. The OTel demo + evals run in UTC so we pin to UTC
# rather than guessing local time. A future v2 reads the on-call's timezone.
BUSINESS_HOUR_START = 9
BUSINESS_HOUR_END = 18

TriageSev = Literal["Sev-1", "Sev-2", "Sev-3", "Sev-4"]

# Only these severities warrant a war room. Sev-3/Sev-4 are handled by the
# routing notification alone (chat / noise bucket).
_WAR_ROOM_SEVERITIES = {"Sev-1", "Sev-2"}
_WAR_ROOM_CHAT_SEVERITY = {"Sev-1": Severity.P1, "Sev-2": Severity.P2}


# ─── shared helpers ────────────────────────────────────────────────────────


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


# Tokenize on alphanumerics; drop very short fragments. Categories' keywords_csv
# stores lower-case canonical terms so case-folding here keeps the join symmetric.
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _category_keywords_for(verdict: TriageVerdict) -> list[str]:
    """Pull candidate sub-domain keywords out of the verdict.

    Combines service name, alert summary, and recommended runbook into a stable
    de-duplicated lower-case token list. The on-call DB matches these against
    each failure-category's ``keywords_csv`` and picks the specialist on shift
    for that sub-domain. No NLP — pure regex.
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
    """One round-trip to the on-call lookup; result feeds mentions + body + SMEs.

    Returns the raw data dict from ``oncall.schedule.lookup`` or ``None`` if the
    lookup failed / wasn't registered. ``ToolRegistry.call`` filters kwargs
    against the provider signature, so the mock provider (which takes only
    ``team``) silently ignores the extra ``category_keywords`` / ``service``.
    """
    keywords = _category_keywords_for(verdict)
    try:
        result = get_registry().call(
            "oncall.schedule.lookup",
            team=verdict.assigned_team,
            category_keywords=keywords,
            service=verdict.affected_service,
        )
    except KeyError:
        return None
    if result.ok and isinstance(result.data, dict):
        return result.data
    return None


# ─── routing (RA-005) ──────────────────────────────────────────────────────


def _mentions_from(verdict: TriageVerdict, oncall: dict | None) -> list[str]:
    """Build the @-mentions list from a pre-fetched lookup result.

    Prefers the Slack handle from the on-call DB (``@chinmay``); falls back to
    the engineer's email when no handle is recorded. Returns ``[]`` when no
    engineer is assigned at all.
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
    * ``notify`` — assign + personal heads-up: Sev-2 in hours, Sev-3.
    * ``log``    — record only, page nobody: Sev-4 noise.
    """
    if sev == "Sev-1":
        return "page"
    if sev == "Sev-2":
        return "notify" if in_hours else "page"
    if sev == "Sev-3":
        return "notify"
    return "log"


# One-line label rendered into the chat body so the reader sees the intended
# human response. Plain ASCII on purpose — this lands in the JSONL audit log and
# SQLite on a Windows (cp1252) host where a stray emoji can break a non-UTF-8 writer.
_RESPONSE_LABEL: dict[str, str] = {
    "page": "PAGE - on-call paged now",
    "notify": "NOTIFY - assigned, review when free",
    "log": "LOG - recorded, no page",
}


def _assignee_from(
    verdict: TriageVerdict, oncall: dict | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve (slack_handle, name, email) of the owning engineer.

    Kept separate from :func:`_mentions_from` because ``mentions`` drives the
    *channel* @-ping (suppressed on low severity for anti-fatigue), whereas the
    assignee always carries the owner so the bot can DM them.
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

    The body is structured (one ``key: value`` per line) so every renderer —
    Slack Block Kit, the React dashboard, the JSON audit log, terminal ``tail``
    — produces the same legible layout.
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
    if specialist_name or email:
        if specialist_name and email:
            who = f"{specialist_name} <{email}>"
        else:
            who = specialist_name or email or "unassigned"
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


def _decide_routing(verdict: TriageVerdict, now: datetime, oncall: dict | None) -> RoutingDecision:
    """Pure routing decision (former RA-005 ``decide``)."""
    in_hours = _is_business_hours(now)
    sev: TriageSev = verdict.severity
    team_slug = _team_slug(verdict.assigned_team)
    title = verdict.alert_summary or f"{sev} alert on {verdict.affected_service}"

    audit: list[str] = [
        f"input: severity={sev}, service={verdict.affected_service!r}, team={verdict.assigned_team!r}",
        f"hour={now.hour:02d}Z, business_hours={'yes' if in_hours else 'no'}",
    ]
    if oncall and oncall.get("matched_category"):
        audit.append(
            f"expertise: matched category={oncall['matched_category']!r} → "
            f"engineer={oncall.get('engineer_name')!r}"
        )

    # RA-001 marked this verdict Suppressed (duplicate cluster). Routing is a
    # no-op: empty actions → notify() skips the chatops emit.
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


# ─── war room (RA-006) ─────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """``"Payment Service"`` -> ``"payment-service"``. Used for the channel name."""
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "service"


def _channel_name(verdict: TriageVerdict) -> str:
    """Stable war-room channel name. Prefers the incident id (set by RA-003);
    falls back to the affected service so the agent still works pre-ticket."""
    if verdict.incident_id:
        return f"war-room-{_slug(verdict.incident_id)}"
    return f"war-room-{_slug(verdict.affected_service)}"


def _invited_smes(verdict: TriageVerdict, oncall: dict | None) -> list[InvitedSME]:
    """Build the invite list. v1: the on-call engineer for the owning team."""
    if not verdict.assigned_engineer and not oncall:
        return []
    handle = ""
    name = verdict.assigned_engineer
    if oncall is not None:
        handle = (oncall.get("slack_handle") or "").strip()
        name = oncall.get("engineer_name") or name
    if not handle and verdict.assigned_engineer:
        handle = f"@{verdict.assigned_engineer}"
    elif handle and not handle.startswith("@"):
        handle = f"@{handle}"
    if not handle:
        return []
    return [
        InvitedSME(
            handle=handle,
            name=name,
            team=verdict.assigned_team,
            reason=f"on-call for {verdict.assigned_team}",
            source="oncall",
        )
    ]


def _context_item(label: str, capability: str, **kwargs) -> ContextPackItem:
    """Call a read-only observability seam and fold the result into one pack
    line. Any failure becomes ``"unavailable"`` rather than raising — the
    assembly must not depend on live infra being up."""
    result = get_registry().call(capability, **kwargs)
    if not result.ok:
        return ContextPackItem(label=label, value="unavailable", source=capability)
    return ContextPackItem(label=label, value=str(result.data), source=capability)


def _build_context_pack(verdict: TriageVerdict) -> list[ContextPackItem]:
    """Live snapshot for the room. Static facts from the verdict first, then
    best-effort live telemetry for the affected service."""
    svc = verdict.affected_service
    pack: list[ContextPackItem] = [
        ContextPackItem(label="Affected service", value=svc, source="verdict"),
        ContextPackItem(label="Severity", value=verdict.severity, source="verdict"),
    ]
    if verdict.recommended_runbook:
        pack.append(
            ContextPackItem(label="Runbook", value=verdict.recommended_runbook, source="verdict")
        )
    pack.append(
        _context_item(
            "Request rate (5m)",
            "observability.metrics.query",
            promql=f'sum(rate(http_server_request_duration_count{{service_name="{svc}"}}[5m]))',
        )
    )
    pack.append(
        _context_item(
            "Recent traces",
            "observability.traces.search",
            service=svc,
            lookback="15m",
            limit=5,
        )
    )
    return pack


def _seed_timeline(
    verdict: TriageVerdict, now: datetime, invited: list[InvitedSME], channel: str
) -> list[TimelineEvent]:
    """The starting timeline. RA-007 / Incident Commander append to it later."""
    events = [
        TimelineEvent(at=now, event=f"{verdict.severity} detected on {verdict.affected_service}"),
        TimelineEvent(at=now, event=f"War room {channel!r} created"),
    ]
    if invited:
        who = ", ".join(s.handle for s in invited)
        events.append(TimelineEvent(at=now, event=f"SMEs invited: {who}"))
    return events


def _decide_war_room(verdict: TriageVerdict, now: datetime, oncall: dict | None) -> WarRoomAssembly:
    """Pure war-room decision (former RA-006 ``decide``). Returns
    ``assembled=False`` for Sev-3/Sev-4 or Suppressed verdicts."""
    sev = verdict.severity
    channel = _channel_name(verdict)
    title = f"War room: {verdict.alert_summary or sev + ' on ' + verdict.affected_service}"
    audit: list[str] = [
        f"input: severity={sev}, service={verdict.affected_service!r}, "
        f"team={verdict.assigned_team!r}, status={verdict.status}",
    ]

    if verdict.status == "Suppressed" or sev not in _WAR_ROOM_SEVERITIES:
        reason = (
            "verdict suppressed by RA-001 — no war room"
            if verdict.status == "Suppressed"
            else f"{sev} below Sev-2 threshold — no war room warranted"
        )
        audit.append(f"rule: {reason}")
        return WarRoomAssembly(
            assembled=False,
            channel=channel,
            title=title,
            chat_severity=Severity.INFO,
            reason=reason,
            audit_trace=audit,
            assembled_at=now,
        )

    chat_sev = _WAR_ROOM_CHAT_SEVERITY[sev]
    if oncall and oncall.get("engineer_name"):
        audit.append(f"oncall: {oncall['engineer_name']!r} for {verdict.assigned_team!r}")
    else:
        audit.append("oncall: no lookup result — falling back to verdict.assigned_engineer")

    invited = _invited_smes(verdict, oncall)
    audit.append(f"smes: invited {len(invited)} (source=oncall)")

    context_pack = _build_context_pack(verdict)
    live = sum(
        1
        for i in context_pack
        if i.source and i.source.startswith("observability") and i.value != "unavailable"
    )
    audit.append(f"context_pack: {len(context_pack)} items, {live} live telemetry")

    timeline = _seed_timeline(verdict, now, invited, channel)
    reason = f"{sev} incident — war room {channel} assembled with {len(invited)} SME(s)"
    audit.append(f"rule: {sev} -> assemble room {channel!r} at {chat_sev}")

    return WarRoomAssembly(
        assembled=True,
        channel=channel,
        title=title,
        chat_severity=chat_sev,
        invited=invited,
        context_pack=context_pack,
        timeline=timeline,
        reason=reason,
        audit_trace=audit,
        assembled_at=now,
    )


def _create_bridge(assembly: WarRoomAssembly) -> WarRoomAssembly:
    """Stand up the real Slack war room via the ``chatops.war_room.create`` seam
    and fold the result back into the assembly (bridge link + per-SME invite
    status + a timeline event).

    Never raises: a missing capability → ``skipped``; a non-ok ToolResult →
    ``failed``; both leave the rest of the assembly intact so the incident
    pipeline keeps moving even if Slack is down (CLAUDE.md safe-autonomy)."""
    try:
        result = get_registry().call(
            "chatops.war_room.create",
            channel=assembly.channel,
            title=assembly.title,
            body=_render_war_room_opening(assembly),
            invite_handles=[s.handle for s in assembly.invited],
        )
    except KeyError:
        return assembly.model_copy(
            update={
                "bridge_status": "skipped",
                "audit_trace": [
                    *assembly.audit_trace,
                    "bridge: no chatops.war_room.create provider registered",
                ],
            }
        )

    if not result.ok or not isinstance(result.data, dict):
        return assembly.model_copy(
            update={
                "bridge_status": "failed",
                "audit_trace": [*assembly.audit_trace, f"bridge: failed — {result.error}"],
            }
        )

    data = result.data
    simulated = bool(data.get("simulated"))
    by_handle = {row["handle"]: row for row in data.get("invited", [])}
    merged_invited = [
        sme.model_copy(
            update={
                "slack_user_id": by_handle.get(sme.handle, {}).get("slack_user_id"),
                "invite_status": by_handle.get(sme.handle, {}).get("invite_status"),
            }
        )
        for sme in assembly.invited
    ]
    url = data.get("url")
    meeting_url = data.get("meeting_url")
    status = "simulated" if simulated else "created"
    event = (
        f"Slack war room {'simulated' if simulated else 'created'}: {url}"
        if url
        else "Slack war room created"
    )
    return assembly.model_copy(
        update={
            "invited": merged_invited,
            "bridge_status": status,
            "bridge_provider": data.get("provider"),
            "bridge_channel_id": data.get("channel_id"),
            "bridge_url": url,
            "meeting_url": meeting_url,
            "timeline": [*assembly.timeline, TimelineEvent(at=assembly.assembled_at, event=event)],
            "audit_trace": [
                *assembly.audit_trace,
                f"bridge: {status} via {data.get('provider')} → {url}"
                + (f" (note: {data['note']})" if data.get("note") else ""),
            ],
        }
    )


def _render_war_room_opening(assembly: WarRoomAssembly) -> str:
    """The opening post for the war-room channel itself: who's in, the context
    pack, and the seed timeline. Posted *into* the new channel by the bridge —
    not the dashboard notification (that's the single combined message)."""
    lines: list[str] = [assembly.reason, ""]
    if assembly.meeting_url:
        lines.append(f"Join the call: {assembly.meeting_url}")
    if assembly.bridge_url:
        lines.append(f"War-room channel: {assembly.bridge_url}")
    if assembly.invited:
        lines.append("SMEs: " + ", ".join(s.handle for s in assembly.invited))
    lines.append("")
    lines.append("Context pack:")
    lines.extend(f"  {i.label}: {i.value}" for i in assembly.context_pack)
    lines.append("")
    lines.append("Timeline:")
    lines.extend(f"  {e.at.isoformat()} — {e.event}" for e in assembly.timeline)
    return "\n".join(lines)


# ─── combined message + public API ─────────────────────────────────────────


def _war_room_section(assembly: WarRoomAssembly) -> str:
    """The war-room block folded into the single notification body — the link
    the operator clicks straight from the one message they receive."""
    lines = ["", "--- War room ---", f"Channel: #{assembly.channel}"]
    if assembly.meeting_url:
        lines.append(f"Join the call: {assembly.meeting_url}")
    if assembly.bridge_url:
        lines.append(f"War-room channel: {assembly.bridge_url}")
    if assembly.invited:
        lines.append("SMEs invited: " + ", ".join(s.handle for s in assembly.invited))
    if assembly.bridge_status not in ("created", "simulated"):
        lines.append(f"(bridge status: {assembly.bridge_status})")
    return "\n".join(lines)


def _combined_chat_message(
    verdict: TriageVerdict,
    decision: RoutingDecision,
    assembly: WarRoomAssembly | None,
) -> ChatMessage:
    """One ChatMessage for the whole incident: the routing notification, with
    the war-room link inline when a room was created."""
    body = decision.body
    actions = list(decision.actions)
    if assembly is not None and assembly.assembled:
        body = f"{body}\n{_war_room_section(assembly)}"
        actions = [*actions, "open_war_room"]
    return ChatMessage(
        channel=decision.channel,
        severity=decision.chat_severity,
        title=decision.title,
        body=body,
        incident_id=verdict.incident_id,
        service=verdict.affected_service,
        category_display=decision.category_display,
        mentions=list(decision.mentions),
        actions=actions,
        response_mode=decision.response_mode,
        assignee=decision.assignee,
        assignee_name=decision.assignee_name,
        assignee_email=decision.assignee_email,
        timestamp=decision.decided_at,
    )


def decide(verdict: TriageVerdict, *, now: datetime | None = None) -> NotificationAssembly:
    """Pure decision — no side effects. Safe to call in tests and evals.

    Resolves the on-call ONCE, then computes the routing decision and the
    war-room plan from it. The war room is only ``assembled=True`` for
    Sev-1/Sev-2 Active verdicts. ``war_room`` is ``None`` only when the routing
    was Suppressed. Use :func:`notify` to actually create the bridge and emit.
    """
    now = now or datetime.now(UTC)
    oncall = _resolve_oncall(verdict)
    decision = _decide_routing(verdict, now, oncall)
    if verdict.status == "Suppressed":
        return NotificationAssembly(decision=decision, war_room=None)
    war_room = _decide_war_room(verdict, now, oncall)
    return NotificationAssembly(decision=decision, war_room=war_room)


def notify(verdict: TriageVerdict, *, now: datetime | None = None) -> NotificationOutcome:
    """Decide, stand up the war room (Sev-1/Sev-2), and emit ONE chatops message.

    Side effects: (1) for Sev-1/Sev-2 Active verdicts, creates the Slack
    war room via the ``chatops.war_room.create`` seam (simulated when no bot
    token) so the join link is real; (2) sends a single ``ChatMessage`` through
    the chatops seam — the routing notification with the war-room link folded
    in. Suppressed verdicts short-circuit both (empty actions → no emit).
    """
    plan = decide(verdict, now=now)
    decision = plan.decision
    assembly = plan.war_room

    if not decision.actions:
        # Suppressed — no chatops emit. Still return the (no-op) war-room plan
        # so the dashboard feed can record the incident as "no room".
        logger.info(
            "RA-005+006: skipped emit for %s on %s (%s)",
            verdict.severity,
            verdict.affected_service,
            decision.reason,
        )
        return NotificationOutcome(decision=decision, war_room=assembly, deliveries={})

    # Stand up the actual Slack room first so the single notification can carry
    # the join link. Bridge creation is offline-safe (simulated without a token).
    if assembly is not None and assembly.assembled:
        assembly = _create_bridge(assembly)

    msg = _combined_chat_message(verdict, decision, assembly)
    deliveries = get_client().send(msg)
    logger.info(
        "RA-005+006: notified %s on %s -> %s (%s); war_room=%s",
        verdict.severity,
        verdict.affected_service,
        decision.channel,
        decision.chat_severity,
        assembly.bridge_status if (assembly and assembly.assembled) else "none",
    )
    return NotificationOutcome(decision=decision, war_room=assembly, deliveries=deliveries)


def decide_war_room(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomAssembly:
    """War-room-only pure decision (no bridge, no emit). Used by the dashboard
    try-it inspector to preview how the agent reacts to any severity/status."""
    now = now or datetime.now(UTC)
    return _decide_war_room(verdict, now, _resolve_oncall(verdict))


def assemble_war_room(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomAssembly:
    """War-room-only assembly: decide then create the real/simulated Slack
    bridge. No chatops notification is emitted — that's :func:`notify`'s job.
    Returns the bridge-enriched assembly (``assembled=False`` for minor sevs)."""
    assembly = decide_war_room(verdict, now=now)
    if assembly.assembled:
        assembly = _create_bridge(assembly)
    return assembly


# ─── standalone-unit entry points ──────────────────────────────────────────
# These let the individually-sellable wrapper agents (``notification_router`` /
# ``war_room_assembler``) be deployed alone with their original one-job-each
# contract. The integrated product flow uses :func:`notify` instead (one
# message). The implementation stays here so there is a single source of truth.


def decide_routing(verdict: TriageVerdict, *, now: datetime | None = None) -> RoutingDecision:
    """Routing-only pure decision (former RA-005 ``decide``). No war room."""
    now = now or datetime.now(UTC)
    return _decide_routing(verdict, now, _resolve_oncall(verdict))


def route(verdict: TriageVerdict, *, now: datetime | None = None) -> RoutingOutcome:
    """Routing-only emit (standalone Notification Router): decide and send ONE
    routing notification through the chatops seam. No war room is stood up.
    Suppressed verdicts short-circuit the emit (empty actions)."""
    decision = decide_routing(verdict, now=now)
    if not decision.actions:
        return RoutingOutcome(decision=decision, deliveries={})
    msg = _combined_chat_message(verdict, decision, None)
    deliveries = get_client().send(msg)
    return RoutingOutcome(decision=decision, deliveries=deliveries)


def _war_room_opening_chat_message(
    verdict: TriageVerdict, assembly: WarRoomAssembly
) -> ChatMessage:
    """The standalone war-room opening message (former RA-006 emit): posted to
    the war-room channel with the context pack + join link, not the routing
    channel."""
    return ChatMessage(
        channel=assembly.channel,
        severity=assembly.chat_severity,
        title=assembly.title,
        body=_render_war_room_opening(assembly),
        incident_id=verdict.incident_id,
        service=verdict.affected_service,
        mentions=[s.handle for s in assembly.invited],
        actions=["open_war_room", "post_context_pack"],
        timestamp=assembly.assembled_at,
    )


def assemble(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomOutcome:
    """War-room-only emit (standalone War-Room Assembler): create the bridge and
    post the war-room opening through the chatops seam. No routing notification.
    A no-op assembly (Sev-3/Sev-4/Suppressed) short-circuits the emit."""
    assembly = assemble_war_room(verdict, now=now)
    if not assembly.assembled:
        return WarRoomOutcome(assembly=assembly, deliveries={})
    msg = _war_room_opening_chat_message(verdict, assembly)
    deliveries = get_client().send(msg)
    return WarRoomOutcome(assembly=assembly, deliveries=deliveries)


def run(input_payload: dict) -> dict[str, Any]:
    """Eval-harness entry point.

    Accepts ``{"verdict": {...}, "now": "ISO8601"}`` and returns a flat,
    JSON-friendly dict carrying both the routing decision fields and the
    war-room outcome under ``war_room_*`` keys (so the flat eval scorer can
    assert either dimension). Pure — does not emit through the chatops seam.
    """
    verdict = TriageVerdict.model_validate(input_payload["verdict"])
    now_raw = input_payload.get("now")
    now: datetime | None = None
    if isinstance(now_raw, str):
        now = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
    plan = decide(verdict, now=now)
    d = plan.decision
    wr = plan.war_room
    return {
        "chat_severity": str(d.chat_severity),
        "channel": d.channel,
        "title": d.title,
        "body": d.body,
        "mentions": list(d.mentions),
        "actions": list(d.actions),
        "reason": d.reason,
        "response_mode": d.response_mode,
        "war_room_assembled": bool(wr.assembled) if wr else False,
        "war_room_channel": wr.channel if wr else None,
        "war_room_chat_severity": str(wr.chat_severity) if wr else "info",
        "war_room_reason": wr.reason if wr else "suppressed — no war room",
    }
