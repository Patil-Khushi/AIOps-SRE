"""Runbook discovery — rank every candidate for an incident, and say why (§3–§6).

Deterministic, explainable, service-scoped, environment-aware and auditable, with no
LLM anywhere on the path: the score is a weighted sum over the facet verdicts
``applicability.evaluate_facets`` produced, and every point is reported back as a
reason string. Two runs over the same library and the same incident produce the same
ranking, including ties (broken by runbook id).

**How the score is defined.** It is *"of the things we could actually compare, how much
matched"* — not a probability and not a similarity metric:

    score = (earned + specificity) / (comparable + max_specificity)

A facet is *comparable* only when the runbook declares a constraint AND the incident
supplied the corresponding fact. A facet the runbook leaves open, or one the incident
says nothing about, is excluded from both halves rather than counted as a match or a
miss — inventing a verdict for missing data is how a "96% match" stops meaning
anything. ``specificity`` then rewards the runbook that committed to more constraints,
so a specific runbook outranks a catch-all when both match everything they claim.

The trade-off this makes on purpose: a generic runbook that constrains nothing can
score well, because it *does* match everything it claims. That is why specificity is in
the numerator and why §6's automatic-selection rule keys off applicability rather than
score — a high score is never on its own a licence to execute.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from agents.runbook_executor import risk as risk_mod
from agents.runbook_executor.actions import validate_runbook
from agents.runbook_executor.applicability import (
    ApplicabilityResult,
    ApplicabilityStatus,
    FacetVerdict,
    IncidentContext,
    evaluate,
    service_matches,
)
from agents.runbook_executor.library import ExecutableRunbook
from agents.runbook_executor.models import RunbookStatus

# Facet weights. Ordered by how much a match tells you about whether this is the right
# procedure: the service is the hard gate, the failure category is the strongest
# semantic signal, the alert is the strongest deployment-specific one, and symptom tags
# are last because they are keyword-derived and noisy (they were the *whole* of the v0
# selector, which is exactly why v0 could pick a runbook off one keyword).
FACET_WEIGHTS: dict[str, float] = {
    "service": 0.30,
    "failure_category": 0.18,
    "alert": 0.14,
    "required_signals": 0.12,
    "environment": 0.08,
    "incident_type": 0.06,
    "severity": 0.04,
    "tags": 0.04,
}
# Weight carried by the prerequisite check as a whole.
PREREQUISITE_WEIGHT = 0.10
# Weight carried by specificity (how many constraints the runbook commits to).
SPECIFICITY_WEIGHT = 0.04
# The constraint kinds specificity counts. Four, so each is worth a quarter of it.
_SPECIFICITY_FIELDS = ("failure_category", "alerts", "required_signals", "incident_types")


class DiscoveryDecision(StrEnum):
    """What the executor concluded about *who chooses*, not about what to run.

    ``AUTO_SELECT``    — exactly one applicable candidate; the platform may proceed.
    ``CANDIDATES``     — several applicable; an SRE picks (§6 CASE 2).
    ``AMBIGUOUS``      — applicability could not be determined; never executes.
    ``BLOCKED``        — the matching runbook exists but is refused (status, stale
                         incident, failed mandatory prerequisite). §6 CASE 5.
    ``NOT_APPLICABLE`` — runbooks exist for the service, none of them fit.
    ``NO_RUNBOOK``     — nothing in the library covers this service.
    """

    AUTO_SELECT = "AUTO_SELECT"
    CANDIDATES = "CANDIDATES"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NO_RUNBOOK = "NO_RUNBOOK"


class ScoreComponent(BaseModel):
    """One line of the score's arithmetic, so the total can be audited."""

    facet: str
    verdict: str
    weight: float
    earned: float
    comparable: bool
    detail: str = ""


class RunbookCandidate(BaseModel):
    """One ranked candidate — the §4 contract, plus the arithmetic behind it."""

    runbook_id: str
    version: int
    title: str
    service: str
    status: RunbookStatus
    match_score: float
    match_reasons: list[str] = Field(default_factory=list)
    applicability_status: ApplicabilityStatus
    risk_level: risk_mod.RiskLevel
    rollback_available: bool
    hitl_required: bool
    missing_prerequisites: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    # Extras the UI and the audit trail use; not part of the §4 minimum.
    specificity: int = 0
    steps_total: int = 0
    mutating_steps: int = 0
    score_components: list[ScoreComponent] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    applicability: ApplicabilityResult | None = None
    # Advisory only — never a licence to execute (§6, §7). Set on exactly the
    # top-ranked APPLICABLE candidate by discover(), including when the decision is
    # CANDIDATES: "several good matches, an SRE must choose" does not mean the
    # executor has nothing useful to say about which one best fits the failure. This
    # is the one flag an SRE clicking through a candidate list should trust as "start
    # here" — it is never used to auto-select or skip re-validation.
    recommended: bool = False

    @property
    def selectable(self) -> bool:
        """May an operator ask for this one to be planned? Blocked/not-applicable
        candidates are shown (so the refusal is visible) but cannot be chosen."""
        return self.applicability_status is ApplicabilityStatus.APPLICABLE


class DiscoveryResult(BaseModel):
    """The outcome of discovery: a ranked, service-scoped candidate list + a decision."""

    decision: DiscoveryDecision
    reason: str = ""
    candidates: list[RunbookCandidate] = Field(default_factory=list)
    auto_selected: str | None = None  # runbook id, only when decision is AUTO_SELECT

    @property
    def applicable(self) -> list[RunbookCandidate]:
        return [c for c in self.candidates if c.selectable]

    def candidate(self, runbook_id: str) -> RunbookCandidate | None:
        return next((c for c in self.candidates if c.runbook_id == runbook_id), None)


def specificity(runbook: ExecutableRunbook) -> int:
    """How many of the four constraint kinds this runbook commits to (0–4)."""
    scope = runbook.applicability
    return sum(1 for field in _SPECIFICITY_FIELDS if getattr(scope, field, None))


def score_candidate(
    runbook: ExecutableRunbook, result: ApplicabilityResult
) -> tuple[float, list[ScoreComponent], list[str]]:
    """(score, components, reasons) for an already-evaluated applicability result."""
    components: list[ScoreComponent] = []
    reasons: list[str] = []
    earned = 0.0
    comparable = 0.0

    for facet in result.facets:
        weight = FACET_WEIGHTS.get(facet.name)
        if weight is None:
            continue
        is_comparable = facet.verdict in (FacetVerdict.MATCH, FacetVerdict.MISMATCH)
        gained = weight if facet.verdict is FacetVerdict.MATCH else 0.0
        if is_comparable:
            comparable += weight
            earned += gained
        if facet.verdict is FacetVerdict.MATCH:
            reasons.append(facet.detail)
        components.append(
            ScoreComponent(
                facet=facet.name,
                verdict=facet.verdict.value,
                weight=weight,
                earned=gained,
                comparable=is_comparable,
                detail=facet.detail,
            )
        )

    # Prerequisites participate as one term: comparable whenever any are declared.
    if result.prerequisites:
        mandatory = [p for p in result.prerequisites if p.mandatory]
        satisfied = all(p.status.value == "satisfied" for p in mandatory) if mandatory else False
        comparable += PREREQUISITE_WEIGHT
        earned += PREREQUISITE_WEIGHT if satisfied else 0.0
        if satisfied:
            reasons.append(f"all {len(mandatory)} mandatory prerequisite(s) satisfied")
        components.append(
            ScoreComponent(
                facet="prerequisites",
                verdict="match" if satisfied else "mismatch",
                weight=PREREQUISITE_WEIGHT,
                earned=PREREQUISITE_WEIGHT if satisfied else 0.0,
                comparable=True,
                detail=(
                    "every mandatory prerequisite satisfied"
                    if satisfied
                    else "not every mandatory prerequisite is satisfied"
                ),
            )
        )

    spec = specificity(runbook)
    spec_earned = SPECIFICITY_WEIGHT * (spec / len(_SPECIFICITY_FIELDS))
    components.append(
        ScoreComponent(
            facet="specificity",
            verdict=f"{spec}/{len(_SPECIFICITY_FIELDS)}",
            weight=SPECIFICITY_WEIGHT,
            earned=spec_earned,
            comparable=True,
            detail=f"runbook declares {spec} of {len(_SPECIFICITY_FIELDS)} constraint kinds",
        )
    )
    if spec:
        reasons.append(f"specific runbook — declares {spec} applicability constraint(s)")

    denominator = comparable + SPECIFICITY_WEIGHT
    score = (earned + spec_earned) / denominator if denominator > 0 else 0.0
    return round(min(1.0, max(0.0, score)), 4), components, reasons


def build_candidate(
    runbook: ExecutableRunbook, ctx: IncidentContext, *, now: object | None = None
) -> RunbookCandidate:
    """Evaluate, score and risk-assess one runbook against one incident."""
    result = evaluate(runbook, ctx, now=now)  # type: ignore[arg-type]
    score, components, reasons = score_candidate(runbook, result)
    plan_risk = risk_mod.assess_plan(runbook, environment=ctx.environment)
    validations = validate_runbook(runbook)
    validation_errors = [e for v in validations for e in v.errors]

    blocking = list(result.blocking_reasons)
    status = result.status
    if validation_errors and status is ApplicabilityStatus.APPLICABLE:
        # A runbook whose steps do not validate is refused here rather than at
        # execution time: the operator should never be offered a plan that cannot run.
        status = ApplicabilityStatus.BLOCKED
        blocking += validation_errors
    if plan_risk.blocked and status is ApplicabilityStatus.APPLICABLE:
        status = ApplicabilityStatus.BLOCKED
        blocking += plan_risk.blocking_reasons

    return RunbookCandidate(
        runbook_id=runbook.id,
        version=runbook.version,
        title=runbook.title,
        service=runbook.service,
        status=runbook.status,
        match_score=score,
        match_reasons=reasons,
        applicability_status=status,
        risk_level=plan_risk.level,
        rollback_available=plan_risk.rollback_available,
        hitl_required=plan_risk.hitl_required,
        missing_prerequisites=result.missing_prerequisites,
        warnings=list(result.warnings) + [w for v in validations for w in v.warnings],
        blocking_reasons=blocking,
        specificity=specificity(runbook),
        steps_total=len(runbook.steps),
        mutating_steps=plan_risk.mutating_steps,
        score_components=components,
        validation_errors=validation_errors,
        applicability=result,
    )


def rank_candidates(candidates: list[RunbookCandidate]) -> list[RunbookCandidate]:
    """Applicable first, then score, then specificity, then id.

    Id is the final key so the order is total and reproducible — an eval that asserts
    on "the top candidate" must not depend on dict or filesystem ordering.
    """
    order = {
        ApplicabilityStatus.APPLICABLE: 0,
        ApplicabilityStatus.UNKNOWN: 1,
        ApplicabilityStatus.BLOCKED: 2,
        ApplicabilityStatus.NOT_APPLICABLE: 3,
    }
    return sorted(
        candidates,
        key=lambda c: (
            order.get(c.applicability_status, 9),
            -c.match_score,
            -c.specificity,
            c.runbook_id,
        ),
    )


def _decide(candidates: list[RunbookCandidate]) -> tuple[DiscoveryDecision, str, str | None]:
    """§6's CASE 1–5, in one place."""
    applicable = [c for c in candidates if c.selectable]
    if len(applicable) == 1:
        winner = applicable[0]
        return (
            DiscoveryDecision.AUTO_SELECT,
            f"exactly one applicable runbook ({winner.runbook_id}@v{winner.version}, "
            f"match {winner.match_score:.2f})",
            winner.runbook_id,
        )
    if len(applicable) > 1:
        ids = ", ".join(f"{c.runbook_id}@v{c.version}" for c in applicable)
        return (
            DiscoveryDecision.CANDIDATES,
            f"{len(applicable)} applicable runbooks — an SRE selects which to evaluate: {ids}",
            None,
        )
    if not candidates:
        return (
            DiscoveryDecision.NO_RUNBOOK,
            "no runbook in the library covers this service",
            None,
        )
    if any(c.applicability_status is ApplicabilityStatus.UNKNOWN for c in candidates):
        return (
            DiscoveryDecision.AMBIGUOUS,
            "applicability could not be determined for the matching runbook(s) — "
            "nothing is executed without a definite verdict",
            None,
        )
    if any(c.applicability_status is ApplicabilityStatus.BLOCKED for c in candidates):
        blocked = [c for c in candidates if c.applicability_status is ApplicabilityStatus.BLOCKED]
        first = blocked[0].blocking_reasons[0] if blocked[0].blocking_reasons else "refused"
        return (
            DiscoveryDecision.BLOCKED,
            f"a runbook matches but is refused: {first}",
            None,
        )
    return (
        DiscoveryDecision.NOT_APPLICABLE,
        f"{len(candidates)} runbook(s) exist for this service but none applies to this incident",
        None,
    )


def discover(
    runbooks: list[ExecutableRunbook], ctx: IncidentContext, *, now: object | None = None
) -> DiscoveryResult:
    """Rank every service-scoped candidate for ``ctx`` and decide who chooses.

    Service scoping happens before anything else (§3): a runbook for another service is
    not a low-scoring candidate, it is not a candidate. Everything that survives is
    returned — including NOT_APPLICABLE and BLOCKED entries — because "there is a
    runbook for this and here is why it will not run" is the answer an operator needs.
    """
    scoped = [rb for rb in runbooks if rb.steps and service_matches(rb.service, ctx.service)]
    candidates = rank_candidates([build_candidate(rb, ctx, now=now) for rb in scoped])
    decision, reason, auto = _decide(candidates)
    # rank_candidates() already sorts applicable-first then by score, so the first
    # selectable entry — if any — IS the best match. Flagging it here, once, keeps
    # every caller (the API, the eval harness, the CLI) honest with the same answer
    # instead of each re-deriving "top of the applicable list" its own way.
    for candidate in candidates:
        if candidate.selectable:
            candidate.recommended = True
            break
    return DiscoveryResult(
        decision=decision, reason=reason, candidates=candidates, auto_selected=auto
    )


__all__ = [
    "FACET_WEIGHTS",
    "PREREQUISITE_WEIGHT",
    "SPECIFICITY_WEIGHT",
    "DiscoveryDecision",
    "DiscoveryResult",
    "RunbookCandidate",
    "ScoreComponent",
    "build_candidate",
    "discover",
    "rank_candidates",
    "score_candidate",
    "specificity",
]
