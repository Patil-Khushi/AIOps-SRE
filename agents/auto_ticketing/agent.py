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

import json
import logging
from pathlib import Path
from typing import Any

# Side-effect imports register providers with the registry.
import aiops.tools.itsm
import aiops.tools.mock_providers
import aiops.tools.observability  # noqa: F401  — registers grafana.render_panel
from agents.alert_triage.models import Severity, TriageVerdict
from agents.auto_ticketing.models import TicketRecord, TicketSystem
from agents.incident_classifier.models import Classification
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# ─── DEMO-8 / #60: Grafana panel attachment ──────────────────────────────
#
# Loaded lazily so a missing or malformed JSON file doesn't break import.
# The mapping keys are Prometheus alert rule names (the ``metric`` field on
# the canonical ``Alert`` model); values carry the Grafana dashboard UID
# and panel coordinates.  See ``grafana_panels.json`` for the schema.
_PANEL_MAP_PATH = Path(__file__).parent / "grafana_panels.json"
_PANEL_MAP: dict[str, dict[str, Any]] | None = None


def _panel_for(alert_name: str) -> dict[str, Any] | None:
    """Look up the Grafana panel mapping for an alert. Returns ``None`` if
    the alert is unmapped (most alerts won't have a panel — the demo only
    wires the high-value ones)."""
    global _PANEL_MAP
    if _PANEL_MAP is None:
        try:
            raw = json.loads(_PANEL_MAP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("could not load grafana_panels.json (%s); skipping attachments", exc)
            _PANEL_MAP = {}
        else:
            # Strip the docstring key so it can never collide with a real alert name.
            _PANEL_MAP = {
                k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)
            }
    return _PANEL_MAP.get(alert_name)


def _reload_panel_map_for_tests() -> None:
    """Test seam — clears the lazy cache so a monkeypatched JSON path reloads."""
    global _PANEL_MAP
    _PANEL_MAP = None


def _try_attach_grafana_panel(
    *,
    registry: Any,
    sys_id: str,
    alert_name: str,
    audit: list[str],
) -> None:
    """Render the alert's Grafana panel and attach it to the incident.

    Every failure path is logged to ``audit`` and swallowed — ticket
    creation has already succeeded, and a missing attachment must not
    erase that success.  Failure modes worth distinguishing in the audit
    log: alert unmapped (most alerts), Grafana unreachable / plugin
    missing, ServiceNow attachment endpoint failure.
    """
    panel = _panel_for(alert_name)
    if panel is None:
        audit.append(f"grafana attachment skipped: no panel mapped for alert {alert_name!r}")
        return

    try:
        render = registry.call(
            "observability.metrics.render_panel",
            dashboard_uid=panel["dashboard_uid"],
            panel_id=int(panel["panel_id"]),
            from_=panel.get("from", "now-15m"),
            to=panel.get("to", "now"),
        )
    except Exception as exc:  # registry-level (capability not registered)
        audit.append(f"grafana attachment skipped: render_panel raised {type(exc).__name__}: {exc}")
        return

    if not render.ok or not render.data:
        audit.append(f"grafana render_panel failed: {render.error}")
        return

    png_bytes = render.data.get("png_bytes")
    if not isinstance(png_bytes, bytes) or not png_bytes:
        audit.append("grafana render_panel returned no png_bytes; skipping attach")
        return

    file_name = f"{alert_name}.png"
    try:
        attach = registry.call(
            "itsm.incident.attachment.add",
            sys_id=sys_id,
            file_name=file_name,
            content=png_bytes,
            content_type=render.data.get("content_type", "image/png"),
        )
    except Exception as exc:
        audit.append(
            f"grafana attachment skipped: incident.attachment.add raised "
            f"{type(exc).__name__}: {exc}"
        )
        return

    if attach.ok:
        audit.append(
            f"grafana panel attached ({file_name}, "
            f"{len(png_bytes)} bytes, "
            f"attachment_sys_id={(attach.data or {}).get('attachment_sys_id')})"
        )
    else:
        audit.append(f"grafana incident.attachment.add failed: {attach.error}")


_SEV1_CHANNEL = "oncall"
_DEFAULT_CHANNEL = "alerts-noise"

# RA-002's incident_type taxonomy is internal; ServiceNow's ``category`` field
# is a fixed choice list (inquiry / software / hardware / network / database).
# Stock PDIs silently drop unknown values (the user sees the default "Inquiry
# / Help" instead of the real category), so we translate at the boundary.
# Keeping the mapping in the auto-ticketing agent — not the classifier — so
# the classifier's taxonomy stays decoupled from any specific ITSM vendor's
# category model.
_INCIDENT_TYPE_TO_SNOW_CATEGORY: dict[str, str] = {
    "infrastructure": "hardware",
    "application": "software",
    "network": "network",
    "external_dependency": "software",
    "change_related": "software",
}


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


def _build_description(
    verdict: TriageVerdict,
    classification: Classification | None,
) -> str:
    """Multi-paragraph triage context for ServiceNow's ``description`` field.

    The short_description is capped at 160 chars and only carries the alert
    headline; a human opening the incident needs the full narrative, the
    routing context, the classifier's verdict (when available), and RA-001's
    8-stage decision trace to act without re-running the agent. Layout is
    plain text with bare section labels so it survives ServiceNow's HTML
    sanitization unchanged. Lines stay aligned with a fixed-width key column
    so the field renders readably in both ServiceNow's monospace
    activity-stream view and the dashboard's prose pane.
    """
    sections: list[str] = []

    sections.append("ALERT SUMMARY\n" + verdict.alert_summary.strip())

    routing_lines = [
        f"  Severity       : {verdict.severity}",
        f"  Confidence     : {verdict.confidence_score:.2f}",
        f"  Assigned team  : {verdict.assigned_team}",
        f"  On-call        : {verdict.assigned_engineer or 'unassigned'}",
        f"  Runbook        : {verdict.recommended_runbook or 'none'}",
    ]
    sections.append("INCIDENT ROUTING\n" + "\n".join(routing_lines))

    if classification is not None:
        tags = ", ".join(classification.tags) if classification.tags else "none"
        cls_lines = [
            f"  Type           : {classification.incident_type}",
            f"  Confidence     : {classification.confidence:.2f}",
            f"  Probable cause : {classification.probable_root_cause}",
            f"  Rationale      : {classification.rationale}",
            f"  Tags           : {tags}",
        ]
        sections.append("CLASSIFICATION (RA-002)\n" + "\n".join(cls_lines))
    else:
        # Placeholder kept in the body even when classification is missing so
        # the structure of the description does not change between pipeline
        # variants. The route can patch the block in later via
        # itsm.incident.update once classification has run.
        sections.append(
            "CLASSIFICATION (RA-002)\n  Pending — classifier has not run for this incident yet."
        )

    trace = list(verdict.audit_metadata.decision_trace or [])
    if trace:
        numbered = "\n".join(f"  {i}. {step}" for i, step in enumerate(trace, 1))
        sections.append("DECISION TRACE (RA-001)\n" + numbered)
    else:
        sections.append("DECISION TRACE (RA-001)\n  (no trace recorded)")

    sections.append("— Generated by Auto-Ticketing (RA-003)")
    return "\n\n".join(sections)


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

    The harness does not supply an RA-002 classification, so the resulting
    ticket description carries the "Pending classification" placeholder.
    The production route (`demo/ui/server.py`) calls ``ticket(...)``
    directly with the classification kwarg populated.
    """
    verdict = TriageVerdict.model_validate(input)
    return ticket(verdict).model_dump(mode="json")


def ticket(
    verdict: TriageVerdict,
    classification: Classification | None = None,
    *,
    alert_name: str | None = None,
) -> TicketRecord:
    """File an ITSM ticket + chat-ops notification for a triage verdict.

    Always returns a ``TicketRecord``; never raises. Three outcomes:

    - Suppressed verdict -> ``created=False`` with a skip reason.
    - ITSM create fails -> ``created=True, ticket_id=None``; chat-ops still
      gets a creation-failed marker so a human sees the alert.
    - Happy path -> ``created=True`` with provider ticket id, urgency,
      channel_notified populated.

    ``classification`` (RA-002) is optional. When supplied, its
    ``incident_type`` is forwarded as the ServiceNow ``category`` and its
    structured fields are inlined into the description body. When omitted —
    e.g. the eval harness which only feeds verdicts — the description still
    contains every other block plus a "Pending classification" placeholder
    so a human triaging the ticket sees the same structure either way.

    ``alert_name`` (DEMO-8 / #60) is the Prometheus alert rule name (the
    ``Alert.metric`` field — e.g. ``PaymentErrorRateHigh``).  When supplied
    AND the ticket lands in ServiceNow AND ``grafana_panels.json`` maps
    that alert to a panel, the agent renders the panel and attaches it
    to the incident.  Failure at any step is non-fatal and audited; the
    ticket creation itself is unaffected.
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
    description = _build_description(verdict, classification)
    assignment_group = verdict.assigned_team
    category: str | None = None
    if classification is not None:
        # Translate RA-002's taxonomy to a ServiceNow stock category. Unknown
        # types fall back to None (rather than passing the raw RA-002 string)
        # so ServiceNow doesn't silently drop the value to its default.
        category = _INCIDENT_TYPE_TO_SNOW_CATEGORY.get(classification.incident_type)
    audit.append(f"mapped severity={verdict.severity} -> urgency={urgency}, channel={channel}")
    audit.append(
        "built description with sections: alert_summary, routing, classification, decision_trace"
    )
    if classification is None:
        audit.append("classification not supplied — description carries the pending placeholder")
    elif category is None:
        audit.append(
            f"incident_type={classification.incident_type!r} has no ServiceNow category mapping"
        )
    audit.append(f"assignment_group={assignment_group}, category={category or 'unset'}")

    ticket_id: str | None = None
    system: TicketSystem = "none"
    try:
        result = registry.call(
            "itsm.incident.create",
            short_description=short_description,
            urgency=urgency,
            description=description,
            assignment_group=assignment_group,
            category=category,
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
            # DEMO-8 / #60: render + attach the matching Grafana panel.
            # ServiceNow only — the mock provider has no attachment endpoint
            # and the demo's value is the human triager seeing the graph in
            # ServiceNow, not in a mock JSON blob.
            if system == "servicenow":
                snow_sys_id = data.get("sys_id")
                if snow_sys_id and alert_name:
                    _try_attach_grafana_panel(
                        registry=registry,
                        sys_id=snow_sys_id,
                        alert_name=alert_name,
                        audit=audit,
                    )
                elif not alert_name:
                    audit.append("grafana attachment skipped: alert_name not supplied by caller")
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
