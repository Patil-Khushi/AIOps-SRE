"""Incident Commander agent (RA-008, SRE) — coordinate Sev-1/Sev-2 response.

Entry point: ``command(alert: Alert, *, scenario_id=None) -> IncidentCommandResult``.

RA-008 closes the orchestration gap the retrospective flagged: it chains the
Reactive-Active flow and RCA into one coordinated incident response, on top of
the INFRA-2 orchestrator seam (issue #74).

Flow:

    1. run_reactive_flow(alert)   — RA-001 → RA-002 → RA-003 → RA-005
    2. Severity gate              — coordination engages only for Sev-1/Sev-2
                                    (catalog: "coordinates Sev-1/2 incident
                                    response"). Lower severities return the
                                    reactive result with engaged=False.
    3. Correlate (RA-007)         — Log Correlation pulls logs/traces/metrics for
                                    the incident window and emits a correlated
                                    evidence pack + suspect components. Read-only;
                                    a failure is non-fatal (RCA still runs without
                                    it). Engaged path only — RCA is its sole
                                    consumer here (catalog chain RA-003 → RA-007 →
                                    RCA).
    4. RCA (read-only)            — analyze() folds in RA-007's evidence and
                                    produces a verdict with ranked, HITL-gated fix
                                    steps. RA-008 never executes a fix: fix-step
                                    execution stays on the separately gated path
                                    (CLAUDE.md #3).
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
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.alert_triage import Alert
from agents.incident_commander.models import (
    ICAuditMetadata,
    IncidentCommandResult,
    IncidentMetrics,
    PostmortemSeed,
    TimelineEntry,
)
from agents.log_correlation import CorrelationInput, TimeWindow, correlate
from agents.rca_agent.agent import analyze as rca_analyze
from aiops.runtime.orchestrator import ReactiveFlowResult, run_reactive_flow
from aiops.tools.chatops import ChatMessage, DeliveryResult, Severity, get_client

logger = logging.getLogger(__name__)

# Catalog: RA-008 "coordinates Sev-1/2 incident response". Below this, the
# reactive pipeline already handled routing; the IC stays out of the way.
_COORDINATED_SEVERITIES = frozenset({"Sev-1", "Sev-2"})

# Where IC comms land. Matches RA-005's Sev-1 channel so the context pack lands
# in the same place the on-call page did.
_INCIDENTS_CHANNEL = "incidents"

# Triage severity → chatops loudness for the IC's own messages.
_SEV_TO_CHAT: dict[str, Severity] = {"Sev-1": Severity.P1, "Sev-2": Severity.P2}

# RA-007 scopes its log/trace/metric pull to the window ending at the alert
# time; 15 min captures the incident lead-up without dragging in unrelated
# history. Incidents that build up over longer (e.g. a slow payment leak) would
# have evidence cut off, so the window is tunable per environment via
# AIOPS_IC_CORRELATION_LOOKBACK_MINUTES without a code change.
_DEFAULT_CORRELATION_LOOKBACK_MINUTES = 15.0


def _correlation_lookback() -> timedelta:
    """Evidence-window lookback for RA-007, from
    ``AIOPS_IC_CORRELATION_LOOKBACK_MINUTES`` (minutes). Falls back to the
    default on an unset, non-numeric, or non-positive value."""
    raw = os.environ.get("AIOPS_IC_CORRELATION_LOOKBACK_MINUTES", "").strip()
    try:
        minutes = float(raw)
    except ValueError:
        minutes = _DEFAULT_CORRELATION_LOOKBACK_MINUTES
    if minutes <= 0:
        minutes = _DEFAULT_CORRELATION_LOOKBACK_MINUTES
    return timedelta(minutes=minutes)


def _build_shared_evidence_context(
    *,
    service: str,
    severity: str,
    window_start: datetime,
    window_end: datetime,
    alert_id: str,
    alert_name: str,
) -> dict[str, Any] | None:
    """Collect once, for both RA-007 and PRS-008, instead of each agent
    fetching its own metrics/logs/traces for the same service and window.

    Returns ``None`` when the Context Engineering Layer is off — matches
    ``aiops.runtime.orchestrator._build_shared_context``'s reasoning exactly:
    building a context nobody will read is wasted work, and both ``correlate()``
    and ``rca_analyze()`` already treat ``context=None`` as "fetch your own
    evidence". Never raises — a build failure costs evidence, not the RCA.
    """
    from aiops.context import config as context_config

    if context_config.context_mode() == "off":
        return None

    try:
        from agents.log_correlation.context_adapter import (
            build_context_request_specs as log_correlation_specs,
        )
        from agents.log_correlation.models import CorrelationInput
        from agents.log_correlation.models import TimeWindow as _Window
        from agents.rca_agent.context_adapter import (
            build_context_request_specs as rca_specs,
        )
        from aiops.context.builder import ContextBuilder, ContextRequest

        correlation_payload = CorrelationInput(
            service=service, window=_Window(start=window_start, end=window_end)
        )
        specs = [
            *rca_specs(service, window_start=window_start, window_end=window_end),
            *log_correlation_specs(correlation_payload),
        ]
        request = ContextRequest(
            service=service,
            window_start=window_start,
            window_end=window_end,
            specs=specs,
            severity=severity,
            alert_id=alert_id,
            alert_name=alert_name,
        )
        return ContextBuilder().build(request).model_dump(mode="json")
    except Exception:
        logger.exception(
            "RA-008: shared evidence context build failed for %s; RA-007/PRS-008 will fetch live",
            service,
        )
        return None


def _entry(stage: str, detail: str, ts: datetime) -> TimelineEntry:
    """One timeline line stamped with the *real* time the stage happened.

    Callers pass the event's own timestamp (e.g. the triage verdict's
    ``created_at``, RA-005's ``decided_at``, or ``now()`` for a step the IC runs
    itself) rather than letting every entry collapse to the reconstruction time.
    """
    return TimelineEntry(ts=ts, stage=stage, detail=detail)


def _elapsed(start: datetime, end: datetime) -> float:
    """Seconds from ``start`` to ``end``, clamped at 0. Agents stamp their own
    events off independent clocks, so a tiny backwards skew is possible; a
    postmortem must never show a negative duration."""
    return max(0.0, (end - start).total_seconds())


def _compute_metrics(detected_at: datetime, timeline: list[TimelineEntry]) -> IncidentMetrics:
    """Derive MTTA/MTTR-style durations from the scribed timeline, all measured
    from detection (``detected_at`` = T0). Reads each stage's real ``ts`` so the
    numbers match what the timeline shows. A stage that did not run is ``None``;
    ``total`` spans detection to the last recorded beat.

    Only stages with a timestamp of their *own* are surfaced. Ticket creation
    (RA-003) records no timestamp — its timeline beat borrows the classify time —
    so there is deliberately no ``time_to_ticket`` metric: it would measure
    classify completion under a ticket label.
    """
    by_stage = {e.stage: e for e in timeline}

    def since(stage: str) -> float | None:
        entry = by_stage.get(stage)
        return _elapsed(detected_at, entry.ts) if entry else None

    total = _elapsed(detected_at, max(e.ts for e in timeline)) if timeline else None
    return IncidentMetrics(
        detected_at=detected_at,
        time_to_triage_seconds=since("triage"),
        time_to_notify_seconds=since("notify"),
        time_to_handoff_seconds=since("handoff"),
        total_coordination_seconds=total,
    )


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
) -> dict[str, DeliveryResult]:
    """Post the IC context pack + human-IC handoff through the chatops seam.

    Returns the per-adapter delivery results so the caller can record what
    actually shipped (and surface failures) instead of assuming success. The
    chatops client fans out to whatever adapters are registered (JSONL audit
    log, WebSocket dashboard, Slack) and never raises — a failing adapter is
    captured as ``ok=False`` in its ``DeliveryResult``. With no adapters
    registered the returned dict is empty.

    ``actions`` carries both intents so adapters can distinguish the IC beat
    from RA-005's routing: ``incident_command`` (this is the IC speaking) and
    ``handoff_human_ic`` (a human should take over).
    """
    msg = ChatMessage(
        channel=_INCIDENTS_CHANNEL,
        severity=chat_sev,
        title=f"Incident Commander engaged — {flow.verdict.affected_service} ({flow.verdict.severity})",
        body=_context_pack_body(flow, rca),
        # The incident handle is the filed ticket id (e.g. INC-42). The verdict's
        # own incident_id is never back-populated upstream, so use it only as a
        # defensive fallback; adapters render this as the structured incident ref.
        incident_id=flow.ticket.ticket_id or flow.verdict.incident_id,
        service=flow.verdict.affected_service,
        mentions=[flow.verdict.assigned_engineer] if flow.verdict.assigned_engineer else [],
        actions=["incident_command", "handoff_human_ic", "post_to_chat"],
    )
    return get_client().send(msg)


def _postmortem_seed(
    flow: ReactiveFlowResult,
    rca: Any,
    timeline: list[TimelineEntry],
    metrics: IncidentMetrics,
) -> PostmortemSeed:
    """Pre-fill a postmortem skeleton with the facts already gathered. The
    contributing-signals list is RA-001's decision trace — the evidence the
    severity + ownership calls were based on. ``metrics`` carries the derived
    MTTA/MTTR durations so the seed is self-contained."""
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
        metrics=metrics,
    )


def command(
    alert: Alert,
    *,
    scenario_id: str | None = None,
    emit_comms: bool = True,
) -> IncidentCommandResult:
    """Coordinate incident response for one alert.

    Runs the full Reactive-Active flow, then — only for Sev-1/Sev-2 — runs RA-007
    Log Correlation, feeds its evidence pack into RCA (read-only), posts IC
    comms, seeds the postmortem, and requests a human-IC handoff.
    ``emit_comms=False`` suppresses the chatops emit so the eval harness and pure
    tests don't write to the audit log.
    """
    trace: list[str] = []
    timeline: list[TimelineEntry] = []
    started_at = datetime.now(UTC)

    # Step 1 — reactive flow (the #74 seam).
    flow = run_reactive_flow(alert)
    flow_done_at = datetime.now(UTC)
    verdict = flow.verdict
    severity = verdict.severity

    # Real event times, not the moment we reconstruct the timeline. Triage and
    # classify record their own ``created_at``; RA-005 records ``decided_at``.
    # RA-003 records no timestamp of its own, so the ticket entry inherits the
    # nearest real upstream time (classify) to stay ordered.
    triage_ts = getattr(verdict.audit_metadata, "created_at", None) or flow_done_at
    classify_ts = getattr(flow.classification.audit_metadata, "created_at", None) or flow_done_at
    notify_ts = flow.routing.decided_at if flow.routing else flow_done_at

    # T0 — the incident began when the alert fired, not when the IC acted. This
    # anchors the timeline and is the baseline every derived metric measures from
    # (cheat-sheet MTTD). alert.timestamp is the source's own fire time.
    timeline.append(
        _entry(
            "detected",
            f"alert {alert.metric}={alert.value} on {alert.service} fired",
            alert.timestamp,
        )
    )
    timeline.append(
        _entry(
            "triage",
            f"RA-001 severity={severity}, service={verdict.affected_service}",
            triage_ts,
        )
    )
    timeline.append(
        _entry("classify", f"RA-002 type={flow.classification.incident_type}", classify_ts)
    )
    timeline.append(
        _entry(
            "ticket",
            f"RA-003 ticket={flow.ticket.ticket_id or 'none'} "
            f"({'opened' if flow.ticket.created else 'updated'})",
            classify_ts,
        )
    )
    timeline.append(
        _entry(
            "notify",
            f"RA-005 channel={flow.routing.channel if flow.routing else 'none'}",
            notify_ts,
        )
    )

    # Step 2 — severity gate.
    if severity not in _COORDINATED_SEVERITIES:
        trace.append(f"severity {severity} below Sev-2 threshold — IC not engaged")
        logger.info("RA-008: not engaging for %s on %s", severity, verdict.affected_service)
        timeline.sort(key=lambda e: e.ts)
        return IncidentCommandResult(
            engaged=False,
            severity=severity,
            affected_service=verdict.affected_service,
            reactive=flow.to_api_dict(),
            rca=None,
            timeline=timeline,
            metrics=_compute_metrics(alert.timestamp, timeline),
            postmortem_seed=None,
            handoff_requested=False,
            audit_metadata=ICAuditMetadata(created_at=started_at, decision_trace=trace),
        )

    trace.append(f"severity {severity} — IC engaged for Sev-1/2 coordination")

    # Built once, before RA-007, so RA-007 and PRS-008 below both reason from
    # the SAME collected evidence for this incident instead of each
    # independently querying Prometheus/Loki/Jaeger. A no-op (returns None)
    # unless AIOPS_CONTEXT_LAYER is on; both correlate() and rca_analyze()
    # already treat context=None as "fetch your own evidence, exactly as
    # before" — engaged path only, since correlation/RCA are Sev-1/2-only here.
    window_start = alert.timestamp - _correlation_lookback()
    shared_context = _build_shared_evidence_context(
        service=verdict.affected_service,
        severity=severity,
        window_start=window_start,
        window_end=alert.timestamp,
        alert_id=alert.alert_id,
        alert_name=alert.metric,
    )
    # Passed only when there is something to pass — correlate()/rca_analyze()
    # already default context to None, so omitting the kwarg when
    # AIOPS_CONTEXT_LAYER is off (the common case) is behaviourally identical
    # AND keeps both calls compatible with any caller — including test stubs —
    # written against the pre-migration signature.
    context_kwargs: dict[str, Any] = (
        {"context": shared_context} if shared_context is not None else {}
    )

    # Step 3 — correlate (RA-007): pull the evidence pack that feeds RCA. Engaged
    # path only, since RCA is its sole consumer here. Read-only; a failure is
    # non-fatal — RCA's correlation arg is additive, so it simply runs without
    # the evidence. RA-007 owns its own synthetic fallback when the observability
    # backends are unreachable (CI / offline demo), so this stays meaningful.
    correlation: Any = None
    try:
        correlation = correlate(
            CorrelationInput(
                service=verdict.affected_service,
                window=TimeWindow(start=window_start, end=alert.timestamp),
                triage_verdict=verdict.model_dump(mode="json"),
                classification=flow.classification.model_dump(mode="json"),
            ),
            **context_kwargs,
        )
    except Exception:
        logger.exception(
            "RA-008: RA-007 correlation failed for %s; RCA will run without evidence",
            verdict.affected_service,
        )
    correlate_ts = datetime.now(UTC)
    correlation_dict = correlation.model_dump(mode="json") if correlation is not None else None
    if correlation is not None:
        timeline.append(
            _entry(
                "correlate",
                f"RA-007 {len(correlation.timeline)} signal(s), "
                f"suspects={correlation.suspected_dependencies or 'none'}, "
                f"confidence={correlation.confidence:.2f}",
                correlate_ts,
            )
        )
        trace.append(
            f"RA-007 correlated {len(correlation.timeline)} signal(s); "
            f"suspects={correlation.suspected_dependencies or 'none'}, "
            f"provenance={correlation.audit_metadata.signal_source}"
        )
    else:
        timeline.append(
            _entry(
                "correlate",
                "RA-007 correlation unavailable; RCA proceeding without it",
                correlate_ts,
            )
        )
        trace.append("RA-007 correlation failed; RCA proceeds without correlation evidence")

    # Step 4 — RCA (read-only; fix-step execution stays separately HITL-gated).
    rca = rca_analyze(
        verdict.model_dump(mode="json"),
        scenario_id=scenario_id,
        correlation=correlation_dict,
        **context_kwargs,
    )
    rca_ts = datetime.now(UTC)
    timeline.append(
        _entry(
            "rca",
            f"PRS-008 confidence={rca.confidence_score:.2f}, "
            f"{len(rca.ranked_fix_steps)} ranked fix step(s)",
            rca_ts,
        )
    )

    # Step 5 — coordinate: comms + handoff + postmortem seed.
    handoff_requested = False
    if emit_comms:
        deliveries = _emit_coordination(flow, rca, _SEV_TO_CHAT[severity])
        emit_ts = datetime.now(UTC)
        delivered = [name for name, r in deliveries.items() if r.ok]
        failed = [f"{name}: {r.error}" for name, r in deliveries.items() if not r.ok]
        total = len(deliveries)
        if delivered:
            # At least one sink accepted the handoff — it reached a human surface.
            handoff_requested = True
            timeline.append(
                _entry(
                    "comms",
                    f"posted IC context pack to #{_INCIDENTS_CHANNEL} "
                    f"({len(delivered)}/{total} sink(s))",
                    emit_ts,
                )
            )
            timeline.append(_entry("handoff", "requested human-IC handoff via chatops", emit_ts))
            trace.append(
                f"posted IC context pack + human-IC handoff through chatops seam "
                f"({len(delivered)}/{total} adapter(s) delivered)"
            )
            if failed:
                trace.append("WARNING: some chatops adapters failed — " + "; ".join(failed))
        else:
            # Nothing shipped — every adapter failed, or none were registered.
            # Don't claim a handoff we could not deliver to anyone.
            handoff_requested = False
            detail = (
                f"chatops delivery failed on all {total} sink(s); handoff not delivered"
                if total
                else "no chatops sinks registered; IC context pack not delivered"
            )
            timeline.append(_entry("comms", detail, emit_ts))
            trace.append(
                "WARNING: human-IC handoff NOT delivered — "
                + ("; ".join(failed) if failed else "no chatops adapters registered")
            )
    else:
        # Coordination decided but comms suppressed (eval / pure-call path).
        handoff_requested = True
        timeline.append(
            _entry("handoff", "human-IC handoff requested (comms suppressed)", datetime.now(UTC))
        )
        trace.append("comms suppressed (emit_comms=False); handoff recorded without chatops emit")

    # Chronological order regardless of which clock stamped each beat, then
    # derive the response metrics off the finalized timeline so they match it.
    timeline.sort(key=lambda e: e.ts)
    metrics = _compute_metrics(alert.timestamp, timeline)

    seed = _postmortem_seed(flow, rca, timeline, metrics)
    trace.append("assembled facts-only postmortem seed")

    return IncidentCommandResult(
        engaged=True,
        severity=severity,
        affected_service=verdict.affected_service,
        reactive=flow.to_api_dict(),
        rca=rca.model_dump(mode="json"),
        timeline=timeline,
        metrics=metrics,
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
    # Alert Triage's reset_state now cascades the classification-step reset
    # (historical-incident store + embedding cache), since the one agent owns
    # both halves — so a single call clears everything RA-008 drives.
    from agents.alert_triage.agent import reset_state as _triage_reset

    _triage_reset()
