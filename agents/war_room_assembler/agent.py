"""War-Room Assembler agent (RA-006).

Reads a ``TriageVerdict`` (RA-001, ticketed by RA-003) and, for severe
incidents (Sev-1 / Sev-2), stands up the incident war room: a dedicated
chatops channel, the on-call SME pulled in, a live context pack (current
metrics + recent traces), and a seed timeline for RCA.

v1 scope (deliberately small, expand once evals are green):

- **SME selection: on-call only.** One ``oncall.schedule.lookup`` for the
  owning team — the same path RA-005 already trusts. CMDB-owner and
  dependency-owner invites are a v2 add (see README).
- **Context pack: live telemetry.** Real ``observability.metrics.query`` +
  ``observability.traces.search`` calls. When the cluster isn't up (evals,
  dry-run) the registry returns ``ok=False`` and each item degrades to
  "unavailable" — the assembly still succeeds.

Sev-3 / Sev-4 and Suppressed verdicts get a no-op assembly (``assembled=False``);
they don't warrant a war room.

Public surface::

    from agents.war_room_assembler import decide, assemble, WarRoomAssembly
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from agents.alert_triage import TriageVerdict
from aiops.tools import get_registry
from aiops.tools.chatops import ChatMessage, Severity, get_client

from .models import (
    ContextPackItem,
    InvitedSME,
    TimelineEvent,
    WarRoomAssembly,
    WarRoomOutcome,
)

logger = logging.getLogger(__name__)

# Only these severities warrant a war room. Sev-3/Sev-4 are handled by RA-005
# routing alone (chat / noise bucket).
_WAR_ROOM_SEVERITIES = {"Sev-1", "Sev-2"}

# Map triage severity -> chatops loudness for the opening post.
_CHAT_SEVERITY = {"Sev-1": Severity.P1, "Sev-2": Severity.P2}

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


def _resolve_oncall(verdict: TriageVerdict) -> dict | None:
    """One on-call lookup for the owning team (v1 SME source).

    Returns the provider's data dict, or ``None`` if the lookup wasn't
    registered or failed. ``registry.call`` filters kwargs by signature, so
    passing ``team`` to the mock provider (which only accepts ``team``) is
    safe even though we don't enrich with category keywords here.
    """
    try:
        result = get_registry().call(
            "oncall.schedule.lookup",
            team=verdict.assigned_team,
        )
    except KeyError:
        return None
    if result.ok and isinstance(result.data, dict):
        return result.data
    return None


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
    line. Any failure (no cluster, gate block, provider error) becomes
    ``"unavailable"`` rather than raising — the assembly must not depend on
    live infra being up."""
    result = get_registry().call(capability, **kwargs)
    if not result.ok:
        return ContextPackItem(label=label, value="unavailable", source=capability)
    return ContextPackItem(label=label, value=str(result.data), source=capability)


def _build_context_pack(verdict: TriageVerdict) -> list[ContextPackItem]:
    """Live snapshot for the room. Static facts from the verdict first (always
    present), then best-effort live telemetry for the affected service."""
    svc = verdict.affected_service
    pack: list[ContextPackItem] = [
        ContextPackItem(label="Affected service", value=svc, source="verdict"),
        ContextPackItem(label="Severity", value=verdict.severity, source="verdict"),
    ]
    if verdict.recommended_runbook:
        pack.append(
            ContextPackItem(
                label="Runbook", value=verdict.recommended_runbook, source="verdict"
            )
        )
    # Best-effort live telemetry. PromQL kept generic so it works against the
    # OTel demo's standard request metrics; degrades to "unavailable" off-cluster.
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


def decide(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomAssembly:
    """Pure assembly decision — no side effects. Safe in tests and evals.

    Returns a ``WarRoomAssembly``. For Sev-3/Sev-4 or Suppressed verdicts it
    returns ``assembled=False`` (no war room). Use ``assemble`` to actually
    create the room through the chatops seam.
    """
    now = now or datetime.now(UTC)
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

    chat_sev = _CHAT_SEVERITY[sev]
    oncall = _resolve_oncall(verdict)
    if oncall and oncall.get("engineer_name"):
        audit.append(f"oncall: {oncall['engineer_name']!r} for {verdict.assigned_team!r}")
    else:
        audit.append("oncall: no lookup result — falling back to verdict.assigned_engineer")

    invited = _invited_smes(verdict, oncall)
    audit.append(f"smes: invited {len(invited)} (source=oncall)")

    context_pack = _build_context_pack(verdict)
    live = sum(1 for i in context_pack if i.source and i.source.startswith("observability") and i.value != "unavailable")
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


def _render_opening_body(assembly: WarRoomAssembly) -> str:
    """The opening post for the war room: who's in, the context pack, and the
    seed timeline — one ``key: value`` per line so every renderer agrees."""
    lines: list[str] = [assembly.reason, ""]
    if assembly.invited:
        lines.append("SMEs: " + ", ".join(s.handle for s in assembly.invited))
    lines.append("")
    lines.append("Context pack:")
    lines.extend(f"  {i.label}: {i.value}" for i in assembly.context_pack)
    lines.append("")
    lines.append("Timeline:")
    lines.extend(f"  {e.at.isoformat()} — {e.event}" for e in assembly.timeline)
    return "\n".join(lines)


def _assembly_to_chat_message(verdict: TriageVerdict, assembly: WarRoomAssembly) -> ChatMessage:
    return ChatMessage(
        channel=assembly.channel,
        severity=assembly.chat_severity,
        title=assembly.title,
        body=_render_opening_body(assembly),
        incident_id=verdict.incident_id,
        service=verdict.affected_service,
        mentions=[s.handle for s in assembly.invited],
        actions=["open_war_room", "post_context_pack"],
        timestamp=assembly.assembled_at,
    )


def assemble(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomOutcome:
    """Decide and emit. Returns the assembly plus per-adapter deliveries.

    Side effect: posts the war-room opening (context pack + timeline) through
    the chatops seam. A no-op assembly (``assembled=False``) short-circuits
    the emit so minor / suppressed incidents never open a room.
    """
    assembly = decide(verdict, now=now)
    if not assembly.assembled:
        logger.info(
            "RA-006: no war room for %s on %s (%s)",
            verdict.severity,
            verdict.affected_service,
            assembly.reason,
        )
        return WarRoomOutcome(assembly=assembly, deliveries={})
    msg = _assembly_to_chat_message(verdict, assembly)
    deliveries = get_client().send(msg)
    logger.info(
        "RA-006: assembled war room %s for %s on %s (%d SMEs)",
        assembly.channel,
        verdict.severity,
        verdict.affected_service,
        len(assembly.invited),
    )
    return WarRoomOutcome(assembly=assembly, deliveries=deliveries)


def run(input_payload: dict) -> dict:
    """Eval-harness entry point. Accepts ``{"verdict": {...}, "now": "ISO8601"}``
    and returns the ``WarRoomAssembly`` as a JSON-friendly dict. Pure — does
    not emit through the chatops seam."""
    verdict = TriageVerdict.model_validate(input_payload["verdict"])
    now_raw = input_payload.get("now")
    now: datetime | None = None
    if isinstance(now_raw, str):
        now = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
    return decide(verdict, now=now).model_dump(mode="json")
