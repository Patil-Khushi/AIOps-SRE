"""Derive structured ``Evidence`` from raw correlated signals.

Kept separate from ``agent.py`` for one practical reason: ``correlate()`` is
already a six-stage pipeline, and evidence assembly is a pure transformation over
its outputs (signals + topology + the rules' conclusions). Isolating it here
means it can be tested exhaustively without running the pipeline, and adding an
evidence dimension later does not mean editing the orchestration.

Per-evidence confidence
-----------------------
``CorrelationResult.confidence`` scores the whole verdict. That is too coarse for
a consumer deciding *which* finding to act on: in a pack where one signature
appears across logs, traces and metrics and another appears once at INFO, both
would inherit the same number. So each piece of evidence is scored on its own
merits — severity, cross-source agreement, occurrence count, and topology
proximity — with the aggregate verdict score left exactly as it was.
"""

from __future__ import annotations

from collections import defaultdict

from agents.log_correlation.evidence import (
    Evidence,
    SignalType,
    SupportingTelemetry,
    TopologyContext,
    _digest,
    make_correlation_id,
)
from agents.log_correlation.models import CorrelatedSignal, CorrelationInput

# Deliberately narrower than ``agent._ERROR_SEVERITIES``, which also counts
# warn/warning. That set answers "is anything concerning here?" — used for
# first-error selection and spike counting, where a warning burst is a real signal.
# This one answers "is this an error or a warning?", and classifying a warning as
# ``error_log`` would mislabel every finding built from it.
#
# Named distinctly on purpose: the two sets previously shared the name
# ``_ERROR_SEVERITIES`` across the two modules with different membership, which
# reads like a copy of one constant and invites someone to "unify" them.
_STRICT_ERROR_SEVERITIES = {"error", "critical", "fatal"}
_WARN_SEVERITIES = {"warn", "warning"}


def _classify(signal: CorrelatedSignal) -> SignalType:
    """Map (source, severity, signature) onto a signal type.

    ``source`` says where an observation came from; the type says what it *is*. A
    slow span and an error span both arrive from ``traces`` and a consumer
    filtering for real failures needs them apart.
    """
    sev = (signal.severity or "").lower()
    if signal.source == "logs":
        if sev in _STRICT_ERROR_SEVERITIES:
            return "error_log"
        if sev in _WARN_SEVERITIES:
            return "warning_log"
        return "log_line"
    if signal.source == "traces":
        if sev in _STRICT_ERROR_SEVERITIES:
            return "error_span"
        # The agent marks a span "error" purely on duration, so anything
        # non-error carrying latency wording is a slow span rather than a failure.
        if "ms" in signal.signature or sev in _WARN_SEVERITIES:
            return "slow_span"
        return "trace_summary"
    if sev in _STRICT_ERROR_SEVERITIES or sev in _WARN_SEVERITIES:
        return "metric_anomaly"
    return "metric_sample"


def _evidence_confidence(
    *,
    severity: str,
    sources_agreeing: int,
    occurrences: int,
    topology_depth: int | None,
) -> float:
    """Score one finding on its own merits.

    Weighting rationale:

    - **Cross-source agreement dominates.** The same signature seen in logs *and*
      traces is far stronger than either alone; it is also the strongest rule the
      rest of the agent uses, so the per-item score should agree with it.
    - **Error severity** matters, but less than agreement — a single ERROR line is
      weaker evidence than a corroborated pattern.
    - **Repetition** counts a little: a recurring signature is unlikely to be
      noise, but volume alone is not proof.
    - **Topology proximity** counts a little: a fault in a direct dependency is
      more likely relevant to the incident than one four hops away.

    Capped below 1.0 — evidence derived from heuristics should never claim
    certainty.
    """
    score = 0.3
    sev = (severity or "").lower()
    if sev in _STRICT_ERROR_SEVERITIES:
        score += 0.2
    elif sev in _WARN_SEVERITIES:
        score += 0.1

    if sources_agreeing >= 3:
        score += 0.3
    elif sources_agreeing == 2:
        score += 0.2

    if occurrences >= 5:
        score += 0.1
    elif occurrences >= 2:
        score += 0.05

    if topology_depth == 0:
        score += 0.05
    elif topology_depth == 1:
        score += 0.1

    return round(min(score, 0.95), 3)


def _topology_context(
    signature: str,
    sample: str,
    incident_service: str,
    dependencies: list[str],
) -> TopologyContext:
    """Infer which service this evidence implicates, and its relation to the root.

    RA-007 only ever queries one service, so every signal *originates* from the
    incident service — a topology context computed from origin alone would always
    say ``self`` and carry no information. What is actually informative is the
    service the evidence *points at*: a checkout log line reading "payment charge
    error" is evidence about payment.

    Inference is by matching known dependency names in the signature and sample
    text, the same mechanism ``_suspects_from_topology`` already uses to derive
    suspects — so evidence and the verdict's suspect list agree by construction
    instead of drifting apart.

    Nothing is guessed: only names from the resolved dependency list can be
    implicated. With no topology available the relation stays ``unknown`` rather
    than defaulting to ``self``, because "we could not place this" and "this is
    the service itself" are different claims.
    """
    incident = incident_service.strip().lower()
    deps = [d.strip().lower() for d in dependencies if d and d.strip()]
    if not deps:
        return TopologyContext(relation="unknown", implicated_service=None, depth=None)

    blob = f"{signature} {sample}".lower()
    implicated = next((d for d in deps if d in blob), None)
    if implicated is not None and implicated != incident:
        return TopologyContext(
            relation="dependency",
            implicated_service=implicated,
            depth=1,
            path=[incident, implicated],
        )
    # Topology was known and no dependency is named, so the evidence is about the
    # queried service itself — a service-internal fault.
    return TopologyContext(relation="self", implicated_service=incident, depth=0, path=[incident])


def build_evidence(
    payload: CorrelationInput,
    signals: list[CorrelatedSignal],
    dependencies: list[str],
) -> list[Evidence]:
    """Turn correlated signals into structured, immutable evidence.

    One ``Evidence`` per (signature, source) pair rather than per raw signal:
    fifty identical log lines are one finding observed fifty times, and emitting
    fifty near-identical objects would bury the distinct findings a responder
    needs to see. The collapsed count is preserved in
    ``supporting_telemetry.occurrences``.

    Ordering is deterministic (strongest first, then by signature) so the same
    input always yields the same list — required for the eval harness and for
    comparing one verdict against another.
    """
    if not signals:
        return []

    correlation_id = make_correlation_id(
        payload.service, payload.window.start.isoformat(), payload.window.end.isoformat()
    )

    # Which sources carry each signature — computed across the whole set first,
    # because a per-group view cannot see cross-source agreement.
    sources_by_signature: dict[str, set[str]] = defaultdict(set)
    for sig in signals:
        sources_by_signature[sig.signature].add(sig.source)

    grouped: dict[tuple[str, str], list[CorrelatedSignal]] = defaultdict(list)
    for sig in signals:
        grouped[(sig.signature, sig.source)].append(sig)

    evidence: list[Evidence] = []
    incident_service = payload.service
    for (signature, source), members in grouped.items():
        ordered = sorted(members, key=lambda s: s.timestamp)
        first, last = ordered[0], ordered[-1]
        # Strongest severity in the group wins: a signature that ever produced an
        # error should not be downgraded because later occurrences were INFO.
        severity = next(
            (s.severity for s in ordered if (s.severity or "").lower() in _STRICT_ERROR_SEVERITIES),
            next(
                (s.severity for s in ordered if (s.severity or "").lower() in _WARN_SEVERITIES),
                first.severity,
            ),
        )
        agreeing = sorted(sources_by_signature[signature])
        topology = _topology_context(signature, first.sample, incident_service, dependencies)
        confidence = _evidence_confidence(
            severity=severity,
            sources_agreeing=len(agreeing),
            occurrences=len(ordered),
            topology_depth=topology.depth,
        )
        evidence.append(
            Evidence(
                evidence_id=_digest(correlation_id, source, signature),
                correlation_id=correlation_id,
                timestamp=first.timestamp,
                source=source,  # type: ignore[arg-type]
                service=incident_service,
                signal_type=_classify(first),
                normalized_signature=signature,
                severity=severity,
                confidence=confidence,
                supporting_telemetry=SupportingTelemetry(
                    sample=first.sample,
                    occurrences=len(ordered),
                    sources_agreeing=agreeing,
                    first_seen=first.timestamp,
                    last_seen=last.timestamp,
                ),
                topology_context=topology,
            )
        )

    evidence.sort(key=lambda e: (-e.confidence, e.normalized_signature, e.source))
    return evidence
