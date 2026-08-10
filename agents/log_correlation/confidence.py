"""Explainable confidence scoring for the Log Correlation agent (RA-007).

The number is unchanged
-----------------------
``explain_confidence`` reproduces the existing arithmetic exactly, and
``agent._confidence`` now delegates to it and returns ``breakdown.score``. There
is one implementation, so the explained score and the returned score cannot
drift apart — an identity guaranteed by construction rather than by a test that
someone has to remember to run.

Why a breakdown at all
----------------------
``confidence = 0.82`` is unactionable. A responder cannot tell whether that came
from three signal sources agreeing or from one weak heuristic, and neither can
the RCA agent. The breakdown answers, for every increment: which rule produced
it, why it applied, and which evidence triggered it.

On "deductions"
---------------
The algorithm has no negative terms. It starts at a 0.3 base, adds five possible
increments, caps at 0.95 and floors at 0.1 when there is no signal at all. So
rather than invent negative contributions that do not exist, this module reports:

- **contributors** — increments that actually applied.
- **deductions** — the cap, when it truncated the raw total. That is a genuine
  loss of score and the only one the algorithm has.
- **unapplied** — rules that did not fire, with the reason. Not a deduction, but
  the most useful part of the explanation in practice: "confidence is 0.6 because
  only one signal source was present" tells a responder what evidence is missing.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The algorithm's constants, named so the explanation can cite them rather than
# repeat magic numbers. Values are exactly those of the original implementation.
BASE_SCORE = 0.3
NO_SIGNAL_SCORE = 0.1
MAX_SCORE = 0.95

RULE_DELTAS = {
    "multi_source": 0.2,
    "tri_source": 0.15,
    "cross_source_recurrence": 0.1,
    "error_severity_first": 0.15,
    "suspects_identified": 0.1,
}


class ConfidenceContributor(BaseModel):
    """One increment that was applied, with its justification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    """Stable identifier of the rule that produced this increment. Stable so a
    consumer can compare two verdicts rule-by-rule rather than by prose."""

    description: str
    """Why the rule applied, in terms a responder can act on."""

    delta: float
    triggered_by: list[str] = Field(default_factory=list)
    """Evidence ids, or concrete facts when no evidence backs the rule.

    Evidence ids are preferred: they let a reader follow a score increment all
    the way to the log line that caused it. Some rules are about the *shape* of
    the evidence set (how many sources exist) rather than any single item, and
    for those a factual description is the honest answer."""


class ConfidenceDeduction(BaseModel):
    """Score that was lost, with the reason.

    Distinct from an unapplied rule: a deduction means points were taken away,
    which in this algorithm happens only at the cap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    description: str
    delta: float
    """Negative. The amount removed from the raw total."""


class UnappliedRule(BaseModel):
    """A rule that did not fire, and why.

    Often the most useful line in the explanation: "only one signal source was
    present" tells a responder which evidence would raise confidence, which a
    bare number never can.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    reason: str
    potential_delta: float
    """What the score would have gained had it applied."""


class ConfidenceBreakdown(BaseModel):
    """Full derivation of a confidence score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    """Identical to ``CorrelationResult.confidence`` — same computation, one
    implementation."""

    base: float = BASE_SCORE
    explanation: str
    contributors: list[ConfidenceContributor] = Field(default_factory=list)
    deductions: list[ConfidenceDeduction] = Field(default_factory=list)
    unapplied: list[UnappliedRule] = Field(default_factory=list)
    rule_trace: list[str] = Field(default_factory=list)
    """Ordered log of every rule evaluation, applied or not.

    The arithmetic audit: summing the base and the applied deltas, then applying
    the deductions, must reproduce ``score``. A reader can verify the number
    instead of trusting it."""

    capped: bool = False

    @property
    def raw_total(self) -> float:
        """Score before the cap — what the rules actually summed to."""
        return round(self.base + sum(c.delta for c in self.contributors), 3)


def _ids_for(evidence: list[Any], predicate) -> list[str]:
    """Evidence ids matching ``predicate``, for linking a rule to its trigger.

    Defensive about shape: evidence may be absent (the breakdown is built even
    when evidence assembly is skipped or failed), and a missing link should
    degrade the explanation rather than break scoring.
    """
    out: list[str] = []
    for ev in evidence or []:
        try:
            if predicate(ev):
                out.append(ev.evidence_id)
        except Exception:
            continue
    return out


def explain_confidence(
    signal_counts: dict[str, int],
    top_signatures: list[str],
    suspects: list[str],
    first_error: Any,
    cross_source: bool,
    *,
    error_severities: set[str],
    evidence: list[Any] | None = None,
) -> ConfidenceBreakdown:
    """Compute the confidence score and its full derivation.

    The arithmetic is byte-for-byte the original: 0.3 base; +0.2 for two or more
    sources; +0.15 for three; +0.1 for cross-source recurrence; +0.15 when the
    first error is error-severity; +0.1 when suspects were identified; capped at
    0.95; and a flat 0.1 when there is no signal at all.

    ``evidence`` is optional and used only to attach ids to contributors. Scoring
    never depends on it, so an explanation stays correct when evidence is absent.
    """
    evidence = evidence or []
    n_sources = sum(1 for v in signal_counts.values() if v > 0)
    total = sum(signal_counts.values())
    trace: list[str] = [
        f"signal_counts={signal_counts} sources_with_signal={n_sources} total_signals={total}"
    ]

    # No-signal floor. Returns early in the original, so it must here too — and
    # it is a distinct outcome worth explaining rather than a degenerate case.
    if total == 0:
        trace.append(f"no signals in window -> floor {NO_SIGNAL_SCORE} (rule: no_signal_floor)")
        return ConfidenceBreakdown(
            score=NO_SIGNAL_SCORE,
            base=NO_SIGNAL_SCORE,
            explanation=(
                "Confidence 0.1: no signals were collected in the window, so the verdict "
                "rests on no evidence. Every scoring rule was skipped."
            ),
            rule_trace=trace,
            unapplied=[
                UnappliedRule(
                    rule_id=rule,
                    reason="no signals collected, so no rule could be evaluated",
                    potential_delta=delta,
                )
                for rule, delta in RULE_DELTAS.items()
            ],
        )

    contributors: list[ConfidenceContributor] = []
    unapplied: list[UnappliedRule] = []
    score = BASE_SCORE
    trace.append(f"base={BASE_SCORE} (rule: base_score)")

    present = sorted(k for k, v in signal_counts.items() if v > 0)

    # Rule: two or more signal sources.
    if n_sources >= 2:
        score += RULE_DELTAS["multi_source"]
        contributors.append(
            ConfidenceContributor(
                rule_id="multi_source",
                description=(
                    f"{n_sources} independent signal sources carried evidence "
                    f"({', '.join(present)}); corroboration across backends is stronger "
                    "than any single source."
                ),
                delta=RULE_DELTAS["multi_source"],
                triggered_by=_ids_for(evidence, lambda e: e.source in present),
            )
        )
        trace.append(f"multi_source applied +{RULE_DELTAS['multi_source']} -> {round(score, 3)}")
    else:
        unapplied.append(
            UnappliedRule(
                rule_id="multi_source",
                reason=(
                    f"only {n_sources} signal source carried evidence "
                    f"({', '.join(present) or 'none'}); two or more are required"
                ),
                potential_delta=RULE_DELTAS["multi_source"],
            )
        )
        trace.append("multi_source not applied (fewer than 2 sources)")

    # Rule: all three signal sources.
    if n_sources >= 3:
        score += RULE_DELTAS["tri_source"]
        contributors.append(
            ConfidenceContributor(
                rule_id="tri_source",
                description=(
                    "All three signal sources (logs, traces, metrics) carried evidence — "
                    "the strongest corroboration the agent can observe."
                ),
                delta=RULE_DELTAS["tri_source"],
                triggered_by=_ids_for(evidence, lambda e: e.source in present),
            )
        )
        trace.append(f"tri_source applied +{RULE_DELTAS['tri_source']} -> {round(score, 3)}")
    else:
        unapplied.append(
            UnappliedRule(
                rule_id="tri_source",
                reason=f"{n_sources} of 3 signal sources carried evidence",
                potential_delta=RULE_DELTAS["tri_source"],
            )
        )
        trace.append("tri_source not applied (fewer than 3 sources)")

    # Rule: the same signature appears in more than one source.
    if cross_source:
        score += RULE_DELTAS["cross_source_recurrence"]
        contributors.append(
            ConfidenceContributor(
                rule_id="cross_source_recurrence",
                description=(
                    "The same error signature recurred in at least two different sources, "
                    "so the finding is not an artifact of one backend's instrumentation."
                ),
                delta=RULE_DELTAS["cross_source_recurrence"],
                triggered_by=_ids_for(
                    evidence, lambda e: len(e.supporting_telemetry.sources_agreeing) >= 2
                ),
            )
        )
        trace.append(
            f"cross_source_recurrence applied "
            f"+{RULE_DELTAS['cross_source_recurrence']} -> {round(score, 3)}"
        )
    else:
        unapplied.append(
            UnappliedRule(
                rule_id="cross_source_recurrence",
                reason="no single signature appeared in two or more sources",
                potential_delta=RULE_DELTAS["cross_source_recurrence"],
            )
        )
        trace.append("cross_source_recurrence not applied (no shared signature)")

    # Rule: the earliest observation is an actual error.
    first_error_severity = getattr(first_error, "severity", None)
    is_error_first = (
        first_error is not None
        and isinstance(first_error_severity, str)
        and first_error_severity.lower() in error_severities
    )
    if is_error_first:
        score += RULE_DELTAS["error_severity_first"]
        sig = getattr(first_error, "signature", "?")
        contributors.append(
            ConfidenceContributor(
                rule_id="error_severity_first",
                description=(
                    f"The earliest observation is error-severity "
                    f"({first_error_severity}): {sig!r} — the window opens on a real "
                    "failure rather than on routine activity."
                ),
                delta=RULE_DELTAS["error_severity_first"],
                triggered_by=_ids_for(evidence, lambda e: e.normalized_signature == sig),
            )
        )
        trace.append(
            f"error_severity_first applied "
            f"+{RULE_DELTAS['error_severity_first']} -> {round(score, 3)}"
        )
    else:
        reason = (
            "no signals in the timeline"
            if first_error is None
            else (
                f"the earliest observation is {first_error_severity!r}, not an error severity; "
                "the window opens on routine activity"
            )
        )
        unapplied.append(
            UnappliedRule(
                rule_id="error_severity_first",
                reason=reason,
                potential_delta=RULE_DELTAS["error_severity_first"],
            )
        )
        trace.append("error_severity_first not applied (earliest signal is not error-severity)")

    # Rule: a suspect component was identified.
    if suspects:
        score += RULE_DELTAS["suspects_identified"]
        contributors.append(
            ConfidenceContributor(
                rule_id="suspects_identified",
                description=(
                    f"Suspect component(s) identified ({', '.join(suspects)}); the evidence "
                    "points somewhere specific rather than merely confirming a problem."
                ),
                delta=RULE_DELTAS["suspects_identified"],
                triggered_by=_ids_for(
                    evidence,
                    lambda e: e.topology_context.implicated_service in suspects,
                ),
            )
        )
        trace.append(
            f"suspects_identified applied +{RULE_DELTAS['suspects_identified']} -> {round(score, 3)}"
        )
    else:
        unapplied.append(
            UnappliedRule(
                rule_id="suspects_identified",
                reason="no suspect component could be derived from the evidence and topology",
                potential_delta=RULE_DELTAS["suspects_identified"],
            )
        )
        trace.append("suspects_identified not applied (no suspects)")

    # The cap is the algorithm's only real deduction.
    deductions: list[ConfidenceDeduction] = []
    raw = score
    capped = raw > MAX_SCORE
    if capped:
        deductions.append(
            ConfidenceDeduction(
                rule_id="max_score_cap",
                description=(
                    f"Raw rule total {round(raw, 3)} exceeded the {MAX_SCORE} ceiling. "
                    "A heuristic verdict is never allowed to claim near-certainty."
                ),
                delta=round(MAX_SCORE - raw, 3),
            )
        )
        trace.append(f"max_score_cap applied {round(MAX_SCORE - raw, 3)} -> {MAX_SCORE}")

    final = round(min(score, MAX_SCORE), 3)
    trace.append(f"final={final}")

    applied_names = [c.rule_id for c in contributors]
    explanation = (
        f"Confidence {final}: base {BASE_SCORE}"
        + (
            f" plus {len(contributors)} rule(s) ({', '.join(applied_names)})"
            if contributors
            else ""
        )
        + (f", capped at {MAX_SCORE}" if capped else "")
        + (
            f". {len(unapplied)} rule(s) did not apply: "
            + "; ".join(f"{u.rule_id} ({u.reason})" for u in unapplied)
            if unapplied
            else "."
        )
    )

    return ConfidenceBreakdown(
        score=final,
        base=BASE_SCORE,
        explanation=explanation,
        contributors=contributors,
        deductions=deductions,
        unapplied=unapplied,
        rule_trace=trace,
        capped=capped,
    )
