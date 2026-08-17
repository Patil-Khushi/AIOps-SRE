"""Deterministic hypothesis scoring — the platform's answer, not the model's.

The LLM used to state its own ``confidence_score`` and the platform passed it through.
There is no way to review a number that was asserted rather than derived, and a model
asked for confidence reliably produces a confident one. So the score is computed here,
from the structured evidence, and the model's figure is kept only as
``llm_stated_confidence`` for calibration.

Shape, not algorithm, is borrowed from ``agents/log_correlation/confidence.py``: a base
plus named increments, with every rule that *did not* fire recorded and why. That
convention exists because "confidence is 0.55 because no second source corroborated it"
tells a responder what to go and look at, and a bare 0.55 never can. The arithmetic
differs — this one has genuine negative terms, because a contradiction has to be able to
push a hypothesis *down* rather than merely fail to lift it.

Why additive rules rather than a weighted sum
--------------------------------------------
``aiops/context/ranker.py`` uses weights summing to 1.0 over four always-present
factors, which is right for ranking observations where every factor is defined for every
input. Here the factors are conditional — a hypothesis with no lifecycle data has no
lifecycle term at all — and a weighted mean over absent factors either invents values
for them or silently reweights the rest. Additive increments with an explicit
"unapplied" list keep the absence visible.

Evidence outranks history, arithmetically
-----------------------------------------
``PRIOR_MAX`` bounds the total contribution any historical prior can make, and
:func:`score` cancels the prior entirely when current evidence contradicts the
hypothesis. Current evidence winning is therefore a property of the arithmetic rather
than an instruction in a prompt, which is what the requirement asks for. Memory itself
arrives in Phase 3; the term is defined and tested now so the ceiling exists before
anything can lean on it.
"""

from __future__ import annotations

from agents.rca_agent.investigation.facts import Availability, ObservedFacts
from agents.rca_agent.investigation.models import (
    EvidenceMatrix,
    HypothesisScore,
    ScoreFactor,
    UnappliedRule,
)

BASE = 0.25
"""Starting point for a hypothesis that was proposed at all.

Non-zero because a rule only proposes itself when something observed triggered it, so
mere candidacy is weak evidence. Low enough that a candidate with nothing else going for
it lands in ``INSUFFICIENT_EVIDENCE`` territory rather than looking like a finding.
"""

DELTA_DIRECT = 0.30
"""At least one direct observation supports it. The largest single increment: a
hypothesis backed by telemetry is categorically different from one backed by reasoning
about the service name."""

DELTA_MULTI_SIGNAL = 0.12
"""Two or more independent supporting observations. Smaller than ``DELTA_DIRECT``
because the second reading of the same kind of signal mostly confirms the first."""

DELTA_CROSS_SOURCE = 0.15
"""Supporting evidence from two or more sources (metrics *and* logs). The strongest
inference available: one backend reporting a problem can be that backend's
instrumentation, two independently seeing it cannot. Matches the emphasis
``ranker.WEIGHT_AGREEMENT`` and RA-007 both place on corroboration."""

DELTA_NEGATIVE_COROBORATION = 0.10
"""A signal was checked, found absent, and its absence is consistent with this
hypothesis while excluding a rival. This is negative evidence doing real work — the
term exists so "we looked and it wasn't CPU" can raise the score of the hypothesis that
remains, which is how an SRE actually narrows a diagnosis."""

PENALTY_CONTRADICTED = -0.35
"""Any contradicting observation. Deliberately larger in magnitude than
``DELTA_DIRECT``: a hypothesis with one supporting reading and one contradicting reading
should end up *below* base, because something observed argues against it. Ranking a
contradicted hypothesis above an unchallenged one is how a confident wrong answer gets
produced."""

PENALTY_CRITICAL_GAP = -0.12
"""A fact category the rule needs was unavailable. Not a refutation — the hypothesis
may well be right — but an untested hypothesis must not outrank a tested one, and the
verdict has to be able to say the investigation was incomplete."""

PENALTY_SINGLE_WEAK = -0.10
"""Exactly one supporting observation, from one source, with nothing corroborating.
The shape of a coincidence."""

PRIOR_MAX = 0.10
"""Ceiling on the total contribution of historical priors.

Bounded by the *weakest* current-evidence increment on purpose: it is equal to
``DELTA_NEGATIVE_COROBORATION`` and strictly below every other term. A prior is
therefore worth at most what one checked-and-found-absent signal is worth, and always
less than a direct observation, a second signal, or cross-source corroboration. History
may break a tie or order an investigation; it may never make the difference between
abstaining and asserting. "Database exhaustion is common here" must not outvote a
healthy database.

**This ceiling alone was not enough**, and the gap is worth recording because the
docstring above claimed a guarantee the arithmetic did not provide. 0.10 is larger than
the gap between adjacent status thresholds (0.30/0.50/0.75) and larger than
``DISCRIMINATION_MARGIN`` can absorb, so an evidence-only 0.45 plus a full prior crossed
into ``PROBABLE``, and a pair the evidence could not separate became separated. Two
further rules in ``pipeline._status_for`` close it: the **status band** and the
**discrimination test** are both computed from the prior-free score. A prior may raise
the number inside its band and may reorder candidates; it may not upgrade the claim.
"""

MAX_SCORE = 0.95
"""No deterministic rule set earns certainty. Matches RA-007's ceiling, and the
reasoning is the same: a heuristic verdict that claims 1.0 is claiming the rules are
complete, and they are not."""

MIN_SCORE = 0.05


def _clamp(value: float) -> float:
    return MIN_SCORE if value < MIN_SCORE else MAX_SCORE if value > MAX_SCORE else value


def _sources(matrix: EvidenceMatrix) -> set[str]:
    return {item.source for item in matrix.supporting if item.source}


def score(matrix: EvidenceMatrix, facts: ObservedFacts | None = None) -> HypothesisScore:
    """Score one hypothesis from its evidence matrix. Pure, total, explainable.

    Takes the matrix rather than the raw facts because everything that bears on the
    hypothesis has already been classified into stances by that point — which is what
    makes the arithmetic auditable: every term below cites evidence ids that a reader can
    follow back to an observation.
    """
    factors: list[ScoreFactor] = []
    unapplied: list[UnappliedRule] = []
    trace: list[str] = []
    total = BASE
    trace.append(f"base={BASE}")

    supporting = list(matrix.supporting)
    contradicting = list(matrix.contradicting)
    absent = list(matrix.checked_absent)
    gaps = list(matrix.gaps)
    sources = _sources(matrix)

    def apply(rule_id: str, delta: float, description: str, ids: tuple[str, ...]) -> None:
        nonlocal total
        total += delta
        factors.append(
            ScoreFactor(rule_id=rule_id, description=description, delta=delta, triggered_by=ids)
        )
        trace.append(f"{rule_id} {delta:+.2f} -> {round(total, 4)}")

    def skip(rule_id: str, reason: str, potential: float) -> None:
        unapplied.append(UnappliedRule(rule_id=rule_id, reason=reason, potential_delta=potential))
        trace.append(f"{rule_id} not applied ({reason})")

    # --- positive terms ---------------------------------------------------
    if supporting:
        apply(
            "direct_evidence",
            DELTA_DIRECT,
            f"{len(supporting)} direct observation(s) support this hypothesis",
            tuple(item.evidence_id for item in supporting),
        )
    else:
        skip("direct_evidence", "no supporting observation", DELTA_DIRECT)

    if len(supporting) >= 2:
        apply(
            "multi_signal",
            DELTA_MULTI_SIGNAL,
            f"{len(supporting)} independent supporting observations, not a single reading",
            tuple(item.evidence_id for item in supporting),
        )
    else:
        skip(
            "multi_signal",
            f"{len(supporting)} supporting observation(s); 2 required",
            DELTA_MULTI_SIGNAL,
        )

    if len(sources) >= 2:
        apply(
            "cross_source",
            DELTA_CROSS_SOURCE,
            f"corroborated across {len(sources)} sources ({'+'.join(sorted(sources))})",
            tuple(item.evidence_id for item in supporting),
        )
    else:
        skip(
            "cross_source",
            f"only {'+'.join(sorted(sources)) or 'no'} source carried supporting evidence",
            DELTA_CROSS_SOURCE,
        )

    if absent:
        apply(
            "negative_corroboration",
            DELTA_NEGATIVE_COROBORATION,
            f"{len(absent)} signal(s) checked and found absent, excluding rival causes",
            tuple(item.evidence_id for item in absent),
        )
    else:
        skip(
            "negative_corroboration",
            "no checked-and-absent signal narrows this hypothesis",
            DELTA_NEGATIVE_COROBORATION,
        )

    # --- negative terms ---------------------------------------------------
    if contradicting:
        apply(
            "contradicted",
            PENALTY_CONTRADICTED,
            f"{len(contradicting)} observation(s) argue against this hypothesis",
            tuple(item.evidence_id for item in contradicting),
        )
    else:
        note = (
            "refutation was attempted and nothing contradicts it"
            if matrix.contradiction_search_performed
            else "no contradiction search was performed, so this is not a clean bill of health"
        )
        skip("contradicted", note, PENALTY_CONTRADICTED)

    if gaps:
        apply(
            "critical_gap",
            PENALTY_CRITICAL_GAP,
            f"{len(gaps)} needed signal(s) unavailable: this hypothesis is undertested",
            tuple(item.evidence_id for item in gaps),
        )
    else:
        skip(
            "critical_gap", "every signal this hypothesis needs was available", PENALTY_CRITICAL_GAP
        )

    if len(supporting) == 1 and len(sources) <= 1 and not absent:
        apply(
            "single_weak_signal",
            PENALTY_SINGLE_WEAK,
            "one observation, one source, nothing corroborating — the shape of a coincidence",
            tuple(item.evidence_id for item in supporting),
        )
    else:
        skip("single_weak_signal", "more than a lone uncorroborated reading", PENALTY_SINGLE_WEAK)

    # --- historical prior, bounded and cancellable ------------------------
    eligible = [prior for prior in matrix.priors if prior.eligible]
    if eligible and contradicting:
        # The requirement, as arithmetic: history cannot rescue a hypothesis that current
        # evidence argues against.
        skip(
            "historical_prior",
            f"{len(eligible)} eligible prior(s) cancelled: current evidence contradicts this "
            "hypothesis and current evidence wins",
            PRIOR_MAX,
        )
    elif eligible:
        best = max(prior.similarity for prior in eligible)
        delta = round(min(PRIOR_MAX, PRIOR_MAX * best), 4)
        apply(
            "historical_prior",
            delta,
            f"{len(eligible)} verified similar incident(s), best similarity {best:.2f}; "
            f"capped at {PRIOR_MAX} so history cannot outweigh current evidence",
            tuple(prior.memory_id for prior in eligible),
        )
    else:
        skip("historical_prior", "no verified historical incident is similar", PRIOR_MAX)

    if facts is not None and facts.metrics is Availability.UNAVAILABLE:
        trace.append("note: metrics were unavailable, so absence carried no information")

    raw = round(total, 4)
    final = _clamp(raw)
    capped = final != raw
    if capped:
        trace.append(f"clamped {raw} -> {final}")
    trace.append(f"final={final}")

    applied = ", ".join(factor.rule_id for factor in factors) or "none"
    explanation = (
        f"Score {final}: base {BASE} with {len(factors)} rule(s) applied ({applied}); "
        f"{len(unapplied)} did not apply."
    )
    return HypothesisScore(
        score=final,
        factors=tuple(factors),
        unapplied=tuple(unapplied),
        rule_trace=tuple(trace),
        capped=capped,
        explanation=explanation,
    )


def rank(matrices: list[EvidenceMatrix]) -> list[EvidenceMatrix]:
    """Score every matrix and return them best-first.

    Ties are broken on ``hypothesis_id`` rather than left to input order: the rule
    catalog is a fixed tuple today, but a future generation step that reorders
    candidates must not silently reorder equal-scoring hypotheses, or an eval diff
    reports a change that did not happen. Same discipline as ``ranker._sort_key``.
    """
    scored = [m.model_copy(update={"score": score(m)}) for m in matrices]
    return sorted(
        scored,
        key=lambda m: (-(m.score.score if m.score else 0.0), m.hypothesis.hypothesis_id),
    )


DISCRIMINATION_MARGIN = 0.15
"""How far ahead the top hypothesis must be to count as chosen.

Roughly one mid-sized increment (``DELTA_CROSS_SOURCE``): two hypotheses closer than
that differ by less than a single piece of corroborating evidence, which is not enough to
call one of them the answer.
"""


def discriminates(ranked: list[EvidenceMatrix], margin: float = DISCRIMINATION_MARGIN) -> bool:
    """Whether the top hypothesis is meaningfully ahead of the runner-up.

    The test that separates ``PROBABLE`` from ``UNCERTAIN``. Two hypotheses within
    ``margin`` of each other means the evidence did not choose between them, and saying
    so is more useful than presenting a coin flip as a conclusion. A single candidate
    trivially discriminates — there is nothing it is failing to separate from.
    """
    if len(ranked) < 2:
        return True
    top, second = ranked[0].score, ranked[1].score
    if top is None or second is None:
        return False
    return (top.score - second.score) >= margin


__all__ = [
    "BASE",
    "MAX_SCORE",
    "MIN_SCORE",
    "PRIOR_MAX",
    "discriminates",
    "rank",
    "score",
]
