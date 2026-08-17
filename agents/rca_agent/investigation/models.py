"""Data contracts for the RCA investigation pipeline.

Phase 1: types only, no logic. Every stage module in later phases is written
against these, so the shape the LLM prompt, the dashboard, the eval harness and the
outcome store all depend on is settled before anything computes against it.

Three conventions inherited from the rest of the repo rather than reinvented here:

* **Frozen, tuple-valued models.** Same discipline as ``aiops/context/pack.py``:
  ``frozen=True`` alone only locks attribute rebinding, so a ``list`` field would
  still be mutable in place. An investigation artifact is a record handed to a
  prompt, a human and an audit log at once; a consumer that can edit it after the
  fact makes the audit trail worthless. The one deliberate exception is
  :class:`InvestigationBudget`, which is *tracking state* rather than a record.

* **Absent is not empty.** ``aiops.context.SectionStatus`` already draws this
  distinction per *section*. :class:`EvidenceStance` draws it per *hypothesis*,
  which is the level at which the RCA reasons — see that class for why the two are
  not the same question.

* **Every score explains itself.** ``agents/log_correlation/confidence.py``
  established that a number handed to a human or a prompt must be able to say where
  it came from, and ``aiops/context/ranker.py`` made ``rationale`` mandatory for the
  same reason. :class:`HypothesisScore` follows it.

On redeclaring the score-breakdown shape
----------------------------------------
RA-007's ``ConfidenceBreakdown`` is very close to :class:`HypothesisScore`, and is
deliberately not imported. Two sellable agents may not depend on each other's
internals (CLAUDE.md principle #2) — the one sanctioned cross-agent import in this
repo is RA-008 orchestrating the agents it chains, not one agent borrowing
another's reasoning model. The *algorithms* also genuinely differ: RA-007 scores
"how sure am I about this correlation", this scores "how well does this hypothesis
explain the evidence", and collapsing them would force one set of rules to serve
both questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── vocabulary ─────────────────────────────────────────────────────────────


class EvidenceStance(StrEnum):
    """How one piece of evidence bears on one hypothesis.

    The four non-committal values are the point of this enum. A hypothesis that
    "CPU saturation caused this" is refuted by *a CPU reading of 35%* and is merely
    unproven by *no CPU data at all*, and a system that stores both as "no
    supporting evidence" will confidently rule out a cause it never checked.

    Why this is not just ``aiops.context.SectionStatus``
    ---------------------------------------------------
    ``SectionStatus`` answers "did the provider answer?" for a whole section.
    This answers "what did that answer mean for this hypothesis?" — and the two
    can disagree in both directions. A ``COLLECTED`` metrics section can still leave
    a specific hypothesis ``UNAVAILABLE`` (the section came back, but not the one
    series that would discriminate), and an ``EMPTY`` section is exactly what
    licenses ``CHECKED_ABSENT``. The stage that builds a matrix maps one onto the
    other explicitly; nothing infers it.
    """

    SUPPORTS = "supports"
    """Observed, and it is consistent with this hypothesis."""

    CONTRADICTS = "contradicts"
    """Observed, and it argues against this hypothesis. Not the absence of support —
    a positive finding pointing the other way."""

    CHECKED_ABSENT = "checked_absent"
    """The signal was queried, the source answered, and the condition is not
    present. **Real evidence against** any cause that would have produced it — this
    is what makes ``render()``'s "NONE — this signal was checked and was absent"
    line truthful, and it is only ever derived from a ``usable`` section."""

    UNAVAILABLE = "unavailable"
    """Could not be checked: no provider, no credentials, breaker open. Carries no
    information about the world. Never counts for or against anything."""

    NOT_REQUESTED = "not_requested"
    """Nobody asked for this signal, so no cost was paid and no claim is made.
    Distinct from ``UNAVAILABLE``: it is a scoping decision, not a failure, and it
    is the honest label for a signal a bounded investigation chose to skip."""

    FAILED = "failed"
    """The query was attempted and errored. Like ``UNAVAILABLE`` it is a gap, but
    it is the only one worth alerting on — a failing provider is a defect, an
    unconfigured one is a deployment choice."""

    @property
    def is_evidence(self) -> bool:
        """Whether this stance may move a hypothesis score at all.

        The three observational stances qualify; the three gaps do not. This is the
        single predicate that keeps "we did not look" out of the arithmetic, so
        scoring reads it rather than enumerating members itself.
        """
        return self in (
            EvidenceStance.SUPPORTS,
            EvidenceStance.CONTRADICTS,
            EvidenceStance.CHECKED_ABSENT,
        )

    @property
    def is_gap(self) -> bool:
        """Whether this stance records a hole in the investigation."""
        return not self.is_evidence


class RootCauseStatus(StrEnum):
    """How much the evidence actually settles the question.

    Exists so "we do not know" is a first-class outcome with its own name rather
    than a low number a consumer has to interpret. A confident wrong root cause
    costs an operator more than an honest abstention, and the truth files say so
    explicitly in their ``known_wrong_fixes`` sections.
    """

    CONFIRMED = "confirmed"
    """Discriminating evidence, corroborated across sources, nothing contradicting."""

    PROBABLE = "probable"
    """Best of several plausible explanations, but the evidence does not exclude
    the runners-up."""

    UNCERTAIN = "uncertain"
    """Hypotheses remain and the evidence does not separate them."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """Not enough was observable to rank anything. Distinct from ``UNCERTAIN``:
    there, we looked and the signals do not discriminate; here, we could not look.
    The correct outcome for a zero-evidence run, and never a cause label."""

    @property
    def is_actionable(self) -> bool:
        """Whether a one-click remediation should be offered at all.

        ``UNCERTAIN`` and ``INSUFFICIENT_EVIDENCE`` both mean the next step is a
        human looking, not a fix executing — so the UI has one predicate to read
        instead of a confidence threshold each caller picks for itself.
        """
        return self in (RootCauseStatus.CONFIRMED, RootCauseStatus.PROBABLE)


class MemoryStatus(StrEnum):
    """Lifecycle of one recorded incident outcome.

    ``NEW -> UNVERIFIED -> VERIFIED -> TRUSTED -> SUPERSEDED | INVALIDATED``

    The whole reason this is a lifecycle rather than a boolean: an RCA *prediction*
    is not knowledge. If predictions entered memory directly, the agent's own
    mistake becomes the prior that reproduces it, and confidence compounds on
    nothing. Only a verifier-confirmed recovery advances past ``UNVERIFIED``.
    """

    NEW = "new"
    """Recorded, nothing corroborates it yet."""

    UNVERIFIED = "unverified"
    """An outcome exists but recovery was never confirmed — a prediction plus an
    action, which is not the same as a resolved incident."""

    VERIFIED = "verified"
    """The resolution verifier confirmed recovery for this incident."""

    TRUSTED = "trusted"
    """Verified, and corroborated enough to carry a prior — repeated recurrence
    with the same pattern, or explicit human confirmation."""

    SUPERSEDED = "superseded"
    """A later outcome replaced this one. Kept, not deleted: the original
    prediction is part of the audit record."""

    INVALIDATED = "invalidated"
    """Proven wrong after the fact. Excluded from ranking, retained for provenance —
    silently deleting bad knowledge destroys the evidence that it was ever used."""

    @property
    def usable_for_ranking(self) -> bool:
        """Whether an entry in this state may influence hypothesis priors.

        Only ``VERIFIED`` and ``TRUSTED``. ``NEW``/``UNVERIFIED`` are unproven,
        ``SUPERSEDED``/``INVALIDATED`` are known stale or wrong.
        """
        return self in (MemoryStatus.VERIFIED, MemoryStatus.TRUSTED)


class BaselineStatus(StrEnum):
    """Whether "is this value abnormal?" could be answered at all.

    Without this, a metric is judged abnormal because its absolute value looks
    large — which is how a service that always runs at 800ms gets diagnosed with a
    latency regression. ``UNAVAILABLE`` must never read as "normal": absence of a
    baseline is absence of a comparison, not evidence of health.
    """

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    STALE = "stale"

    @property
    def comparable(self) -> bool:
        return self in (BaselineStatus.AVAILABLE, BaselineStatus.PARTIAL)


class ImpactState(StrEnum):
    """One service's position in the blast radius.

    ``OBSERVED_HEALTHY`` and ``NOT_OBSERVED`` are the pair that matters. Claiming a
    service is fine because no evidence was collected about it is the blast-radius
    equivalent of treating unavailable telemetry as a negative result, and it is
    how an incident report understates its own scope.
    """

    DIRECTLY_AFFECTED = "directly_affected"
    INDIRECTLY_AFFECTED = "indirectly_affected"
    OBSERVED_HEALTHY = "observed_healthy"
    """Telemetry was collected for this service and shows it working."""

    NOT_OBSERVED = "not_observed"
    """In the topology, but no telemetry was collected. Status unknown, and not a
    claim of health."""

    UNKNOWN = "unknown"
    """Not placeable at all — no topology, or the service is not in it."""


class TemporalRelation(StrEnum):
    """How an event sits in time relative to incident onset.

    Named states rather than a signed offset because the distinction the RCA must
    not blur is *precedes* versus *causes*. A deployment that precedes onset is a
    candidate; one that follows it is excluded as a cause outright, and that
    exclusion should be readable in the data rather than inferred from arithmetic
    at every call site.
    """

    PRECEDES_ONSET = "precedes_onset"
    AT_ONSET = "at_onset"
    FOLLOWS_ONSET = "follows_onset"
    UNKNOWN = "unknown"


TimelineSource = Literal[
    "alert",
    "metrics",
    "logs",
    "traces",
    "k8s_events",
    "deployment",
    "configuration",
    "dependency",
    "remediation",
    "verification",
]
"""Where a timeline event came from.

Names the kind of source, not the vendor, matching ``aiops.context.Source``.
Includes ``remediation`` and ``verification`` because the closed loop puts the fix
and its verification on the same timeline as the failure — which is what lets a
re-investigation after a failed recovery see that the action happened and did not
help.
"""


# ─── stage 1: scope ─────────────────────────────────────────────────────────


class IncidentScope(BaseModel):
    """What is being investigated, established before any cause is proposed.

    The point of making this an explicit artifact is the ``symptom`` /
    ``root cause`` split. "HTTP 500 rate increased" is an observation about the
    world; "the database is unreachable" is a claim about why. Collapsing them —
    which a one-shot prompt does by default, because the alert summary is the most
    salient text it is given — produces the classic non-answer *"root cause: HTTP
    500 errors"*. Separating them at the type level means the symptom has somewhere
    to live that is not the conclusion.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    affected_service: str
    severity: str

    user_visible_symptom: str
    """What a customer would notice, phrased as an effect. Never a cause."""

    alert_name: str | None = None
    alert_summary: str | None = None
    affected_endpoint: str | None = None
    affected_workload: str | None = None

    onset_at: datetime | None = None
    """Best estimate of when the incident began — not when the alert fired, which
    is delayed by the rule's ``for:`` clause. ``None`` when nothing datable was
    available, so a timeline can say so instead of anchoring on the alert and
    mislabelling every prior event as ``FOLLOWS_ONSET``."""

    observed_at: datetime | None = None
    current_state: str = "active"

    initial_blast_radius: tuple[str, ...] = ()
    """Services worth checking, from topology alone before any evidence is weighed.
    A starting set for investigation, not a finding."""

    correlation_id: str | None = None


# ─── stage 2: timeline ──────────────────────────────────────────────────────


class RcaTimelineEvent(BaseModel):
    """One dated thing that happened, as the RCA sees it.

    Named ``RcaTimelineEvent`` because this repo already has two timelines and a
    third name collision would be genuinely confusing: RA-007 owns ``TimelineEvent``
    (``agents/log_correlation/timeline.py``) and RA-008 owns ``TimelineEntry``
    (``agents/incident_commander/models.py``). RA-007's is not reused directly for
    the reason given in this module's docstring; where RA-007 has already built one,
    its output reaches RCA as a dict through the existing ``correlation`` parameter
    and is projected into this type.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    source: TimelineSource
    service: str
    event: str
    severity: str = "info"

    temporal_relation: TemporalRelation = TemporalRelation.UNKNOWN
    """Position relative to ``IncidentScope.onset_at``. Computed once here rather
    than re-derived by every consumer that needs to know whether an event could
    have been a cause."""

    is_change: bool = False
    """A human or system *change* (deploy, config, flag) rather than a symptom.

    Load-bearing and dangerous in equal measure: most outages follow a change, and
    a model given a recent deploy will blame it. Carrying this as a flag on a dated
    event — rather than as prose in a prompt — is what lets the scoring stage treat
    a change as one weighted factor subject to the correlation-is-not-causation
    rule, instead of as a conclusion.
    """

    occurrences: int = 1
    evidence_ids: tuple[str, ...] = ()
    """Back-links to the observations this entry was derived from, so a reader can
    reach the underlying log line. Empty is honest for deploy and config events —
    nothing in the telemetry produced them."""


class IncidentTimelineView(BaseModel):
    """The ordered account, with an explicit statement of what it could see."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[RcaTimelineEvent, ...] = ()
    onset_at: datetime | None = None

    sources_present: tuple[str, ...] = ()
    """Which sources actually contributed. Recorded because an absent source is
    ambiguous — no deployment events could mean nothing was deployed, or that
    Kubernetes was unreachable — and a reader must be able to tell which."""

    sources_unavailable: tuple[str, ...] = ()
    truncated: bool = False
    coverage_note: str | None = None

    @property
    def changes(self) -> tuple[RcaTimelineEvent, ...]:
        return tuple(e for e in self.events if e.is_change)

    @property
    def pre_onset_changes(self) -> tuple[RcaTimelineEvent, ...]:
        """Changes that could *temporally* have caused the incident.

        "Could have" is the whole claim. Membership here makes an event eligible
        for consideration; it is not evidence that it is responsible.
        """
        return tuple(
            e
            for e in self.changes
            if e.temporal_relation in (TemporalRelation.PRECEDES_ONSET, TemporalRelation.AT_ONSET)
        )


# ─── stage 3: baseline ──────────────────────────────────────────────────────


class BaselineComparison(BaseModel):
    """One metric now, against what normal looks like for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    status: BaselineStatus
    current_value: float | None = None
    baseline_value: float | None = None
    deviation_ratio: float | None = None
    """``current / baseline`` when both are known. ``None`` whenever the comparison
    could not be made — never ``1.0``, which would read as "normal"."""

    window_note: str | None = None
    is_abnormal: bool | None = None
    """Tri-state on purpose. ``None`` means "cannot say", which is a different
    answer from ``False`` ("say: this is normal") and the difference is exactly what
    stops an unavailable baseline being scored as evidence of health."""


# ─── stage 4: completeness ──────────────────────────────────────────────────


class InvestigationCompleteness(BaseModel):
    """How much of the evidence an investigation *wanted* it actually got.

    Reported alongside confidence rather than folded into it, because they answer
    different questions and the pair is what makes either interpretable:
    ``confidence 0.92 / completeness 0.40`` ("the little I saw was decisive") is a
    materially different verdict from ``0.92 / 0.95`` ("I looked everywhere"), and
    a single blended number hides which one the operator is being handed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    per_source: dict[str, str] = Field(default_factory=dict)
    """``source -> SectionStatus`` value, verbatim from the Context Pack so a
    reader can tell ``unavailable`` from ``empty`` per source."""

    overall: float = Field(ge=0.0, le=1.0, default=0.0)
    critical_gaps: tuple[str, ...] = ()
    """Sources whose absence materially weakens the conclusion — the "what would
    raise confidence" list, and the input to targeted retrieval."""

    note: str | None = None


# ─── stage 5: historical memory ─────────────────────────────────────────────


class MemoryProvenance(BaseModel):
    """Where one memory entry came from, in enough detail to audit or retract it.

    Every field is a pointer to something outside this record. That is deliberate:
    a learned fact whose justification cannot be followed back to an incident, an
    evidence id and a verification result is a rumour, and a memory store that
    cannot be audited cannot responsibly be invalidated either.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_incident_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    recorded_at: datetime | None = None
    verification_result: str | None = None
    human_confirmed: bool = False
    human_corrected: bool = False
    service_version: str | None = None
    topology_version: str | None = None
    action_ref: str | None = None
    """The runbook or fault key that was actually run, not one that was proposed."""

    recovery_result: str | None = None


class MemoryReliability(BaseModel):
    """Track record of one recalled pattern.

    A pattern that was right 15 of 17 times and one that was right once are both
    "seen before", and only this distinguishes them. ``freshness_days`` is carried
    beside the counts because a verified outcome from a since-rearchitected service
    is not evidence about the service running now.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurrences: int = 0
    verified_correct: int = 0
    rejected: int = 0
    freshness_days: float | None = None
    superseded_by: tuple[str, ...] = ()

    @property
    def success_rate(self) -> float | None:
        """``verified_correct / occurrences``, or ``None`` with no history.

        ``None`` rather than ``0.0``: a pattern nobody has graded is unproven, and
        scoring it as a total failure would bury a first-ever correct recall.
        """
        return round(self.verified_correct / self.occurrences, 4) if self.occurrences else None


class HistoricalPrior(BaseModel):
    """One past *verified* outcome, offered as a prior and nothing more.

    Mirrors ``aiops.tools.incident_history.ResolutionMetadata``'s discipline, which
    names its field ``recorded_cause`` rather than ``root_cause`` so no consumer
    mistakes history for a verdict. The same care is taken here, and the invariant
    is enforced further downstream too: this type has no field that can express a
    claim about the *current* incident.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str
    status: MemoryStatus
    similarity: float = Field(ge=0.0, le=1.0)
    recorded_cause: str | None = None
    matched_on: tuple[str, ...] = ()
    """Which dimensions matched — service, alert, signature, topology, timeline.
    Substance a reader can judge, rather than a bare score to trust."""

    reliability: MemoryReliability = Field(default_factory=MemoryReliability)
    provenance: MemoryProvenance = Field(default_factory=MemoryProvenance)

    @property
    def eligible(self) -> bool:
        """Whether this entry may influence ranking at all."""
        return self.status.usable_for_ranking


class HistoricalInfluence(BaseModel):
    """What history actually did to the ranking, stated openly.

    §27's requirement in type form: when a past incident materially moved a
    conclusion, the operator is told so and told how much. Hidden priors are the
    mechanism by which "we have seen this before" silently becomes "this is that",
    which is precisely the inference this whole design refuses to make.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["none", "weak", "moderate", "strong"] = "none"
    priors_considered: int = 0
    priors_eligible: int = 0
    priors_applied: tuple[str, ...] = ()
    overridden_by_current_evidence: tuple[str, ...] = ()
    """Memory ids whose prior was cancelled because current evidence contradicted
    them. The audit record for "current evidence wins" actually having happened —
    an empty tuple when history and evidence simply agreed."""

    changed_ranking: bool = False
    """Whether the priors changed *which* hypothesis ranked first.

    The only unambiguous measure of influence, and the reason it is a field rather
    than a derived number: a prior that moved a score by 0.04 without changing the
    outcome did not change the answer, and a level of "moderate" would overstate it.
    Computed by ranking twice — with priors and without — which is cheap because
    scoring is pure. This is also what makes memory influence *measurable* rather
    than asserted (see ``evals/rca_metrics.py``)."""

    note: str | None = None


# ─── stages 6-8: hypotheses, evidence matrix, scoring ───────────────────────


class EvidenceItem(BaseModel):
    """One observation, as it bears on one hypothesis.

    Deliberately *not* a second copy of ``aiops.context.Observation``: that records
    what was seen, this records what it means for a specific candidate cause. The
    same observation appears in several matrices with different stances, which is
    the same facts-versus-judgement separation ``RankedObservation`` makes by
    keeping a score out of ``Observation``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    stance: EvidenceStance
    statement: str
    """Operator-readable, and for observational stances quoted from the evidence
    the agent was actually given. A claim the agent cannot quote is a claim it
    should not make."""

    source: str = "unknown"
    signature: str | None = None
    observation_id: str | None = None
    """Link into the Context Pack, when the item came from one."""

    section_status: str | None = None
    """The ``SectionStatus`` this item's stance was derived from. Carried so a gap
    can be explained ("metrics section was unavailable") rather than merely
    reported."""

    topology_relation: str | None = None
    sources_agreeing: tuple[str, ...] = ()
    occurrences: int = 1
    temporal_relation: TemporalRelation = TemporalRelation.UNKNOWN
    observed_at: datetime | None = None

    @property
    def is_corroborated(self) -> bool:
        """Whether more than one independent source carries this signature.

        The strongest inference available: one backend reporting an error can be
        that backend's instrumentation, the same signature in logs *and* traces
        cannot. Matches ``correlator``'s own-source-inclusive convention, so the
        test is ``> 1`` rather than ``>= 1``.
        """
        return len(self.sources_agreeing) > 1


class ScoreFactor(BaseModel):
    """One rule's contribution to a hypothesis score, with its justification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    description: str
    delta: float
    """Signed. Negative for contradiction and staleness penalties — unlike RA-007's
    algorithm, this one has genuine negative terms, because a contradiction is a
    finding that must be able to push a hypothesis down rather than merely fail to
    lift it."""

    triggered_by: tuple[str, ...] = ()
    """Evidence ids, or a plain fact when the rule is about the *shape* of the
    evidence set rather than any single item."""


class UnappliedRule(BaseModel):
    """A rule that did not fire, and why.

    Usually the most useful line in an explanation: "confidence is 0.55 because no
    second source corroborated it" tells a responder what to go and look at, which
    a bare number never can.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    reason: str
    potential_delta: float


class HypothesisScore(BaseModel):
    """A hypothesis's score and the full derivation of it.

    The platform computes this; the LLM explains it. That split is the reason the
    type exists — a model free to state its own confidence will state a confident
    one, and there is no way to review a number that was asserted rather than
    derived.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    factors: tuple[ScoreFactor, ...] = ()
    unapplied: tuple[UnappliedRule, ...] = ()
    rule_trace: tuple[str, ...] = ()
    """Ordered log of every rule evaluation. The arithmetic audit: replaying it
    must reproduce ``score``, so a reader can verify the number instead of
    trusting it."""

    capped: bool = False
    explanation: str = ""


class Hypothesis(BaseModel):
    """One candidate explanation, specific enough to be tested.

    ``mechanism`` is required alongside ``label`` because a label alone cannot be
    checked. "Database problem" admits no discriminating query; "MySQL is
    unreachable from user-service, so /login returns 500" names the gauge to read
    and the log line to look for, and a hypothesis that cannot be refuted is not a
    hypothesis.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    label: str
    mechanism: str
    candidate_component: str | None = None
    """What is believed to be *at fault*, which is routinely not the service that
    alerted. An order-service timeout alert whose cause is the payment gateway has
    ``affected_service="order-service"`` and this set to the gateway; conflating
    the two is how a victim gets remediated instead of a cause."""

    category: str = "unknown"
    origin: Literal["catalog", "llm", "historical", "operator"] = "catalog"
    """Where the candidate came from. Recorded because an LLM-proposed hypothesis
    and a catalog one warrant different scrutiny, and because a historically
    suggested one must be visibly attributable to memory."""

    action_hint: str | None = None
    """A remediation vocabulary key this hypothesis *may* map to, subject to
    grounding. A hint, never an instruction — the action registry decides what is
    executable, and an unrecognised hint becomes a manual step."""


class EvidenceMatrix(BaseModel):
    """One hypothesis, everything bearing on it, and its score.

    The contradiction and gap lists are separate fields rather than a filtered view
    of one list, because they must be *populated by different work*. Supporting
    evidence accumulates on its own as observations are read; contradicting evidence
    only appears if something deliberately goes looking for it, and a matrix whose
    ``contradicting`` is empty because nobody looked is indistinguishable — without
    this shape — from one where nothing contradicts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis: Hypothesis
    supporting: tuple[EvidenceItem, ...] = ()
    contradicting: tuple[EvidenceItem, ...] = ()
    checked_absent: tuple[EvidenceItem, ...] = ()
    gaps: tuple[EvidenceItem, ...] = ()
    """Evidence that could not be obtained — ``UNAVAILABLE`` / ``NOT_REQUESTED`` /
    ``FAILED``. Never merged with ``checked_absent``: that pair is the whole
    negative-evidence distinction, and one list would erase it."""

    baseline: tuple[BaselineComparison, ...] = ()
    priors: tuple[HistoricalPrior, ...] = ()
    score: HypothesisScore | None = None
    contradiction_search_performed: bool = False
    """Whether refutation was actually attempted. Without this an empty
    ``contradicting`` list is ambiguous, and the optimistic reading of it is the
    one that produces confident wrong answers."""

    @property
    def sources_agreeing(self) -> tuple[str, ...]:
        """Distinct sources behind the supporting evidence, sorted."""
        return tuple(sorted({s for item in self.supporting for s in item.sources_agreeing}))

    @property
    def has_unresolved_gap(self) -> bool:
        return bool(self.gaps)


# ─── stage 9: causal chain and blast radius ─────────────────────────────────


class CausalChain(BaseModel):
    """root cause -> immediate effect -> service effect -> user impact.

    A chain rather than a label because a label cannot be checked against the
    evidence. "PostgreSQL unavailable" is unfalsifiable as a bare string; the
    chain that connects it to the HTTP 500s an operator is looking at either holds
    together against the timeline or visibly does not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_cause: str
    steps: tuple[str, ...] = ()
    user_impact: str | None = None
    evidence_ids: tuple[str, ...] = ()
    note: str | None = None


class ServiceImpact(BaseModel):
    """One service's place in the blast radius, and how that was determined."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    state: ImpactState
    relation: str | None = None
    """``self`` / ``dependency`` / ``dependent`` / ``unrelated`` / ``unknown``,
    the ``correlator``'s vocabulary."""

    hops: int | None = None
    evidence_ids: tuple[str, ...] = ()
    rationale: str = ""


class BlastRadiusReport(BaseModel):
    """Who else is affected — including an explicit list of who we cannot say about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impacts: tuple[ServiceImpact, ...] = ()
    affected_endpoints: tuple[str, ...] = ()
    topology_available: bool = False
    """False means every ``UNKNOWN`` below is a coverage gap rather than a finding,
    and a consumer must not render the report as a complete picture."""

    note: str | None = None

    def _of(self, state: ImpactState) -> tuple[str, ...]:
        return tuple(i.service for i in self.impacts if i.state is state)

    @property
    def directly_affected(self) -> tuple[str, ...]:
        return self._of(ImpactState.DIRECTLY_AFFECTED)

    @property
    def indirectly_affected(self) -> tuple[str, ...]:
        return self._of(ImpactState.INDIRECTLY_AFFECTED)

    @property
    def observed_healthy(self) -> tuple[str, ...]:
        """Services with telemetry showing them working. Deliberately *not* a
        catch-all for "everything else" — see ``not_observed``."""
        return self._of(ImpactState.OBSERVED_HEALTHY)

    @property
    def not_observed(self) -> tuple[str, ...]:
        return self._of(ImpactState.NOT_OBSERVED)

    @property
    def unknown(self) -> tuple[str, ...]:
        return self._of(ImpactState.UNKNOWN)


# ─── stage 10: recovery and risk ────────────────────────────────────────────


class RiskAssessment(BaseModel):
    """What could go wrong if this action is taken.

    Every field defaults to the *cautious* reading — ``None`` for the risk
    questions, ``False`` for the reassurances. An unassessed risk must never
    present as an assessed-absent one, because the operator reads this to decide
    whether to click Approve.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["low", "medium", "high", "unknown"] = "unknown"

    causes_downtime: bool | None = None
    interrupts_active_requests: bool | None = None
    risks_data_loss: bool | None = None
    risks_duplicate_transactions: bool | None = None
    affects_downstream: bool | None = None
    affects_upstream: bool | None = None
    destroys_evidence: bool | None = None
    """Whether running this would erase the telemetry needed to finish the
    investigation. A pod restart clears the crashed container's logs — which is
    why "just restart it" can cost the diagnosis, and why this is asked before
    the action rather than regretted after it."""

    reversible: bool = False
    rollback_available: bool = False
    safer_alternative: str | None = None
    concerns: tuple[str, ...] = ()
    rationale: str = ""

    @property
    def unassessed(self) -> tuple[str, ...]:
        """Risk questions that were never answered.

        Surfaced so a decision package can say "not assessed" out loud instead of
        rendering an unasked question as a cleared one.
        """
        questions = (
            "causes_downtime",
            "interrupts_active_requests",
            "risks_data_loss",
            "risks_duplicate_transactions",
            "affects_downstream",
            "affects_upstream",
            "destroys_evidence",
        )
        return tuple(q for q in questions if getattr(self, q) is None)


class RecoveryOption(BaseModel):
    """One candidate way to restore service, with its risk.

    Kept distinct from ``RankedFixStep`` (the wire contract) and from PRS-001's
    ``RemediationOption`` (a different agent's sellable output) because this is the
    investigation's *internal* candidate, before grounding decides whether any of
    it is executable. ``grounded`` and ``executable`` are separate flags for that
    reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    option_id: str
    description: str
    addresses_hypothesis_id: str | None = None
    why_it_addresses_the_cause: str = ""
    expected_effect: str = ""
    expected_recovery_seconds: int | None = None
    changes: str = ""
    dependencies: tuple[str, ...] = ()
    rollback: str = ""
    blast_radius: Literal["low", "medium", "high"] = "medium"
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)

    action_key: str | None = None
    """A key from the platform's action vocabulary, once grounding has confirmed
    it. ``None`` means no automated action — which is the correct outcome for a
    proposal the registry does not recognise."""

    grounded: bool = False
    """Whether ``action_key`` was checked against the live action registry rather
    than taken from the model's spelling."""

    executable: bool = False
    """Whether the platform has an executor for it. Grounded and executable are
    different facts: a real key with no wired executor is still a manual step."""

    requires_hitl: Literal[True] = True
    """Invariant, and typed so it cannot be deserialised away — the same defence
    ``RankedFixStep`` uses. Recovery is HITL-gated at the registry boundary; this
    field documents the contract, it does not enforce it."""


class VerificationPlan(BaseModel):
    """How we will know whether the action worked — written before it runs.

    Committing the success criteria up front is what makes verification a test
    rather than a rationalisation: an operator who has already seen the outcome can
    always find a reading of the metrics that agrees with it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    window_seconds: tuple[int, ...] = ()
    """Staged re-check offsets, matching ``resolution_verifier``'s existing
    60/180/300s convention rather than introducing a second cadence."""

    if_not_resolved: str = ""
    """What to do on failure. Never "retry the same action" — a fix that did not
    work is evidence the root cause was wrong or incomplete, and the loop goes back
    to investigation."""


# ─── budget ─────────────────────────────────────────────────────────────────


@dataclass
class InvestigationBudget:
    """Bounds on one investigation, and what it has spent.

    A mutable dataclass rather than a frozen model, deliberately: this is
    accounting state carried through the pipeline, not a record of what happened.
    Same line ``aiops/context/builder.py`` draws with ``ContextRequest`` — plain
    object for intra-process plumbing, validated frozen models at the boundary.

    Exists because an investigation that can always ask one more question will,
    and the failure mode is not a wrong answer but a request that never returns.
    When the budget is gone the honest outcome is ``INSUFFICIENT_EVIDENCE``, not a
    guess made to fill the silence.
    """

    max_iterations: int = 3
    max_additional_queries: int = 10
    max_llm_calls: int = 1
    """One on the normal path. Raising this is a deliberate cost decision, not a
    convenience — see the package docstring."""

    max_duration_seconds: float = 30.0

    iterations: int = 0
    additional_queries: int = 0
    llm_calls: int = 0
    elapsed_seconds: float = 0.0
    queried: set[str] = field(default_factory=set)
    """Fingerprints of retrievals already performed, so the loop cannot re-ask a
    question it has already answered — the cheapest way to make "do not repeatedly
    query the same source" structural rather than aspirational."""

    exhaustion_reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.exhaustion_reason is not None or any(
            (
                self.iterations >= self.max_iterations,
                self.additional_queries >= self.max_additional_queries,
                self.llm_calls >= self.max_llm_calls,
                self.elapsed_seconds >= self.max_duration_seconds,
            )
        )

    def may_query(self, fingerprint: str) -> bool:
        """Whether one more retrieval is allowed, and is not a repeat."""
        return not self.exhausted and fingerprint not in self.queried

    def record_query(self, fingerprint: str) -> None:
        self.queried.add(fingerprint)
        self.additional_queries += 1

    def exhaust(self, reason: str) -> None:
        """Stop the investigation with a stated reason.

        The reason is required because "the budget ran out" has to reach the
        decision trace: a verdict that abstained because it ran out of time is a
        different fact from one that abstained because the evidence was mute, and
        an operator deciding what to do next needs to know which.
        """
        self.exhaustion_reason = reason


# ─── outcome ────────────────────────────────────────────────────────────────


class RCAOutcome(BaseModel):
    """What actually happened to one RCA prediction, end to end.

    The record that closes the loop, and the only thing eligible to become memory.
    ``predicted_root_cause`` and ``human_corrected_root_cause`` are separate fields
    for a reason worth stating plainly: overwriting a prediction with the truth
    destroys the only data that would show the agent was wrong. Both are kept, and
    calibration is measured from the pair.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    affected_service: str
    recorded_at: datetime

    predicted_root_cause: str
    predicted_status: RootCauseStatus
    confidence: float = Field(ge=0.0, le=1.0)
    selected_hypothesis_id: str | None = None
    """Points at the specific matrix inside *that* incident's investigation. Not usable
    as a cross-incident key — it is ``digest(incident_id, rule_id)``."""

    selected_hypothesis_class: str | None = None
    """The failure *class* concluded, i.e. ``Hypothesis.category`` / the catalog rule id.

    The key historical memory joins on, and the reason it is a separate field:
    ``selected_hypothesis_id`` is incident-scoped, so recalling against it matched nothing
    across incidents and memory did nothing while looking correctly wired."""

    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    rejected_hypothesis_ids: tuple[str, ...] = ()

    recommended_action: str | None = None
    action_key: str | None = None
    human_decision: Literal["approved", "denied", "expired", "not_requested"] = "not_requested"
    approver: str | None = None
    executed_action: str | None = None
    execution_result: str | None = None

    verification_result: Literal["resolved", "partially_resolved", "not_resolved", "not_run"] = (
        "not_run"
    )
    time_to_recovery_seconds: float | None = None

    human_corrected_root_cause: str | None = None
    """Set only when a human said the RCA was wrong and supplied the real cause.
    The highest-value feedback the system can receive, and never written over
    ``predicted_root_cause``."""

    final_outcome: str | None = None
    memory_status: MemoryStatus = MemoryStatus.NEW
    provenance: MemoryProvenance = Field(default_factory=MemoryProvenance)
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def eligible_for_memory(self) -> bool:
        """Whether this outcome may be promoted past ``UNVERIFIED``.

        Requires the verifier to have confirmed recovery, or a human to have
        supplied a correction — a correction is itself verified knowledge, and is
        the one path by which a *failed* prediction still teaches something. A
        prediction with no verification is never eligible, whatever its confidence.
        """
        return self.verification_result == "resolved" or self.human_corrected_root_cause is not None


class Investigation(BaseModel):
    """Everything the deterministic stages concluded, before the LLM says a word.

    This is the object the prompt is built from and the verdict is derived from, and it
    is the reason the LLM's role changes from *deciding* to *explaining*: by the time it
    is called, the hypotheses exist, the evidence is classified, the scores are computed
    and the status is settled. A model that disagrees with the ranking can say so in
    prose, but it cannot move the number.

    ``matrices`` is ranked best-first, and that ordering is meaningful — index 0 is the
    hypothesis the evidence favours.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: IncidentScope
    timeline: IncidentTimelineView = Field(default_factory=IncidentTimelineView)
    completeness: InvestigationCompleteness = Field(default_factory=InvestigationCompleteness)
    baselines: tuple[BaselineComparison, ...] = ()
    matrices: tuple[EvidenceMatrix, ...] = ()

    status: RootCauseStatus = RootCauseStatus.INSUFFICIENT_EVIDENCE
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    selected_hypothesis_id: str | None = None
    discriminated: bool = False
    """Whether the top hypothesis was meaningfully ahead of the runner-up. ``False`` with
    candidates present is what makes the difference between ``PROBABLE`` and
    ``UNCERTAIN``."""

    historical_influence: HistoricalInfluence = Field(default_factory=HistoricalInfluence)

    blast_radius: BlastRadiusReport | None = None
    """Who else is affected, and who was never looked at. ``None`` when the stage did
    not run — distinct from a report whose ``impacts`` happen to be empty."""

    recovery_options: tuple[RecoveryOption, ...] = ()
    """Ranked recovery options, each carrying its own grounded/executable split. Empty
    when no cause was established: there is nothing to plan a recovery for."""

    verification: VerificationPlan | None = None
    """What to re-check after a fix. Populated from the evidence that established the
    cause, so the signal that raised the incident is the signal that closes it."""

    budget: str | None = None
    """Why the investigation stopped, when it stopped early. ``None`` means it ran to
    completion."""

    notes: tuple[str, ...] = ()

    @property
    def selected(self) -> EvidenceMatrix | None:
        """The favoured hypothesis, or ``None`` when nothing was proposed."""
        if not self.matrices:
            return None
        if self.selected_hypothesis_id is None:
            return self.matrices[0]
        return next(
            (m for m in self.matrices if m.hypothesis.hypothesis_id == self.selected_hypothesis_id),
            self.matrices[0],
        )

    @property
    def rejected(self) -> tuple[EvidenceMatrix, ...]:
        """Every hypothesis that was considered and not selected.

        Exposed because "why the others lost" is half of a reviewable RCA — an operator
        judging a conclusion needs to see what it was chosen over.
        """
        chosen = self.selected
        return tuple(m for m in self.matrices if m is not chosen)


__all__ = [
    "BaselineComparison",
    "BaselineStatus",
    "BlastRadiusReport",
    "CausalChain",
    "EvidenceItem",
    "EvidenceMatrix",
    "EvidenceStance",
    "HistoricalInfluence",
    "HistoricalPrior",
    "Hypothesis",
    "HypothesisScore",
    "ImpactState",
    "IncidentScope",
    "IncidentTimelineView",
    "Investigation",
    "InvestigationBudget",
    "InvestigationCompleteness",
    "MemoryProvenance",
    "MemoryReliability",
    "MemoryStatus",
    "RCAOutcome",
    "RcaTimelineEvent",
    "RecoveryOption",
    "RiskAssessment",
    "RootCauseStatus",
    "ScoreFactor",
    "ServiceImpact",
    "TemporalRelation",
    "TimelineSource",
    "UnappliedRule",
    "VerificationPlan",
]
