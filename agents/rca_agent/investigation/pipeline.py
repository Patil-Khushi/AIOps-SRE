"""The deterministic investigation: scope → timeline → baseline → completeness →
hypotheses → evidence matrix → scoring.

Pure Python, no LLM, no new retrieval. Everything here reads facts already collected
(``facts.collect_facts`` over the existing ``evidence.Backend``) plus the change records
and Context Pack the agent already had, and turns them into an :class:`Investigation`.

What this buys, concretely: the root cause is now *chosen by the platform* from a scored
candidate set, and the confidence number is derived from classified evidence. The LLM's
job moves to explaining that result — which is the difference between a verdict you can
audit and a verdict you can only read.

Honest limits, stated where they are made rather than hidden
-----------------------------------------------------------
* **The timeline is sparse.** RCA's telemetry comes from *instant* PromQL queries, which
  carry no history — a gauge reading 0 says nothing about when it became 0. So timeline
  events are built only from things that genuinely have a timestamp: commits, Context
  Pack observations, and the alert itself. Nothing is stamped with "now" to look fuller
  than it is, and ``coverage_note`` says so.
* **Baselines are static, not learned.** There is no historical-metric source in this
  repo, so "normal" is taken from alert thresholds and container limits — real expected
  operating ranges, but not a learned per-service baseline. Reported as ``PARTIAL``
  precisely so nobody reads it as the latter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agents.rca_agent.investigation import catalog, impact, memory, recovery, scoring
from agents.rca_agent.investigation.facts import Availability, ObservedFacts
from agents.rca_agent.investigation.models import (
    BaselineComparison,
    BaselineStatus,
    EvidenceItem,
    EvidenceMatrix,
    EvidenceStance,
    Hypothesis,
    IncidentScope,
    IncidentTimelineView,
    Investigation,
    InvestigationBudget,
    InvestigationCompleteness,
    RcaTimelineEvent,
    RootCauseStatus,
    TemporalRelation,
)
from aiops.context.models import digest

logger = logging.getLogger(__name__)

# Fact category -> how to tell whether it carried anything. Drives both the
# completeness report and the CHECKED_ABSENT / UNAVAILABLE split, so the two cannot
# disagree about what was observed.
_CATEGORY_PRESENT = {
    catalog.NEED_GAUGES: lambda f: bool(f.gauges),
    catalog.NEED_ERRORS: lambda f: bool(f.error_rates),
    catalog.NEED_LATENCY: lambda f: bool(f.latencies),
    catalog.NEED_RESOURCES: lambda f: bool(f.resources),
    catalog.NEED_LIFECYCLE: lambda f: bool(f.lifecycles),
}

_SYMPTOM_RULES: tuple[tuple[str, Any], ...] = (
    ("requests are failing with errors", lambda f: bool(f.error_rates)),
    ("the service is restarting or has terminated", lambda f: bool(f.lifecycles)),
    (
        "requests are slower than their threshold",
        lambda f: any(latency.breaches_threshold for latency in f.latencies),
    ),
    ("a backing datastore is unreachable", lambda f: bool(f.unreachable_stores)),
    (
        "the container is resource-constrained",
        lambda f: bool(f.saturated_cpu or f.pressured_memory),
    ),
)


def _evidence_id(hypothesis_id: str, statement: str) -> str:
    """Deterministic id for one evidence item.

    Uses the platform's ``digest`` so two runs over the same incident produce the same
    ids and a verdict can be *compared* with its predecessor rather than merely replacing
    it — the same reasoning ``make_observation_id`` documents.
    """
    return digest("rca-ev", hypothesis_id, statement)


# ─── stage 1: scope ─────────────────────────────────────────────────────────


def build_scope(
    triage: dict[str, Any],
    facts: ObservedFacts,
    *,
    context: dict[str, Any] | None = None,
) -> IncidentScope:
    """Establish what is being investigated, before any cause is proposed.

    The ``user_visible_symptom`` is derived from *observed facts*, not from the alert
    summary. That is the point of the stage: the alert summary is the claim under
    investigation, and copying it into the symptom field would reintroduce the
    "root cause: HTTP 500" collapse this stage exists to prevent.
    """
    audit = triage.get("audit_metadata") or {}
    sources = audit.get("source_alerts") if isinstance(audit, dict) else None
    incident_id = (
        str((sources or ["unknown"])[0]) if isinstance(sources, list) and sources else "unknown"
    )

    identity = ((context or {}).get("incident") or {}) if isinstance(context, dict) else {}
    alert_name = str(identity.get("alert_name") or "") or None
    summary = str(triage.get("alert_summary") or "") or None
    if not alert_name and summary and " firing:" in summary:
        alert_name = summary.split(" firing:")[0].strip() or None
    if not alert_name and facts.alerts:
        alert_name = facts.alerts[0].name

    symptoms = [text for text, predicate in _SYMPTOM_RULES if predicate(facts)]
    symptom = "; ".join(symptoms) if symptoms else "no symptom is currently observable"

    onset: datetime | None = None
    raw_onset = identity.get("window_start") or (
        audit.get("created_at") if isinstance(audit, dict) else None
    )
    if raw_onset:
        onset = _parse_time(raw_onset)

    return IncidentScope(
        incident_id=incident_id,
        affected_service=str(triage.get("affected_service") or "unknown"),
        severity=str(triage.get("severity") or "unknown"),
        user_visible_symptom=symptom,
        alert_name=alert_name,
        alert_summary=summary,
        onset_at=onset,
        correlation_id=str(identity.get("correlation_id") or "") or None,
        initial_blast_radius=_initial_blast_radius(
            context, str(triage.get("affected_service") or "unknown")
        ),
    )


def _initial_blast_radius(context: dict[str, Any] | None, service: str) -> tuple[str, ...]:
    """Direct dependencies for ``service`` — a starting set for investigation,
    not a finding, which is why it lives on the scope rather than the
    blast-radius report (Phase 5). ``build_blast_radius`` (impact.py) still
    treats every name here as a candidate to check telemetry against, never as
    proof that it is actually involved — that discipline is unchanged by where
    the names came from.

    Prefers the Context Pack's topology section when one was built. Otherwise
    falls back to a direct call into the topology provider chain
    (``aiops.tools.topology.resolver`` — cmdb/otel/k8s/snow/mock) — the same
    precedent ``evidence.py``'s ``recent_changes``/``trace_health`` already set
    for a live-only category that is not part of the Context Engineering
    Layer's byte-identity parity contract. Without this fallback, blast radius
    only ever sees real topology when ``AIOPS_CONTEXT_LAYER`` is on, which
    defaults to off — so every investigation reported ``topology_available:
    false`` regardless of how well-configured the resolver chain was.
    """
    if isinstance(context, dict):
        section = context.get("topology")
        if isinstance(section, dict):
            for payload in (section.get("raw") or {}).values():
                if isinstance(payload, dict):
                    deps = payload.get("dependencies")
                    if isinstance(deps, list) and deps:
                        return tuple(str(dep) for dep in deps)

    if service and service != "unknown":
        try:
            from aiops.tools.topology import resolver

            resolution = resolver.resolve(service)
            if resolution.resolved:
                return tuple(resolution.dependencies)
        except Exception:
            pass
    return ()


# ─── stage 2: timeline ──────────────────────────────────────────────────────


def build_timeline(
    scope: IncidentScope,
    facts: ObservedFacts,
    *,
    change_evidence: list[dict[str, Any]] | None = None,
) -> IncidentTimelineView:
    """Order the events that carry a real timestamp.

    Only commits and the alert qualify today (see the module docstring). Each change is
    classified ``PRECEDES_ONSET`` / ``FOLLOWS_ONSET`` against ``scope.onset_at``, which is
    what makes a change *eligible* as a cause — eligibility, not blame: the
    correlation-is-not-causation rule still applies downstream, and a change that follows
    onset is excluded outright.
    """
    events: list[RcaTimelineEvent] = []
    present: list[str] = []
    unavailable: list[str] = []

    if scope.onset_at and scope.alert_name:
        events.append(
            RcaTimelineEvent(
                timestamp=scope.onset_at,
                source="alert",
                service=scope.affected_service,
                event=f"{scope.alert_name} fired",
                severity=scope.severity,
                temporal_relation=TemporalRelation.AT_ONSET,
            )
        )
        present.append("alert")

    if change_evidence is None:
        unavailable.append("deployment")
    elif change_evidence:
        present.append("deployment")
        for commit in change_evidence:
            stamp = _parse_time(commit.get("date"))
            if stamp is None:
                continue
            events.append(
                RcaTimelineEvent(
                    timestamp=stamp,
                    source="deployment",
                    service=scope.affected_service,
                    event=f"commit {commit.get('sha', '?')}: {commit.get('message', '')}".strip(),
                    is_change=True,
                    temporal_relation=_relation(stamp, scope.onset_at),
                )
            )
    else:
        # An empty list is a real and useful answer — "nothing changed here recently"
        # actively argues against a deploy-induced cause — so the source counts as
        # present rather than unavailable.
        present.append("deployment")

    events.sort(key=lambda e: (e.timestamp, e.source, e.event))
    note = (
        "instant PromQL carries no history, so metric and pod facts appear without "
        "timestamps and are not on this timeline"
    )
    return IncidentTimelineView(
        events=tuple(events),
        onset_at=scope.onset_at,
        sources_present=tuple(dict.fromkeys(present)),
        sources_unavailable=tuple(dict.fromkeys(unavailable)),
        coverage_note=note,
    )


def _parse_time(raw: object) -> datetime | None:
    """Parse a provider timestamp, always returning an aware value.

    A naive stamp is read as UTC rather than left naive. Every provider in this repo
    queries in UTC, and the alternative is worse than an assumption: mixing naive and
    aware datetimes in one list makes ``sorted`` raise ``TypeError``, which on the incident
    path costs the whole timeline because one commit date lacked an offset. Same
    reconciliation ``ranker._signed_age_seconds`` and ``repository._aware`` already make.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _relation(stamp: datetime, onset: datetime | None) -> TemporalRelation:
    if onset is None:
        return TemporalRelation.UNKNOWN
    # Compared with tz-awareness reconciled the way ``ranker._signed_age_seconds`` does:
    # providers disagree about offsets and a naive/aware subtraction raises, which on the
    # incident path would cost the whole timeline for one badly formatted date.
    left, right = stamp, onset
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif right.tzinfo is None and left.tzinfo is not None:
        right = right.replace(tzinfo=left.tzinfo)
    return TemporalRelation.PRECEDES_ONSET if left < right else TemporalRelation.FOLLOWS_ONSET


# ─── stage 3: baseline ──────────────────────────────────────────────────────


def build_baselines(facts: ObservedFacts) -> tuple[BaselineComparison, ...]:
    """Compare observations against their expected operating range.

    Every comparison is ``PARTIAL``: the reference is an alert threshold or a container
    limit, which is a real expected range but not a learned per-service baseline. A
    service that always runs at 800 ms would still look abnormal against a 500 ms
    threshold, and only historical data could tell the difference — so the status says
    ``PARTIAL`` rather than ``AVAILABLE`` and no consumer can mistake one for the other.
    """
    out: list[BaselineComparison] = []
    for latency in facts.latencies:
        if latency.threshold is None:
            out.append(
                BaselineComparison(
                    metric=f"{latency.hop} p95",
                    status=BaselineStatus.UNAVAILABLE,
                    current_value=latency.seconds,
                    window_note="no threshold defined for this hop",
                )
            )
            continue
        out.append(
            BaselineComparison(
                metric=f"{latency.hop} p95",
                status=BaselineStatus.PARTIAL,
                current_value=latency.seconds,
                baseline_value=latency.threshold,
                deviation_ratio=round(latency.seconds / latency.threshold, 3),
                is_abnormal=latency.breaches_threshold,
                window_note="reference is the alert threshold, not a learned baseline",
            )
        )
    for gauge in facts.gauges:
        out.append(
            BaselineComparison(
                metric=gauge.metric,
                status=BaselineStatus.PARTIAL,
                current_value=gauge.value,
                baseline_value=1.0,
                is_abnormal=not gauge.reachable,
                window_note="a connection gauge is expected to read 1",
            )
        )
    for pod in facts.resources:
        if pod.cpu_cores is not None:
            out.append(
                BaselineComparison(
                    metric=f"{pod.pod} cpu cores",
                    status=BaselineStatus.PARTIAL,
                    current_value=pod.cpu_cores,
                    baseline_value=1.0,
                    deviation_ratio=round(pod.cpu_cores, 3),
                    is_abnormal=pod.cpu_cores >= 0.8,
                    window_note="reference is the 1-core container limit",
                )
            )
        if pod.memory_ratio is not None:
            out.append(
                BaselineComparison(
                    metric=f"{pod.pod} memory / limit",
                    status=BaselineStatus.PARTIAL,
                    current_value=pod.memory_ratio,
                    baseline_value=1.0,
                    deviation_ratio=round(pod.memory_ratio, 3),
                    is_abnormal=pod.memory_ratio >= 0.85,
                    window_note="reference is the container memory limit",
                )
            )
    return tuple(out)


# ─── stage 4: completeness ──────────────────────────────────────────────────


def build_completeness(
    facts: ObservedFacts, *, change_evidence: list[dict[str, Any]] | None
) -> InvestigationCompleteness:
    """How much of the evidence the investigation wanted it actually got.

    Reported beside confidence, never folded into it: ``confidence 0.9 /
    completeness 0.4`` and ``0.9 / 0.95`` are different verdicts, and a blended number
    hides which one the operator is holding.
    """
    per_source: dict[str, str] = {}
    gaps: list[str] = []

    for category, present in _CATEGORY_PRESENT.items():
        if facts.metrics is Availability.UNAVAILABLE:
            per_source[category] = "unavailable"
            gaps.append(category)
        else:
            per_source[category] = "collected" if present(facts) else "empty"

    per_source["logs"] = (
        "unavailable"
        if facts.logs is Availability.UNAVAILABLE
        else ("collected" if facts.log_lines else "empty")
    )
    if facts.logs is Availability.UNAVAILABLE:
        gaps.append("logs")

    if change_evidence is None:
        per_source[catalog.NEED_CHANGES] = "unavailable"
        gaps.append(catalog.NEED_CHANGES)
    else:
        per_source[catalog.NEED_CHANGES] = "collected" if change_evidence else "empty"

    answered = sum(1 for status in per_source.values() if status != "unavailable")
    overall = round(answered / len(per_source), 4) if per_source else 0.0
    return InvestigationCompleteness(
        per_source=per_source,
        overall=overall,
        critical_gaps=tuple(gaps),
        note=(
            f"{answered} of {len(per_source)} evidence sources answered"
            if per_source
            else "no sources requested"
        ),
    )


# ─── stages 5-7: hypotheses, matrix, scoring ────────────────────────────────


def build_matrices(
    facts: ObservedFacts,
    scope: IncidentScope,
    timeline: IncidentTimelineView,
    baselines: tuple[BaselineComparison, ...],
    *,
    change_evidence: list[dict[str, Any]] | None,
) -> list[EvidenceMatrix]:
    """Generate the candidate hypotheses that the evidence actually triggers, with a
    full matrix each.

    Only triggered rules become hypotheses. A catalog that proposed all ten every time
    would hand the discrimination work back to the LLM, which is the arrangement this
    replaces.
    """
    matrices: list[EvidenceMatrix] = []
    for rule in catalog.RULES:
        outcome = (
            _evaluate_change_rule(scope, timeline, change_evidence)
            if rule.rule_id == "change_induced_regression"
            else _safe_evaluate(rule, facts)
        )
        if not outcome.triggered:
            continue

        component = outcome.component or scope.affected_service
        hypothesis = Hypothesis(
            hypothesis_id=digest("rca-hyp", scope.incident_id, rule.rule_id),
            label=rule.label,
            mechanism=rule.mechanism.format(component=component),
            candidate_component=component,
            category=rule.category,
            origin="catalog",
            action_hint=rule.action_category,
        )
        hid = hypothesis.hypothesis_id

        supporting = tuple(
            _item(hid, statement, source, EvidenceStance.SUPPORTS)
            for statement, source in outcome.supporting
        )
        contradicting = tuple(
            _item(hid, statement, source, EvidenceStance.CONTRADICTS)
            for statement, source in outcome.contradicting
        )
        checked_absent, gaps = _absence_and_gaps(hid, rule, facts, change_evidence)

        matrices.append(
            EvidenceMatrix(
                hypothesis=hypothesis,
                supporting=supporting,
                contradicting=contradicting,
                checked_absent=checked_absent,
                gaps=gaps,
                baseline=baselines,
                # Every rule returns what argues against it, so refutation was attempted
                # for all of them — which is what makes an empty ``contradicting`` list
                # mean "nothing contradicts" rather than "nobody looked".
                contradiction_search_performed=True,
            )
        )
    return matrices


def _safe_evaluate(rule: catalog.HypothesisRule, facts: ObservedFacts) -> catalog.RuleOutcome:
    """A buggy rule costs its own hypothesis, never the investigation."""
    try:
        return rule.evaluate(facts)
    except Exception:
        logger.debug("hypothesis rule %s raised", rule.rule_id, exc_info=True)
        return catalog.RuleOutcome()


def _evaluate_change_rule(
    scope: IncidentScope,
    timeline: IncidentTimelineView,
    change_evidence: list[dict[str, Any]] | None,
) -> catalog.RuleOutcome:
    """Change correlation, evaluated against the timeline rather than the telemetry.

    Fires only on changes that *precede* onset. A commit landing after the incident
    started cannot have caused it, and the timeline already carries that classification —
    so the exclusion is applied from data rather than left to the model to notice.
    """
    outcome = catalog.RuleOutcome()
    if change_evidence is None:
        return outcome
    for event in timeline.pre_onset_changes:
        outcome.supporting.append((f"{event.event} before onset", "deployments"))
        outcome.component = outcome.component or scope.affected_service
    following = [
        e for e in timeline.changes if e.temporal_relation is TemporalRelation.FOLLOWS_ONSET
    ]
    if outcome.supporting and following:
        outcome.contradicting.append(
            (f"{len(following)} change(s) landed after onset and cannot be causes", "deployments")
        )
    if not outcome.supporting and not timeline.changes:
        # Worth surfacing as a fact even though the rule does not fire: it argues against
        # a deploy-induced cause for every other hypothesis's benefit.
        outcome.contradicting.append(
            ("no change preceded onset in the queried window", "deployments")
        )
    return outcome


def _item(hid: str, statement: str, source: str, stance: EvidenceStance) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=_evidence_id(hid, statement),
        stance=stance,
        statement=statement,
        source=source,
        section_status=None,
    )


def _absence_and_gaps(
    hid: str,
    rule: catalog.HypothesisRule,
    facts: ObservedFacts,
    change_evidence: list[dict[str, Any]] | None,
) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]]:
    """Split a rule's needed-but-empty signals into checked-absent versus unavailable.

    The single most important branch in this module. A needed category that is *empty on
    a reachable source* is evidence — it rules things out. The same category on an
    unreachable source is a blind spot. Collapsing them would let the agent rule out a
    cause it never observed.
    """
    absent: list[EvidenceItem] = []
    gaps: list[EvidenceItem] = []

    for need in rule.needs:
        if need == catalog.NEED_CHANGES:
            if change_evidence is None:
                gaps.append(
                    _item(
                        hid,
                        "change history was not available",
                        "deployments",
                        EvidenceStance.UNAVAILABLE,
                    )
                )
            elif not change_evidence:
                absent.append(
                    _item(
                        hid,
                        "change history was queried and no change preceded onset",
                        "deployments",
                        EvidenceStance.CHECKED_ABSENT,
                    )
                )
            continue

        present = _CATEGORY_PRESENT.get(need)
        if present is None:
            continue
        if facts.metrics is Availability.UNAVAILABLE:
            gaps.append(
                _item(hid, f"{need} could not be queried", "metrics", EvidenceStance.UNAVAILABLE)
            )
        elif not present(facts):
            absent.append(
                _item(
                    hid,
                    f"{need} was queried and reported nothing",
                    "metrics",
                    EvidenceStance.CHECKED_ABSENT,
                )
            )
    return tuple(absent), tuple(gaps)


# ─── orchestration ──────────────────────────────────────────────────────────


_STATUS_BANDS: tuple[tuple[float, RootCauseStatus], ...] = (
    (0.75, RootCauseStatus.CONFIRMED),
    (0.50, RootCauseStatus.PROBABLE),
    (0.30, RootCauseStatus.UNCERTAIN),
)
"""``(floor, status)`` best-first. Below the last floor is ``INSUFFICIENT_EVIDENCE``."""

_BAND_EPSILON = 0.0001
"""Kept below a band's ceiling by one unit of the 4-dp rounding scores use, so a
capped confidence cannot land exactly on the next band's floor."""


def _band(score: float) -> tuple[RootCauseStatus, float]:
    """``(status, exclusive ceiling)`` for a score."""
    ceiling = 1.0
    for floor, status in _STATUS_BANDS:
        if score >= floor:
            return status, ceiling
        ceiling = floor
    return RootCauseStatus.INSUFFICIENT_EVIDENCE, ceiling


def _status_for(
    ranked: list[EvidenceMatrix],
    facts: ObservedFacts,
    *,
    discriminated: bool,
    evidence_only_confidence: float | None = None,
) -> tuple[RootCauseStatus, float]:
    """Turn the ranking into a status and the authoritative confidence.

    Confidence is the selected hypothesis's deterministic score — not the model's figure,
    which this replaces. The status then follows from the score *and* from whether the
    ranking actually separated the candidates: a top score of 0.8 that its runner-up
    matches is ``UNCERTAIN``, because the evidence did not choose.

    History may refine, never promote
    ---------------------------------
    ``evidence_only_confidence`` is the selected hypothesis's score with its historical
    priors removed, and when supplied it — not the prior-inclusive score — decides the
    **status band**. Without this, ``scoring.PRIOR_MAX`` (0.10) was larger than the gap
    between adjacent thresholds, so an evidence-only 0.45 plus a full prior crossed 0.50
    and an ``UNCERTAIN`` became a ``PROBABLE`` on the strength of history alone. That is
    exactly the "history may never make the difference between abstaining and asserting"
    rule that ``PRIOR_MAX``'s docstring claims and the arithmetic did not deliver.

    A prior may still raise the *number* within the band it was already in, and may still
    reorder which hypothesis leads — breaking a tie is the job history is genuinely good
    at. What it cannot do is upgrade the claim.
    """
    if not facts.any_observation:
        return RootCauseStatus.INSUFFICIENT_EVIDENCE, 0.0
    if not ranked:
        return RootCauseStatus.INSUFFICIENT_EVIDENCE, 0.0

    top = ranked[0].score
    confidence = top.score if top else 0.0
    if not discriminated:
        # Cap as well as relabel: a number that reads like a settled conclusion beside a
        # status that says otherwise is worse than either alone.
        return RootCauseStatus.UNCERTAIN, min(confidence, 0.5)

    status, ceiling = _band(
        confidence if evidence_only_confidence is None else evidence_only_confidence
    )
    return status, round(min(confidence, ceiling - _BAND_EPSILON), 4)


def investigate(
    triage: dict[str, Any],
    facts: ObservedFacts,
    *,
    context: dict[str, Any] | None = None,
    change_evidence: list[dict[str, Any]] | None = None,
    budget: InvestigationBudget | None = None,
    recall: memory.MemoryRecall | None = None,
    action_vocabulary: tuple[str, ...] = (),
    executor_available: bool = False,
) -> Investigation:
    """Run every deterministic stage and return the result. Never raises.

    The budget is consulted rather than enforced by interruption: these stages are pure
    functions over already-collected facts, so none of them can run long. It is threaded
    through so an exhausted budget — set by a caller that gave up on retrieval — is
    reported on the investigation rather than being invisible, and so the iterative
    retrieval loop can hang off the same object without a second mechanism.

    ``recall`` is passed *in* rather than performed here so this function stays pure and
    testable against literal priors: retrieval is I/O and belongs with the caller that
    already owns the agent's other I/O. ``None`` means memory was never consulted, which
    is reported differently from "consulted and found nothing".

    ``action_vocabulary`` and ``executor_available`` arrive the same way and for the same
    reason — resolving what the platform can run is a registry call. Passing them in keeps
    this function pure, and keeps the recovery stage honest: the keys it may propose are
    exactly the keys the prompt was given, because the caller resolves both from one
    place.
    """
    notes: list[str] = []
    scope = build_scope(triage, facts, context=context)
    timeline = build_timeline(scope, facts, change_evidence=change_evidence)
    baselines = build_baselines(facts)
    completeness = build_completeness(facts, change_evidence=change_evidence)

    matrices = build_matrices(facts, scope, timeline, baselines, change_evidence=change_evidence)

    # Ranked twice when priors exist, so influence can be *measured* rather than
    # asserted. Cheap: scoring is pure, and the second pass is over the same matrices.
    ranked_without_memory: list[EvidenceMatrix] | None = None
    if recall is not None and recall.priors:
        ranked_without_memory = scoring.rank(matrices)
        matrices = memory.attach_priors(matrices, recall)

    ranked = scoring.rank(matrices)

    # Discrimination is an **evidence-only** property, for the same reason the status band
    # is: a 0.10 prior is larger than DISCRIMINATION_MARGIN's reach, so letting priors
    # into this test would turn "the evidence does not separate these two" into a
    # confident answer on the strength of history. Memory may reorder the candidates; it
    # may not manufacture the separation that justifies asserting one.
    evidence_ranking = ranked_without_memory or ranked
    discriminated = scoring.discriminates(evidence_ranking)
    evidence_only = None
    if ranked_without_memory and ranked:
        chosen = ranked[0].hypothesis.hypothesis_id
        match = next(
            (m for m in ranked_without_memory if m.hypothesis.hypothesis_id == chosen), None
        )
        evidence_only = match.score.score if match and match.score else None

    status, confidence = _status_for(
        ranked, facts, discriminated=discriminated, evidence_only_confidence=evidence_only
    )

    if not matrices and facts.any_observation:
        notes.append(
            "evidence was observed but no catalogued failure class matched it — the cause "
            "is outside the hypothesis catalog rather than absent"
        )
    if ranked and not discriminated:
        notes.append(
            f"top two hypotheses are within {scoring.DISCRIMINATION_MARGIN:.2f}: "
            "the evidence does not separate them"
        )

    budget_note = budget.exhaustion_reason if budget and budget.exhausted else None
    if budget_note:
        notes.append(f"investigation stopped early: {budget_note}")

    historical_influence = memory.influence(
        ranked, recall, ranked_without_memory=ranked_without_memory
    )
    if historical_influence.changed_ranking:
        # Surfaced in the notes as well as the influence record: an operator reading the
        # verdict should not have to open the memory section to learn that history is
        # why this hypothesis is on top.
        notes.append(
            "historical memory changed which hypothesis ranked first — see "
            "historical_influence for the priors involved"
        )

    blast_radius = impact.build_blast_radius(scope, facts, context=context)
    recovery_options = (
        recovery.build_recovery_options(
            tuple(ranked),
            vocabulary=action_vocabulary,
            executor_available=executor_available,
        )
        if status.is_actionable
        else ()
    )
    if ranked and not status.is_actionable:
        # Deliberate: RootCauseStatus.is_actionable is the single predicate for "should a
        # remediation be offered". Planning a recovery for a cause the evidence did not
        # settle produces a button beside the words "uncertain".
        notes.append(
            "no recovery option was planned: the status is not actionable, so the next "
            "step is a human looking rather than a fix executing"
        )
    verification = recovery.build_verification_plan(tuple(ranked))

    return Investigation(
        scope=scope,
        timeline=timeline,
        completeness=completeness,
        baselines=baselines,
        matrices=tuple(ranked),
        status=status,
        confidence=confidence,
        selected_hypothesis_id=(
            ranked[0].hypothesis.hypothesis_id if ranked and status.is_actionable else None
        ),
        discriminated=discriminated,
        historical_influence=historical_influence,
        blast_radius=blast_radius,
        recovery_options=recovery_options,
        verification=verification,
        budget=budget_note,
        notes=tuple(notes),
    )


__all__ = [
    "build_baselines",
    "build_completeness",
    "build_matrices",
    "build_scope",
    "build_timeline",
    "investigate",
]
