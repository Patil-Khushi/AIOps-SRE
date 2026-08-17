"""Historical outcome memory — recall, weighting, and the promotion lifecycle.

What may become memory
----------------------
Only an :class:`RCAOutcome` whose recovery was verified, or whose cause a human
corrected. A prediction is not knowledge: if predictions entered memory directly the
agent's own mistake becomes the prior that reproduces it, confidence compounds on
nothing, and the system gets more certain precisely where it is most wrong.
:func:`promote` is the only function that advances a lifecycle state, and it refuses
every input that has not been verified.

What memory may do to a conclusion
----------------------------------
Very little, by arithmetic rather than by instruction. A prior reaches the score
through one term capped at :data:`scoring.PRIOR_MAX` (0.10), below every
current-evidence increment, and :func:`scoring.score` cancels it outright when current
evidence contradicts the hypothesis. Three further attenuations happen *here*, before
the cap even applies:

* **Reliability.** A pattern that has been right 15 of 17 times and one that has been
  right once are both "seen before"; only its track record separates them. An
  ungraded pattern is discounted to :data:`UNPROVEN_RELIABILITY` rather than trusted.
* **Freshness.** A verified outcome from a since-rearchitected service is not evidence
  about the service running now, so influence halves every
  :data:`MEMORY_HALF_LIFE_DAYS` and priors past :data:`MEMORY_STALE_DAYS` are dropped.
* **Refutation.** A pattern rejected more often than confirmed weighs less than one
  never seen at all.

Where memory may come from
--------------------------
:data:`OUTCOME_BACKED_PROVIDERS` — an allowlist, not a chain. The platform's default
history provider searches the repository's truth files, and those files carry a
``root_cause`` field that ``corpus.py`` maps onto ``recorded_cause``. For the
ecommerce scenarios that field is the graded answer to the evaluation about to be
run, so recalling it would measure lookup rather than diagnosis. Any configured
provider outside the allowlist is **refused and reported**, so the leak cannot be
reintroduced by configuration — see ``tests/test_rca_memory_blindness.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents.rca_agent.investigation import scoring
from agents.rca_agent.investigation.models import (
    EvidenceMatrix,
    HistoricalInfluence,
    HistoricalPrior,
    MemoryProvenance,
    MemoryReliability,
    MemoryStatus,
    RCAOutcome,
)

logger = logging.getLogger(__name__)

OUTCOME_BACKED_PROVIDERS = frozenset({"rca_outcomes"})
"""History providers whose records are verified incident *outcomes*.

The complete set of sources from which a prior may be built. Every other registered
provider searches the truth-file corpus, which for the ecommerce scenarios is the
evaluation's answer key. Membership is a claim about a provider's population, not
about its quality — ``embedding`` is a better retriever than this one and still may
not be used, because what it retrieves from is the problem.
"""

_ENV_PROVIDERS = "AIOPS_RCA_MEMORY_PROVIDERS"
_DEFAULT_PROVIDERS = "rca_outcomes"

UNPROVEN_RELIABILITY = 0.5
"""Weight for a pattern with no graded history.

Not 1.0: a first-ever recall is unproven and must not carry the weight of a pattern
with a track record. Not 0.0 either — that would make a correct first recall
worthless and memory could never start being useful. ``MemoryReliability.success_rate``
returns ``None`` rather than 0.0 for exactly this distinction; this constant is what
that ``None`` becomes, and what :data:`RELIABILITY_SMOOTHING` shrinks a thin record
*towards*.
"""

RELIABILITY_SMOOTHING = 2.0
"""Pseudo-observations pulling a thin track record toward ``UNPROVEN_RELIABILITY``.

Without this, ``success_rate`` was a raw ratio and one verified occurrence scored
1.0 — identical to fifteen-of-seventeen. So the first time a pattern was ever
confirmed it immediately carried maximum prior weight, which is precisely the
over-trust ``UNPROVEN_RELIABILITY`` exists to prevent; the constant was simply
unreachable, because a recalled row always has at least one occurrence of its own
pair. Shrinkage makes the ramp gradual: 1-of-1 earns 0.67, 15-of-17 earns 0.84.

Deliberately *not* applied when a pattern has never been confirmed —
:func:`reliability_weight` returns a hard 0.0 there. "We have checked this pattern
and it has been wrong every time" is a strong, clean statement, and smoothing it up
to 0.17 would keep a discredited pattern nudging live rankings forever.
"""

MEMORY_HALF_LIFE_DAYS = 30.0
"""Influence halves every 30 days. Chosen against deployment cadence rather than
statistics: a service that has been released a dozen times since an incident is not
obviously the same service, and 30 days is roughly where that becomes true here."""

MEMORY_STALE_DAYS = 180.0
"""Beyond this a prior is dropped entirely rather than merely attenuated.

A floor on decay would leave ancient outcomes nudging a live ranking forever, and
"we saw this two years ago" is not a reason to prefer one hypothesis today. Dropped
priors are still counted in ``priors_considered`` and named in the note, so the
exclusion is visible rather than silent."""

MIN_PRIOR_SIMILARITY = 0.15
"""Retrieval floor. Below this a match is shared vocabulary rather than a
recurrence, and presenting it as precedent is worse than returning nothing."""

MAX_PRIORS = 5
"""Cap on priors carried into scoring. The score uses only the single best
similarity, so a long tail adds report noise without changing arithmetic."""


@dataclass(frozen=True)
class MemoryRecall:
    """The result of one recall attempt, including its own blind spots.

    ``status`` distinguishes the four outcomes that a bare empty list would collapse:
    ``disabled`` (no provider configured — a deliberate cold start), ``unavailable``
    (the store could not be read, which is *not* an empty history), ``empty`` (asked,
    nothing similar) and ``recalled``. Collapsing these is how an unreadable database
    starts looking like a service with no history of problems.
    """

    status: str = "disabled"
    priors: tuple[HistoricalPrior, ...] = ()
    providers_used: tuple[str, ...] = ()
    providers_refused: tuple[str, ...] = ()
    """Configured names rejected for not being outcome-backed. Surfaced rather than
    silently skipped, so a misconfiguration is visible to whoever set it."""

    corpus_size: int | None = None
    considered: int = 0
    """How many matches came back before eligibility, staleness and similarity
    filtering. The denominator for every claim about what memory contributed."""

    dropped_stale: tuple[str, ...] = ()
    dropped_unverified: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return bool(self.priors)


def memory_providers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve the configured provider list into ``(allowed, refused)``.

    Read per call, not at import, matching ``aiops/context/config.py`` — an
    import-time env read is what broke ``monkeypatch`` in an earlier RA-007 bug, and
    cold-start vs learning is exactly the switch a test needs to flip.

    An explicitly empty value means cold start and is honoured as a deliberate
    choice, not overridden by the default.
    """
    raw = os.environ.get(_ENV_PROVIDERS)
    if raw is None:
        raw = _DEFAULT_PROVIDERS
    names = [n.strip() for n in raw.split(",") if n.strip()]
    allowed = tuple(n for n in names if n in OUTCOME_BACKED_PROVIDERS)
    refused = tuple(n for n in names if n not in OUTCOME_BACKED_PROVIDERS)
    return allowed, refused


def _age_days(recorded_at: datetime | None, *, now: datetime | None = None) -> float | None:
    if recorded_at is None:
        return None
    reference = now or datetime.now(UTC)
    stamp = recorded_at if recorded_at.tzinfo else recorded_at.replace(tzinfo=UTC)
    return max(0.0, (reference - stamp).total_seconds() / 86400.0)


def freshness_weight(age_days: float | None) -> float:
    """Exponential decay on a 30-day half-life; ``1.0`` when the age is unknown.

    Unknown age is *not* treated as ancient. A record with no timestamp is a gap in
    provenance, and inventing a penalty for it would be as unfounded as inventing a
    date — the missing timestamp is instead reported on the prior's provenance so a
    reader can discount it themselves.
    """
    if age_days is None:
        return 1.0
    if age_days >= MEMORY_STALE_DAYS:
        return 0.0
    return round(0.5 ** (age_days / MEMORY_HALF_LIFE_DAYS), 4)


def reliability_weight(reliability: MemoryReliability) -> float:
    """Track-record multiplier in ``[0, 1]``, shrunk toward "unproven".

    Three regimes, and the middle one is the reason this is not just
    ``success_rate``:

    * **No history at all** — ``UNPROVEN_RELIABILITY``. ``success_rate`` returns
      ``None`` rather than 0.0 for exactly this case.
    * **Some history** — the confirmed ratio pulled toward ``UNPROVEN_RELIABILITY``
      by ``RELIABILITY_SMOOTHING`` pseudo-observations, so a pattern confirmed once
      does not carry the weight of one confirmed fifteen times.
    * **Never confirmed** — a hard 0.0. Smoothing a discredited pattern back up to a
      small positive weight would leave it nudging rankings indefinitely.
    """
    if reliability.occurrences <= 0:
        return UNPROVEN_RELIABILITY
    if reliability.verified_correct <= 0:
        return 0.0
    shrunk = (reliability.verified_correct + UNPROVEN_RELIABILITY * RELIABILITY_SMOOTHING) / (
        reliability.occurrences + RELIABILITY_SMOOTHING
    )
    return round(min(1.0, shrunk), 4)


def _reliability_for(
    rows: list[dict[str, Any]], *, hypothesis_class: str | None, service: str, now: datetime | None
) -> MemoryReliability:
    """Track record of one ``(service, hypothesis class)`` pair across all outcomes.

    Computed over every row for the pair, including ones too stale or dissimilar to
    become priors themselves: a pattern's history is its history regardless of which
    records happened to match this incident's signatures.
    """
    if not hypothesis_class:
        return MemoryReliability()
    related = [
        r
        for r in rows
        if r.get("selected_hypothesis_class") == hypothesis_class
        and str(r.get("affected_service") or "") == service
    ]
    if not related:
        return MemoryReliability()

    verified = sum(
        1
        for r in related
        if r.get("verification_result") == "resolved" and not r.get("human_corrected_root_cause")
    )
    # A human correction is a rejection of the prediction *and* verified knowledge in
    # its own right. Counted here as a rejection because this figure weights the
    # pattern's reliability, and "a human had to fix our answer" is evidence against
    # trusting it again.
    rejected = sum(
        1
        for r in related
        if r.get("human_corrected_root_cause") or r.get("verification_result") == "not_resolved"
    )
    ages = [_age_days(_parse_dt(r.get("recorded_at")), now=now) for r in related]
    fresh = [a for a in ages if a is not None]
    return MemoryReliability(
        occurrences=len(related),
        verified_correct=verified,
        rejected=rejected,
        freshness_days=round(min(fresh), 2) if fresh else None,
        superseded_by=tuple(str(r["superseded_by"]) for r in related if r.get("superseded_by")),
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def recall(
    *,
    service: str,
    signatures: list[str],
    exclude_incident_ids: tuple[str, ...] = (),
    limit: int = MAX_PRIORS,
    now: datetime | None = None,
) -> MemoryRecall:
    """Retrieve weighted priors for one incident. Never raises.

    Returns a ``MemoryRecall`` whose ``status`` says which of the four outcomes
    happened. A recall failure degrades to no priors and a recorded reason, never to
    an exception — memory is an enrichment, and an unreadable store must cost a hint
    rather than a verdict.
    """
    allowed, refused = memory_providers()
    notes: list[str] = []
    if refused:
        notes.append(
            f"refused non-outcome-backed provider(s) {', '.join(refused)}: only "
            f"{', '.join(sorted(OUTCOME_BACKED_PROVIDERS))} may supply RCA priors, because "
            "the other providers search the truth-file corpus"
        )
    if not allowed:
        notes.append(
            "no outcome-backed history provider configured: running cold-start, with no priors"
        )
        return MemoryRecall(status="disabled", providers_refused=refused, notes=tuple(notes))
    if not signatures:
        notes.append("no observable signature to match on, so no recall was attempted")
        return MemoryRecall(
            status="empty",
            providers_used=allowed,
            providers_refused=refused,
            notes=tuple(notes),
        )

    try:
        from aiops.state.repository import RECALLABLE_MEMORY_STATUSES, list_rca_outcomes
        from aiops.tools.incident_history import RetrievalQuery
        from aiops.tools.incident_history.retriever import _PROVIDERS

        # All rows, for track-record arithmetic. Separate from the retrieval below
        # because reliability is a property of the pattern's whole history, not of the
        # subset that matched this incident.
        all_rows = list_rca_outcomes(
            statuses=RECALLABLE_MEMORY_STATUSES,
            exclude_incident_ids=exclude_incident_ids,
            limit=500,
        )

        query = RetrievalQuery(
            service=service,
            signatures=list(signatures),
            services_involved=[service] if service else [],
            limit=max(1, limit) * 3,
            min_similarity=MIN_PRIOR_SIMILARITY,
        )
        matches: list[Any] = []
        used: list[str] = []
        corpus_size: int | None = None
        unavailable: list[str] = []
        for name in allowed:
            provider = _PROVIDERS.get(name)
            if provider is None:
                unavailable.append(name)
                continue
            result = provider.search(query)
            corpus_size = result.corpus_size if corpus_size is None else corpus_size
            if result.status.value == "unavailable":
                unavailable.append(name)
                notes.append(f"{name}: {result.note or result.error or 'unavailable'}")
                continue
            used.append(name)
            matches.extend(result.matches)
    except Exception as exc:  # pragma: no cover - defensive; memory is an enrichment
        logger.debug("rca memory recall failed (%s)", exc)
        return MemoryRecall(
            status="unavailable",
            providers_refused=refused,
            notes=(*notes, f"recall failed: {type(exc).__name__}: {exc}"),
        )

    if not used:
        return MemoryRecall(
            status="unavailable",
            providers_refused=refused,
            notes=(*notes, "no outcome-backed provider could be read"),
        )

    excluded = {i for i in exclude_incident_ids if i}
    priors: list[HistoricalPrior] = []
    stale: list[str] = []
    unverified: list[str] = []
    considered = 0
    for match in matches:
        if match.incident_id in excluded:
            continue
        considered += 1
        resolution = match.resolution
        hypothesis_class = getattr(resolution, "recorded_hypothesis_class", None)

        # A record that reached here should already be verified — the provider filters
        # on status. Re-checked because a second provider could be added later, and
        # "the other layer validates it" is how an unverified prediction eventually
        # becomes a prior.
        if resolution is None or not resolution.resolved:
            unverified.append(match.incident_id)
            continue

        reliability = _reliability_for(
            all_rows, hypothesis_class=hypothesis_class, service=service, now=now
        )
        age = _age_days(match.occurred_at, now=now)
        fresh_w = freshness_weight(age)
        if fresh_w <= 0.0:
            stale.append(match.incident_id)
            continue

        rel_w = reliability_weight(reliability)
        weighted = round(match.similarity_score * rel_w * fresh_w, 4)
        if weighted < MIN_PRIOR_SIMILARITY:
            # Attenuation, not retrieval, pushed it below the floor. Distinguished in
            # the note because "we know this pattern and it has been wrong" is a
            # different fact from "nothing similar was found".
            stale.append(match.incident_id)
            continue

        matched_on = [f"service:{service}"] if match.matching_services else []
        matched_on += [f"signature:{s}" for s in match.matching_signatures]
        if hypothesis_class:
            matched_on.append(f"class:{hypothesis_class}")

        row = next((r for r in all_rows if r.get("incident_id") == match.incident_id), {})
        priors.append(
            HistoricalPrior(
                memory_id=match.incident_id,
                # Verified is the floor for recall; trusted is earned by repetition.
                status=(
                    MemoryStatus.TRUSTED
                    if str(row.get("memory_status")) == MemoryStatus.TRUSTED.value
                    else MemoryStatus.VERIFIED
                ),
                similarity=min(1.0, weighted),
                recorded_cause=resolution.recorded_cause,
                matched_on=tuple(matched_on),
                reliability=reliability,
                provenance=MemoryProvenance(
                    source_incident_ids=(match.incident_id,),
                    recorded_at=match.occurred_at,
                    verification_result=str(row.get("verification_result") or "") or None,
                    human_corrected=bool(row.get("human_corrected_root_cause")),
                    action_ref=row.get("action_key"),
                    recovery_result=str(row.get("verification_result") or "") or None,
                ),
            )
        )

    priors.sort(key=lambda p: (-p.similarity, p.memory_id))
    priors = priors[: max(1, limit)]

    if stale:
        notes.append(
            f"{len(stale)} recalled outcome(s) dropped as stale or too weakly reliable "
            f"to carry a prior: {', '.join(sorted(stale)[:5])}"
        )
    if unverified:
        notes.append(
            f"{len(unverified)} recalled outcome(s) dropped as unverified: "
            f"{', '.join(sorted(unverified)[:5])}"
        )

    return MemoryRecall(
        status="recalled" if priors else "empty",
        priors=tuple(priors),
        providers_used=tuple(used),
        providers_refused=refused,
        corpus_size=corpus_size,
        considered=considered,
        dropped_stale=tuple(sorted(stale)),
        dropped_unverified=tuple(sorted(unverified)),
        notes=tuple(notes),
    )


def priors_for(
    hypothesis_class: str, priors: tuple[HistoricalPrior, ...]
) -> tuple[HistoricalPrior, ...]:
    """Priors that concern one failure *class*.

    Matched on the recorded class rather than by comparing the remembered cause text
    against the candidate's description. Keyword-matching prose would attach "Redis
    connection pool exhausted" to a CPU-saturation hypothesis on the strength of a shared
    word, and a prior attached to the wrong candidate is worse than no prior at all. A
    record with no recorded class attaches to nothing.

    The class is ``Hypothesis.category`` (equal to the catalog rule id) and emphatically
    **not** ``Hypothesis.hypothesis_id``: that is ``digest(incident_id, rule_id)``, so it
    is unique per incident. Keying on it matched nothing across incidents, and memory
    measurably did nothing while looking correctly wired — priors were retrieved,
    attenuated, reported as eligible, and then attached to no hypothesis at all.
    """
    token = f"class:{hypothesis_class}"
    return tuple(p for p in priors if token in p.matched_on)


def attach_priors(
    matrices: list[EvidenceMatrix], recall_result: MemoryRecall
) -> list[EvidenceMatrix]:
    """Return matrices with their matching priors attached. Pure."""
    if not recall_result.priors:
        return list(matrices)
    out: list[EvidenceMatrix] = []
    for matrix in matrices:
        matching = priors_for(matrix.hypothesis.category, recall_result.priors)
        out.append(matrix.model_copy(update={"priors": matching}) if matching else matrix)
    return out


def _prior_delta(matrix: EvidenceMatrix | None) -> float:
    if matrix is None or matrix.score is None:
        return 0.0
    return sum(f.delta for f in matrix.score.factors if f.rule_id == "historical_prior")


def _cancelled_memory_ids(matrices: list[EvidenceMatrix]) -> tuple[str, ...]:
    """Memory ids whose prior was cancelled because current evidence contradicted it.

    Read off the ``unapplied`` rules rather than recomputed, so the audit record and
    the arithmetic cannot disagree about whether "current evidence wins" happened.
    """
    ids: list[str] = []
    for matrix in matrices:
        if matrix.score is None:
            continue
        cancelled = any(
            rule.rule_id == "historical_prior" and "cancelled" in rule.reason
            for rule in matrix.score.unapplied
        )
        if cancelled:
            ids.extend(p.memory_id for p in matrix.priors)
    return tuple(sorted(set(ids)))


def influence(
    ranked: list[EvidenceMatrix],
    recall_result: MemoryRecall | None,
    *,
    ranked_without_memory: list[EvidenceMatrix] | None = None,
) -> HistoricalInfluence:
    """State openly what history did to this ranking.

    ``changed_ranking`` is the honest measure and the reason this function takes both
    rankings: a prior that moved a score by 0.04 without changing which hypothesis
    won did not change the answer, and reporting "moderate influence" for it would
    overstate memory's role. Scoring is pure, so ranking twice is cheap and the
    comparison is exact rather than inferred.
    """
    if recall_result is None:
        return HistoricalInfluence(note="historical memory was not consulted")
    if recall_result.status == "disabled":
        return HistoricalInfluence(
            note="; ".join(recall_result.notes) or "cold start: no memory provider configured"
        )
    if not recall_result.priors:
        # The status sentence leads, and the provider's own notes follow it. Ordered this
        # way deliberately: with the notes first, a detailed provider message ("store
        # unreadable") crowded out the distinction that matters to a reader, and
        # "unavailable" then looked indistinguishable from "no history".
        headline = (
            "no verified outcome was similar enough to carry a prior"
            if recall_result.status == "empty"
            else "memory was unavailable, which is not the same as no history"
        )
        return HistoricalInfluence(
            priors_considered=recall_result.considered,
            note="; ".join([headline, *recall_result.notes]),
        )

    applied = tuple(
        p.memory_id
        for m in ranked
        for p in m.priors
        if m.score and any(f.rule_id == "historical_prior" for f in m.score.factors)
    )
    overridden = _cancelled_memory_ids(ranked)
    delta = _prior_delta(ranked[0] if ranked else None)

    changed = False
    if ranked_without_memory and ranked:
        before = ranked_without_memory[0].hypothesis.hypothesis_id
        after = ranked[0].hypothesis.hypothesis_id
        changed = before != after

    if changed:
        level: str = "strong"
    elif delta >= 0.07:
        level = "moderate"
    elif delta > 0.0:
        level = "weak"
    else:
        level = "none"

    parts = [
        f"{len(recall_result.priors)} verified prior(s) considered, "
        f"{len(applied)} applied, contributing {delta:+.3f} to the top hypothesis "
        f"(hard cap {scoring.PRIOR_MAX})"
    ]
    if changed:
        parts.append(
            f"memory changed the ranking: without priors the top hypothesis would be "
            # The *class*, not the id — an operator reading this needs "crash loop", not a
            # 16-character digest.
            f"{ranked_without_memory[0].hypothesis.category!r}"  # type: ignore[index]
        )
    elif ranked_without_memory:
        parts.append("memory did not change which hypothesis ranked first")
    if overridden:
        parts.append(
            f"{len(overridden)} prior(s) cancelled because current evidence "
            f"contradicts them: {', '.join(overridden)}"
        )
    parts.extend(recall_result.notes)

    return HistoricalInfluence(
        level=level,  # type: ignore[arg-type]
        priors_considered=recall_result.considered,
        priors_eligible=len(recall_result.priors),
        priors_applied=tuple(sorted(set(applied))),
        overridden_by_current_evidence=overridden,
        changed_ranking=changed,
        note="; ".join(parts),
    )


# ─── the promotion lifecycle ────────────────────────────────────────────────

TRUST_THRESHOLD = 3
"""Verified recurrences of one ``(service, hypothesis)`` pair before it is trusted.

``TRUSTED`` differs from ``VERIFIED`` only in that repetition has corroborated it.
Three rather than two because two occurrences of anything is barely a pattern, and
the difference this state makes is small by design — both are recallable, and the
cap applies either way.
"""


def promote(
    outcome: RCAOutcome,
    *,
    verified_recurrences: int = 0,
    superseded: bool = False,
    invalidated: bool = False,
) -> MemoryStatus:
    """The single decision point for a memory's lifecycle state.

    ``NEW -> UNVERIFIED -> VERIFIED -> TRUSTED``, with ``SUPERSEDED`` and
    ``INVALIDATED`` as terminal retractions. Deliberately the *only* function that
    returns a ``MemoryStatus``, and it is pure — a promotion that could happen in
    several places is a promotion whose rules will eventually differ between them.

    ``eligible_for_memory`` is the gate: verifier-confirmed recovery, or a human
    correction. A confident, approved, executed prediction with no verification stays
    ``UNVERIFIED`` no matter how plausible it looked, which is the whole point.
    """
    if invalidated:
        return MemoryStatus.INVALIDATED
    if superseded:
        return MemoryStatus.SUPERSEDED
    if not outcome.eligible_for_memory:
        # Distinguishes "we have a record but nothing corroborates it" from "we have a
        # record and an action was taken but recovery was never confirmed".
        return (
            MemoryStatus.UNVERIFIED
            if outcome.verification_result != "not_run" or outcome.executed_action
            else MemoryStatus.NEW
        )
    if verified_recurrences >= TRUST_THRESHOLD:
        return MemoryStatus.TRUSTED
    return MemoryStatus.VERIFIED


def record_outcome(outcome: RCAOutcome, *, verified_recurrences: int = 0) -> int | None:
    """Persist one outcome with its promoted status. Returns the row id, or ``None``.

    The write path memory grows through, and the *only* one: everything that decides
    whether a record may later influence a ranking happens in :func:`promote` above,
    so there is no way to insert a row that is recallable without having gone through
    it. Returns ``None`` on any storage failure — recording an outcome is bookkeeping
    and must never cost the incident response that produced it.
    """
    status = promote(outcome, verified_recurrences=verified_recurrences)
    try:
        from aiops.state import init_db
        from aiops.state.repository import save_rca_outcome

        init_db()
        return save_rca_outcome(
            incident_id=outcome.incident_id,
            affected_service=outcome.affected_service,
            predicted_root_cause=outcome.predicted_root_cause,
            predicted_status=outcome.predicted_status.value,
            confidence=outcome.confidence,
            selected_hypothesis_id=outcome.selected_hypothesis_id,
            selected_hypothesis_class=outcome.selected_hypothesis_class,
            action_key=outcome.action_key,
            human_decision=outcome.human_decision,
            verification_result=outcome.verification_result,
            human_corrected_root_cause=outcome.human_corrected_root_cause,
            memory_status=status.value,
            signatures=list(outcome.extra.get("signatures") or []),
            outcome=outcome.model_dump(mode="json"),
            recorded_at=outcome.recorded_at,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("rca outcome not recorded (%s)", exc)
        return None


__all__ = [
    "MAX_PRIORS",
    "MEMORY_HALF_LIFE_DAYS",
    "MEMORY_STALE_DAYS",
    "MIN_PRIOR_SIMILARITY",
    "OUTCOME_BACKED_PROVIDERS",
    "TRUST_THRESHOLD",
    "UNPROVEN_RELIABILITY",
    "MemoryRecall",
    "attach_priors",
    "freshness_weight",
    "influence",
    "memory_providers",
    "priors_for",
    "promote",
    "recall",
    "record_outcome",
    "reliability_weight",
]
