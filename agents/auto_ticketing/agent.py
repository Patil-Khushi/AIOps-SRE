"""Auto-Ticketing agent (RA-003) — TriageVerdict -> ITSM ticket + chatops notify.

Entry point: ``run(verdict: dict) -> dict``.

Flow:

    1. Validate input as a ``TriageVerdict`` (re-uses the canonical model
       from alert_triage so the contract is single-sourced).
    2. Short-circuit on Suppressed verdicts — duplicate alerts must not
       produce duplicate INC tickets.
    3. Map severity to ServiceNow urgency (1=High / 2=Med / 3=Low).
    4. Build the ITSM payload from the verdict.
    5. ``registry.call("itsm.incident.create", ...)``. Continue on error —
       chat-ops notification still goes out so a human sees the alert.
    6. Map severity to a chat channel (Sev-1 -> oncall, everyone else ->
       alerts-noise). The eventual Notification Router (#35) will own this.
    7. ``registry.call("notify.send", channel=..., message=...)``.
    8. Return a ``TicketRecord`` dict.

Vendor neutrality: this module imports ``aiops.tools`` only. ServiceNow
specifics live in ``aiops.tools.itsm.servicenow``; the mock CMDB and notify
sink live in ``aiops.tools.mock_providers``.
"""

from __future__ import annotations

import logging
from typing import Any

# Side-effect imports register providers with the registry.
import aiops.tools.itsm
import aiops.tools.mock_providers  # noqa: F401
from agents.alert_triage.models import Severity, TriageVerdict
from agents.auto_ticketing.models import TicketRecord, TicketSystem
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

_SEV1_CHANNEL = "oncall"
_DEFAULT_CHANNEL = "alerts-noise"


def _severity_to_urgency(severity: Severity) -> int:
    """ServiceNow urgency is 1=High / 2=Medium / 3=Low. Sev-4 clamps to 3."""
    return {"Sev-1": 1, "Sev-2": 2, "Sev-3": 3, "Sev-4": 3}[severity]


def _severity_to_channel(severity: Severity) -> str:
    """Sev-1 goes to the oncall channel; everything else to the noise bucket.

    Notification Router (#35) will replace this with policy-driven routing
    that respects tenant config + on-call schedules. Until then, two
    channels is enough to demo the path.
    """
    return _SEV1_CHANNEL if severity == "Sev-1" else _DEFAULT_CHANNEL


def _build_short_description(verdict: TriageVerdict) -> str:
    """ServiceNow's short_description has a 160-char limit; cap aggressively."""
    summary = verdict.alert_summary.strip()
    head = f"[{verdict.severity}] {verdict.affected_service}: "
    budget = 160 - len(head)
    return head + (summary[:budget] if len(summary) > budget else summary)


def reset_state() -> None:
    """Eval-harness hook (A11). Auto-Ticketing holds no in-process state —
    the mock ITSM provider returns a fixed id and the chat-ops sink is
    fire-and-forget — but the harness contract expects this symbol to exist."""
    return None


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out.

    The input is a ``TriageVerdict`` dict (the upstream agent's
    ``.model_dump(mode="json")``). Validation is strict — extra keys
    raise pydantic ValidationError, which the harness records as a
    failed case rather than silently swallowing schema drift.
    """
    verdict = TriageVerdict.model_validate(input)
    return ticket(verdict).model_dump(mode="json")


def ticket(verdict: TriageVerdict) -> TicketRecord:
    """File an ITSM ticket + chat-ops notification for a triage verdict.

    Always returns a ``TicketRecord``; never raises. Three outcomes:

    - Suppressed verdict -> ``created=False`` with a skip reason.
    - ITSM create fails -> ``created=True, ticket_id=None``; chat-ops still
      gets a creation-failed marker so a human sees the alert.
    - Happy path -> ``created=True`` with provider ticket id, urgency,
      channel_notified populated.
    """
    audit: list[str] = []
    registry = get_registry()

    if verdict.status == "Suppressed":
        audit.append("skipped: status=Suppressed")
        audit.append(f"duplicate cluster covered {verdict.duplicate_alert_count} alert(s)")
        return TicketRecord(created=False, audit_metadata=audit)

    urgency = _severity_to_urgency(verdict.severity)
    channel = _severity_to_channel(verdict.severity)
    short_description = _build_short_description(verdict)
    audit.append(f"mapped severity={verdict.severity} -> urgency={urgency}, channel={channel}")

    ticket_id: str | None = None
    system: TicketSystem = "none"
    try:
        result = registry.call(
            "itsm.incident.create",
            short_description=short_description,
            urgency=urgency,
        )
    except Exception as exc:
        # Registry-level failure (capability not registered, etc.) is rare —
        # tests rely on the mock being registered — but recover by skipping
        # the ITSM step and still firing chat-ops.
        audit.append(f"itsm.incident.create raised {type(exc).__name__}: {exc}")
        result = None

    if result is not None:
        if result.ok and result.data:
            data = result.data
            # ServiceNow returns ``number`` ("INC0010001"); the mock returns ``id``.
            # Surface whichever is present; agents downstream do not care which.
            ticket_id = data.get("number") or data.get("id") or data.get("sys_id")
            system = "servicenow" if result.metadata.get("provider") == "servicenow" else "mock"
            audit.append(f"itsm.incident.create ok (system={system}, ticket_id={ticket_id})")
        else:
            audit.append(f"itsm.incident.create failed: {result.error}")

    notification_text = _build_notification(verdict, ticket_id, channel)
    notification_sent = False
    try:
        notify_result = registry.call(
            "notify.send",
            channel=channel,
            message=notification_text,
        )
        if notify_result.ok:
            notification_sent = True
            audit.append(f"notify.send ok (channel={channel})")
        else:
            audit.append(f"notify.send failed: {notify_result.error}")
    except Exception as exc:
        audit.append(f"notify.send raised {type(exc).__name__}: {exc}")

    return TicketRecord(
        created=True,
        ticket_id=ticket_id,
        system=system,
        urgency=urgency,
        short_description=short_description,
        channel_notified=channel,
        notification_sent=notification_sent,
        audit_metadata=audit,
    )


def _build_notification(verdict: TriageVerdict, ticket_id: str | None, channel: str) -> str:
    """Plain-text chat-ops payload. Replaced by ``ChatopsMessage`` once D1 lands."""
    ticket_marker = ticket_id if ticket_id else "CREATION_FAILED"
    engineer = f" (on-call: {verdict.assigned_engineer})" if verdict.assigned_engineer else ""
    return (
        f"[{verdict.severity}] {verdict.affected_service} — {verdict.alert_summary} "
        f"-> ticket {ticket_marker}, team {verdict.assigned_team}{engineer} "
        f"[#{channel}]"
    )
