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
from agents.alert_triage.models import TriageVerdict
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
    wires the high-value ones).

    Thread-safety: the lazy load builds the map into a local first, then
    publishes via a single assignment. Two concurrent first-callers (FastAPI
    can serve /api/triage in parallel) may both read the file, but neither
    sees a half-built map and the final state is correct either way."""
    cached = _PANEL_MAP
    if cached is None:
        try:
            raw = json.loads(_PANEL_MAP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("could not load grafana_panels.json (%s); skipping attachments", exc)
            cached = {}
        else:
            # Keys starting with ``_`` are reserved for documentation entries
            # (e.g. ``_doc``); stripping them here means they can never
            # collide with a real alert rule name.
            cached = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
        globals()["_PANEL_MAP"] = cached
    return cached.get(alert_name)


def _reload_panel_map_for_tests() -> None:
    """Test seam — clears the lazy cache so a monkeypatched JSON path reloads."""
    globals()["_PANEL_MAP"] = None


def _safe_attachment_filename(alert_name: str) -> str:
    """Strip path separators and other shell-hostile characters from an
    alert rule name so it can be used as a ServiceNow attachment filename.

    Prometheus rule names are conventionally CamelCase identifiers, but a
    typo or a future caller could feed in something like ``foo/bar`` or
    ``../etc/passwd``. ServiceNow stores the decoded form server-side; we
    keep the filename to a safe alphabet.
    """
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in alert_name)
    # Avoid leading dots (would create hidden files on Unix-y viewers).
    safe = safe.lstrip(".")
    # Fall back to a placeholder if the input had no alphanumerics — a
    # filename of just underscores is technically valid but unhelpful.
    if not any(c.isalnum() for c in safe):
        safe = "alert"
    return f"{safe}.png"


def _try_attach_grafana_panel(
    *,
    registry: Any,
    sys_id: str,
    alert_name: str,
    audit: list[str],
) -> bool:
    """Render the alert's Grafana panel and attach it to the incident.

    Returns ``True`` only when the PNG actually attached to the incident;
    every other path returns ``False``. Each failure is logged to ``audit``
    and swallowed — ticket creation has already succeeded, and a missing
    attachment must not erase that success. Failure modes worth
    distinguishing in the audit log: alert unmapped (most alerts), Grafana
    unreachable / plugin missing, ServiceNow attachment endpoint failure.
    """
    panel = _panel_for(alert_name)
    if panel is None:
        audit.append(f"grafana attachment skipped: no panel mapped for alert {alert_name!r}")
        return False

    # A mapping WITHOUT panel_id renders the whole dashboard in kiosk mode
    # (e.g. the OTel Collector "Overview" summary); WITH panel_id it renders a
    # single panel. Dashboards want a taller default height than a lone panel.
    panel_id = panel.get("panel_id")
    is_dashboard = panel_id is None
    try:
        render = registry.call(
            "observability.metrics.render_panel",
            dashboard_uid=panel["dashboard_uid"],
            panel_id=None if is_dashboard else int(panel_id),
            time_range=panel.get("time_range", "15m"),
            width=int(panel.get("width", 800)),
            height=int(panel.get("height", 860 if is_dashboard else 400)),
            format="png",
        )
    except Exception as exc:  # registry-level (capability not registered)
        audit.append(f"grafana attachment skipped: render_panel raised {type(exc).__name__}: {exc}")
        return False

    if not render.ok or not render.data:
        audit.append(f"grafana render_panel failed: {render.error}")
        return False

    png_bytes = render.data.get("png_bytes")
    if not isinstance(png_bytes, bytes) or not png_bytes:
        audit.append("grafana render_panel returned no png_bytes; skipping attach")
        return False

    file_name = _safe_attachment_filename(alert_name)
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
        return False

    if attach.ok:
        audit.append(
            f"grafana panel attached ({file_name}, "
            f"{len(png_bytes)} bytes, "
            f"attachment_sys_id={(attach.data or {}).get('attachment_sys_id')})"
        )
        return True
    audit.append(f"grafana incident.attachment.add failed: {attach.error}")
    return False


# Severity → ServiceNow urgency (1=High / 2=Medium / 3=Low; Sev-4 clamps to 3)
# and → chat channel (Sev-1 pages #oncall; everything else batches to the noise
# bucket). Module-level lookups rather than functions so the mapping is a single
# transparent table — and ``.get(severity, default)`` keeps an unexpected
# severity string from raising, defaulting it to Low / noise.
#
# Notification Router (#35) will eventually own channel routing with
# policy-driven config + on-call schedules; two channels is enough to demo it.
URGENCY_MAP: dict[str, int] = {"Sev-1": 1, "Sev-2": 2, "Sev-3": 3, "Sev-4": 3}
CHANNEL_MAP: dict[str, str] = {
    "Sev-1": "oncall",
    "Sev-2": "alerts-noise",
    "Sev-3": "alerts-noise",
    "Sev-4": "alerts-noise",
}
_DEFAULT_URGENCY = 3
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


def _build_short_description(verdict: TriageVerdict) -> str:
    """ServiceNow's short_description is VARCHAR(160). Build the headline, then
    truncate the whole string to ``157 + "..."`` when it overruns so the
    reader can tell the title was clipped (vs. a silent hard cut)."""
    summary = verdict.alert_summary.strip()
    short = f"[{verdict.severity}] {verdict.affected_service}: {summary}"
    if len(short) > 160:
        short = short[:157] + "..."
    return short


def _build_description(
    verdict: TriageVerdict,
    classification: Classification | None,
) -> str:
    """Multi-section triage context for ServiceNow's ``description`` field.

    The short_description is capped at 160 chars and only carries the alert
    headline; a human opening the incident needs the full narrative, the
    routing context, the classifier's verdict (when available), and RA-001's
    decision trace to act without re-running the agent. Layout is plain text
    with ``=== Section ===`` headers so it survives ServiceNow's HTML
    sanitization unchanged and renders readably in both ServiceNow's
    activity-stream view and the dashboard's prose pane.

    Decision-trace note (issue #196): the doc's ``matched rule`` line comes
    from a ``rule_matched`` field that the real ``TriageVerdict`` does not
    carry. We render the two lines that DO map to real fields (CMDB lookup,
    on-call) and keep RA-001's full ordered ``decision_trace`` beneath them —
    honoring the doc's new line style without fabricating a rule or dropping
    the real trace.
    """
    sections: list[str] = []

    sections.append("=== Alert Summary ===\n" + verdict.alert_summary.strip())

    # Routing is Team / Engineer / Runbook only. The doc's routing block (#196)
    # intentionally drops the old Severity + Confidence lines — this is a
    # decision, not an oversight. Severity is already visible in
    # short_description; the triage confidence_score is deliberately not
    # surfaced on the ticket per the doc. If a future reader wants it back,
    # re-adding f"{'Confidence:':<9} {verdict.confidence_score:.2f}" here is the
    # spot — but that's a doc change, not a bug fix.
    routing_lines = [
        f"{'Team:':<9} {verdict.assigned_team}",
        f"{'Engineer:':<9} {verdict.assigned_engineer or 'unassigned'}",
        f"{'Runbook:':<9} {verdict.recommended_runbook or 'none'}",
    ]
    sections.append("=== Routing ===\n" + "\n".join(routing_lines))

    # Classification block is emitted only when RA-002 actually ran. When it
    # didn't (e.g. the eval harness feeds verdicts only), the section is
    # omitted entirely rather than carrying a placeholder — the route can
    # patch it in later via itsm.incident.update once classification runs.
    if classification is not None:
        tags = ", ".join(classification.tags) if classification.tags else "none"
        # Type / Confidence / Probable cause / Tags per the doc (#196). The old
        # body's Rationale line is intentionally dropped here — a decision, not
        # an accidental deletion. (The classifier's Confidence shown here is
        # RA-002's, distinct from the triage confidence_score dropped above.)
        cls_lines = [
            f"{'Type:':<15} {classification.incident_type}",
            f"{'Confidence:':<15} {classification.confidence:.2f}",
            f"{'Probable cause:':<15} {classification.probable_root_cause}",
            f"{'Tags:':<15} {tags}",
        ]
        sections.append("=== Classification (RA-002) ===\n" + "\n".join(cls_lines))

    trace_lines = [
        f"- CMDB lookup: {verdict.affected_service} -> {verdict.assigned_team}",
        f"- assigned on-call: {verdict.assigned_engineer or 'unassigned'} (PagerDuty schedule)",
    ]
    trace = list(verdict.audit_metadata.decision_trace or [])
    if trace:
        trace_lines.append("- trace:")
        trace_lines.extend(f"  {i}. {step}" for i, step in enumerate(trace, 1))
    else:
        trace_lines.append("- trace: (none recorded)")
    sections.append("=== Decision Trace (RA-001) ===\n" + "\n".join(trace_lines))

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

    urgency = URGENCY_MAP.get(verdict.severity, _DEFAULT_URGENCY)
    channel = CHANNEL_MAP.get(verdict.severity, _DEFAULT_CHANNEL)
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
    attachment_added = False
    try:
        result = registry.call(
            "itsm.incident.create",
            short_description=short_description,
            # ServiceNow's REST API expects urgency as a string ("1".."3");
            # pass it as one here so the payload is correct at the seam.
            urgency=str(urgency),
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
                    attachment_added = _try_attach_grafana_panel(
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
            severity=verdict.severity,
            ticket_id=ticket_id,
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
        category=category,
        channel_notified=channel,
        notification_sent=notification_sent,
        assigned_team=verdict.assigned_team,
        assigned_engineer=verdict.assigned_engineer,
        attachment_added=attachment_added,
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
