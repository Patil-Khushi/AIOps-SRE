"""Incident Commander agent (RA-008, SRE) — coordinate Sev-1/Sev-2 response.

Entry point: ``command(alert: Alert, *, scenario_id=None) -> IncidentCommandResult``.

RA-008 closes the orchestration gap the retrospective flagged: it chains the
Reactive-Active flow and RCA into one coordinated incident response, on top of
the INFRA-2 orchestrator seam (issue #74).

Flow:

    1. run_reactive_flow(alert)   — RA-001 → RA-002 → [Correlate] → RA-003 → RA-005
    2. Correlate                  — RA-007 Log Correlation is NOT built yet, so
                                    this step is a traced placeholder (no fake
                                    evidence). Drop in the real call when RA-007
                                    ships; the seam position is reserved here.
    3. Severity gate              — coordination engages only for Sev-1/Sev-2
                                    (catalog: "coordinates Sev-1/2 incident
                                    response"). Lower severities return the
                                    reactive result with engaged=False.
    4. RCA (read-only)            — analyze() produces a verdict with ranked,
                                    HITL-gated fix steps. RA-008 never executes
                                    a fix: fix-step execution stays on the
                                    separately gated path (CLAUDE.md #3).
    5. Coordinate                 — scribe the timeline, post an IC context pack
                                    + human-IC handoff through the chatops seam,
                                    and assemble a facts-only postmortem seed.

Deferred (no seam exists yet): timed comms-cadence enforcement, status-page
sync. RA-008 v0 posts a single context-pack/handoff beat; those expand when
their adapters land.

Vendor-neutrality (CLAUDE.md #1): imports only ``aiops.runtime``,
``aiops.tools.chatops``, and sibling agents — no SDKs. HITL stays
platform-enforced: RA-008 takes no destructive action, so it holds no gate
logic itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agents.alert_triage import Alert
from agents.incident_commander.models import (
    ICAuditMetadata,
    IncidentCommandResult,
    PostmortemSeed,
    TimelineEntry,
)
from agents.rca_agent.agent import analyze as rca_analyze
from aiops.runtime.orchestrator import ReactiveFlowResult, run_reactive_flow
from aiops.tools.chatops import ChatMessage, Severity, get_client

logger = logging.getLogger(__name__)

# Catalog: RA-008 "coordinates Sev-1/2 incident response". Below this, the
# reactive pipeline already handled routing; the IC stays out of the way.
_COORDINATED_SEVERITIES = frozenset({"Sev-1", "Sev-2"})

# Where IC comms land. Matches RA-005's Sev-1 channel so the context pack lands
# in the same place the on-call page did.
_INCIDENTS_CHANNEL = "incidents"

# Triage severity → chatops loudness for the IC's own messages.
_SEV_TO_CHAT: dict[str, Severity] = {"Sev-1": Severity.P1, "Sev-2": Severity.P2}


def _entry(stage: str, detail: str) -> TimelineEntry:
    return TimelineEntry(ts=datetime.now(UTC), stage=stage, detail=detail)


def _context_pack_body(
    flow: ReactiveFlowResult,
    rca: Any,
) -> str:
    """Human-readable IC context pack — one ``key: value`` per line so every
    chatops renderer (Slack, dashboard, JSONL tail) lays it out the same way."""
    v = flow.verdict
    c = flow.classification
    top_fix = rca.ranked_fix_steps[0].description if rca.ranked_fix_steps else "none proposed"
    lines = [
        f"What failed: {v.alert_summary}",
        f"Service: {v.affected_service}",
        f"Severity: {v.severity}",
        f"Type: {c.incident_type}",
        f"Owning team: {v.assigned_team}",
        f"On-call: {v.assigned_engineer or 'unassigned'}",
        f"Ticket: {flow.ticket.ticket_id or 'not filed'}",
        f"Runbook: {v.recommended_runbook or 'none'}",
        f"Probable root cause: {rca.root_cause}",
        f"Top fix step (HITL-gated): {top_fix}",
        f"RCA confidence: {rca.confidence_score:.2f}",
        "Handoff: requesting a human Incident Commander to take ownership.",
    ]
    return "\n".join(lines)


def _emit_coordination(
    flow: ReactiveFlowResult,
    rca: Any,
    chat_sev: Severity,
) -> bool:
    """Post the IC context pack + human-IC handoff through the chatops seam.

    Returns ``True`` once the handoff request has been emitted. The chatops
    client fans out to whatever adapters are registered (JSONL audit log,
    WebSocket dashboard, Slack); with no adapters registered (tests/evals via
    ``emit_comms=False`` skip this entirely) ``send`` is a no-op.

    ``actions`` carries both intents so adapters can distinguish the IC beat
    from RA-005's routing: ``incident_command`` (this is the IC speaking) and
    ``handoff_human_ic`` (a human should take over).
    """
    msg = ChatMessage(
        channel=_INCIDENTS_CHANNEL,
        severity=chat_sev,
        title=f"Incident Commander engaged — {flow.verdict.affected_service} ({flow.verdict.severity})",
        body=_context_pack_body(flow, rca),
        incident_id=flow.verdict.incident_id,
        service=flow.verdict.affected_service,
        mentions=[flow.verdict.assigned_engineer] if flow.verdict.assigned_engineer else [],
        actions=["incident_command", "handoff_human_ic", "post_to_chat"],
    )
    get_client().send(msg)
    return True


def _postmortem_seed(
    flow: ReactiveFlowResult,
    rca: Any,
    timeline: list[TimelineEntry],
) -> PostmortemSeed:
    """Pre-fill a postmortem skeleton with the facts already gathered. The
    contributing-signals list is RA-001's decision trace — the evidence the
    severity + ownership calls were based on."""
    v = flow.verdict
    return PostmortemSeed(
        affected_service=v.affected_service,
        severity=v.severity,
        incident_summary=v.alert_summary,
        incident_type=flow.classification.incident_type,
        ticket_id=flow.ticket.ticket_id,
        root_cause=rca.root_cause,
        confidence_score=rca.confidence_score,
        ranked_fix_steps=[s.model_dump(mode="json") for s in rca.ranked_fix_steps],
        contributing_signals=list(v.audit_metadata.decision_trace),
        timeline=list(timeline),
    )


def command(
    alert: Alert,
    *,
    scenario_id: str | None = None,
    emit_comms: bool = True,
) -> IncidentCommandResult:
    """Coordinate incident response for one alert.

    Runs the full Reactive-Active flow, then — only for Sev-1/Sev-2 — runs RCA
    (read-only), posts IC comms, seeds the postmortem, and requests a human-IC
    handoff. ``emit_comms=False`` suppresses the chatops emit so the eval
    harness and pure tests don't write to the audit log.
    """
    trace: list[str] = []
    timeline: list[TimelineEntry] = []
    started_at = datetime.now(UTC)

    # Step 1 — reactive flow (the #74 seam).
    flow = run_reactive_flow(alert)
    verdict = flow.verdict
    severity = verdict.severity
    timeline.append(
        _entry("triage", f"RA-001 severity={severity}, service={verdict.affected_service}")
    )
    timeline.append(_entry("classify", f"RA-002 type={flow.classification.incident_type}"))

    # Step 2 — correlate placeholder (RA-007 not built).
    timeline.append(
        _entry("correlate", "RA-007 Log Correlation not yet implemented; correlation step skipped")
    )
    trace.append("correlation pending — RA-007 Log Correlation agent not yet built")

    timeline.append(
        _entry(
            "ticket",
            f"RA-003 ticket={flow.ticket.ticket_id or 'none'}, created={flow.ticket.created}",
        )
    )
    timeline.append(
        _entry("notify", f"RA-005 channel={flow.routing.channel if flow.routing else 'none'}")
    )

    # Step 3 — severity gate.
    if severity not in _COORDINATED_SEVERITIES:
        trace.append(f"severity {severity} below Sev-2 threshold — IC not engaged")
        logger.info("RA-008: not engaging for %s on %s", severity, verdict.affected_service)
        return IncidentCommandResult(
            engaged=False,
            severity=severity,
            affected_service=verdict.affected_service,
            reactive=flow.to_api_dict(),
            rca=None,
            timeline=timeline,
            postmortem_seed=None,
            handoff_requested=False,
            audit_metadata=ICAuditMetadata(created_at=started_at, decision_trace=trace),
        )

    trace.append(f"severity {severity} — IC engaged for Sev-1/2 coordination")

    # Step 4 — RCA (read-only; fix-step execution stays separately HITL-gated).
    rca = rca_analyze(verdict.model_dump(mode="json"), scenario_id=scenario_id)
    timeline.append(
        _entry(
            "rca",
            f"PRS-008 confidence={rca.confidence_score:.2f}, "
            f"{len(rca.ranked_fix_steps)} ranked fix step(s)",
        )
    )

    # Step 5 — coordinate: comms + handoff + postmortem seed.
    handoff_requested = False
    if emit_comms:
        handoff_requested = _emit_coordination(flow, rca, _SEV_TO_CHAT[severity])
        timeline.append(_entry("comms", f"posted IC context pack to #{_INCIDENTS_CHANNEL}"))
        timeline.append(_entry("handoff", "requested human-IC handoff via chatops"))
        trace.append("posted IC context pack + human-IC handoff through chatops seam")
    else:
        # Coordination decided but comms suppressed (eval / pure-call path).
        handoff_requested = True
        timeline.append(_entry("handoff", "human-IC handoff requested (comms suppressed)"))
        trace.append("comms suppressed (emit_comms=False); handoff recorded without chatops emit")

    seed = _postmortem_seed(flow, rca, timeline)
    trace.append("assembled facts-only postmortem seed")

    return IncidentCommandResult(
        engaged=True,
        severity=severity,
        affected_service=verdict.affected_service,
        reactive=flow.to_api_dict(),
        rca=rca.model_dump(mode="json"),
        timeline=timeline,
        postmortem_seed=seed,
        handoff_requested=handoff_requested,
        audit_metadata=ICAuditMetadata(created_at=started_at, decision_trace=trace),
    )


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out.

    Accepts ``{"alert": {<Alert payload>}, "scenario_id"?: str}`` (or a bare
    Alert payload). Comms are suppressed so evals don't write to the chatops
    audit log."""
    raw_alert = input.get("alert", input)
    alert = Alert(**raw_alert)
    scenario_id = input.get("scenario_id")
    result = command(alert, scenario_id=scenario_id, emit_comms=False)
    return result.model_dump(mode="json")


def reset_state() -> None:
    """Eval-harness hook. RA-008 holds no state of its own, but it drives RA-001
    (dedup + idempotency persistence) and RA-002 (historical-incident store) via
    the orchestrator. Cascade their resets so each eval case starts clean —
    otherwise a repeated alert_id would be idempotency-suppressed across cases."""
    from agents.alert_triage.agent import reset_state as _triage_reset
    from agents.incident_classifier.agent import reset_state as _classify_reset

    _triage_reset()
    _classify_reset()
