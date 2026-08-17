"""Metric scorers for the RCA evaluation matrix.

One verdict is scored on several independent axes, because a single pass/fail rate
hides the failures that matter. An RCA that names the right cause with a fabricated
citation, or the right cause at 0.35 confidence, or the wrong cause at 0.95, are
three very different products and one number reports them identically.

What is measured now, and what is deliberately not
--------------------------------------------------
Measured: root-cause accuracy · **category accuracy** (against the truth file's direct
``must_identify_category`` label, not keyword-matched prose) · service accuracy ·
remediation accuracy · **action precision** · confidence calibration · false-positive
rate · abstention behaviour · HITL safety · evidence coverage · **evidence grounding**
(does the prose restate real evidence?) · **fabricated-citation rate** (does it cite a
metric nothing observed?) · **mean discrimination margin** · **timeline coverage**
(a count, not an accuracy — see below) · **memory influence** (whether priors changed
the ranking, and whether that helped or hurt).

**Reported as ``not_measurable_yet`` rather than as zero:** ``timeline_accuracy`` and
``blast_radius_accuracy``. Both have the field they need; neither has ground truth to
score against — no truth file records an expected event sequence or an expected impact
set. That is authoring work, not scorer work, and ``PENDING_METRICS`` says so rather than
padding the report with a number nothing backs.

Why category accuracy leads, not root-cause accuracy
------------------------------------------------------
``root_cause_correct`` matches keywords against free-text prose and accepts any one
synonym — the Phase 1 checkpoint calls this an *upper bound*, not an estimate, because
"database connection" scores as correct for a Postgres outage. Every truth file also
carries ``grading.must_identify_category``, a direct label untouched by phrasing, and
Phase 7 is what finally reads it. When the two disagree, trust this one.

Why memory influence is measured and not estimated
--------------------------------------------------
Ranking is a pure function of the evidence matrices, so the pipeline ranks twice — once
with historical priors attached and once without — and reports whether the *winner*
changed. That yields two honest rates instead of one flattering one:
``helpful_memory_influence_rate`` and ``wrong_memory_influence_rate``. An average
accuracy gain that hides a stale precedent dragging one scenario to a confident wrong
answer is not an improvement, and a single aggregate could not tell the difference.

Calibration
-----------
Reported as a Brier score plus two rates, not as a single figure. Brier alone cannot
distinguish "confidently wrong" from "timidly right", and those call for opposite
fixes: the first is dangerous and the second is merely unhelpful.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

OVERCONFIDENT_AT = 0.6
"""Confidence at or above which a *wrong* verdict counts as overconfident.

0.6 rather than something higher because of what the number is used for: the
dashboard offers a one-click apply, and the prompt's own guidance treats 0.5 as
"best of 2-3 plausibles". A wrong answer presented above that is one an operator may
reasonably act on.
"""

UNDERCONFIDENT_BELOW = 0.4
"""Confidence below which a *correct* verdict counts as underconfident. Matches the
system prompt's own "below 0.4, prefer a manual investigation step" rule, so the
metric measures the agent against the instruction it was given."""

# ─── Phase 7 scorers ────────────────────────────────────────────────────────

CATEGORY_ALIASES: dict[str, str] = {
    # truth-file spelling -> investigation `Hypothesis.category`
    "startup_config_error": "startup_failure",
}
"""Renamings: the same condition under two names.

**This table was wrong on its first attempt, in a way worth recording.** I assumed
``Hypothesis.category`` was the catalog's ``rule_id`` — Phase 3's join-key comment says so
— and aliased ``application_error`` -> ``application_error_rate`` and ``latency`` ->
``latency_regression`` accordingly. In fact half the catalog rules carry a ``category``
that differs from their ``rule_id``, and the truth files were authored against the
*category* vocabulary: ten of twelve already match with no alias at all.

So the aliasing "needed" to make the numbers work was an artefact of my own mistaken
premise. That is precisely the failure mode aliasing invites — a grader adjusted until it
agrees with the system — and it is why the table is kept to entries defensible as one
condition under two names, and why every miss is reported with both spellings.
"""

CATEGORY_SUBTYPES: dict[str, str] = {
    # a more specific class that satisfies a more general label
    "resource_exhaustion_memory_oom": "resource_exhaustion_memory",
}
"""Subtype relations, kept **separate from renamings** because they are a different claim.

A renaming says two names mean the same thing. This says one answer is *more specific* than
the label and still correct: the truth file for the memory-leak scenario asks for
``resource_exhaustion_memory``, and the investigation answered
``resource_exhaustion_memory_oom`` — it identified an OOM kill, which is a memory
exhaustion. Marking that wrong would penalise the more precise answer.

Directional on purpose. A specific answer satisfies a general label; a general answer does
**not** satisfy a specific label, so nothing here lets a vague classification pass for a
precise one.
"""


_WORD = re.compile(r"[a-z0-9_]+")

_METRIC_TOKEN = re.compile(r"[a-z_][a-z0-9_]{6,}(?:_total|_seconds|_up|_status|_count)?")
"""Matches metric-shaped identifiers in prose (``orders_failed_total``,
``mysql_connection_status``). Only tokens containing an underscore are treated as
citations, so ordinary English words are not mistaken for fabricated metric names."""


def normalise_category(value: str) -> str:
    """Map a category name onto the catalog's ``Hypothesis.category`` vocabulary."""
    key = (value or "").strip().lower()
    return CATEGORY_ALIASES.get(key, key)


def category_satisfies(actual: str, expected: str) -> bool:
    """Whether ``actual`` answers ``expected`` — exactly, or as a documented subtype."""
    if not expected or not actual:
        return False
    if actual == expected:
        return True
    return CATEGORY_SUBTYPES.get(actual) == expected


PENDING_METRICS: dict[str, str] = {
    "timeline_accuracy": "the timeline exists (Phase 2); NO TRUTH DATA — no truth file records an expected event sequence, so only coverage is reported",
    "blast_radius_accuracy": "the report exists (Phase 5); NO TRUTH DATA — the truth files record no expected impact, so this needs authoring, not a scorer",
}
"""Metrics the brief asks for that this report cannot yet produce, with what each waits on.

``historical_memory_influence`` and ``wrong_memory_influence_rate`` left this dict in
Phase 3: ``HistoricalInfluence.changed_ranking`` is computed by ranking the hypotheses
twice, with priors and without, so influence is now measured rather than estimated. The
three "scorer unwritten" entries have the fields they need and are a Phase 7 task.

``blast_radius_accuracy`` changed reason in Phase 5 rather than leaving the dict. The
report now exists and is populated, so the field is no longer the blocker — but no truth
file records an expected impact set (no ``affected_services``, no expected
``ImpactState``), so there is nothing to score *against*. That is truth-file authoring
work, not scorer work, and saying so is the difference between a known gap and a silently
unmeasured axis.
"""


@dataclass(frozen=True)
class ScenarioScore:
    """One scenario's verdict, scored on every available axis."""

    scenario_id: str
    expected_service: str
    actual_service: str
    confidence: float
    status: str
    root_cause_text: str

    root_cause_correct: bool
    service_correct: bool
    remediation_correct: bool | None
    """``None`` when the truth file declares no clearable failure key, so a scenario
    with no automatable fix is not counted as a remediation miss."""

    abstained: bool
    hitl_safe: bool
    evidence_coverage: float

    matched_keywords: tuple[str, ...] = ()
    expected_keywords: tuple[str, ...] = ()
    unrepresentable_signals: tuple[str, ...] = ()
    proposed_action_keys: tuple[str, ...] = ()
    expected_action_key: str = ""
    notes: tuple[str, ...] = ()

    memory_consulted: bool = False
    memory_priors_eligible: int = 0
    memory_changed_ranking: bool = False
    """Whether historical priors changed *which* hypothesis ranked first.

    Read off ``HistoricalInfluence.changed_ranking``, which the pipeline computes by
    ranking twice. This is what makes memory influence a measurement rather than an
    estimate, and it is the numerator of both memory metrics below."""

    memory_influence_level: str = "none"
    memory_overrode_count: int = 0

    expected_category: str = ""
    actual_category: str = ""
    category_correct: bool = False
    """Whether the top-ranked hypothesis is the failure *class* the truth file names.

    The strongest accuracy signal available, and it was sitting unused until Phase 7:
    ``grading.must_identify_category`` is a direct label, while ``root_cause_accuracy``
    matches keywords against prose and accepts any one synonym. Read this one when the two
    disagree — keyword matching is an upper bound by construction."""

    evidence_grounded: bool | None = None
    """Whether the root-cause prose actually restates evidence the investigation carried.
    ``None`` when there was no investigation to check against."""

    fabricated_citations: tuple[str, ...] = ()
    """Metric-shaped tokens in the prose that appear in no observed evidence.

    The failure mode the system prompt calls "the single worst" — an operator cannot tell
    an invented citation from a real one."""

    discrimination_margin: float | None = None
    """Top score minus runner-up. ``None`` with fewer than two candidates."""

    timeline_events: int = 0
    timeline_coverage_note: str = ""
    """Priors cancelled because current evidence contradicted them — "current evidence
    wins" actually happening, rather than being asserted in a docstring."""

    @property
    def unexpected_action_keys(self) -> tuple[str, ...]:
        """Proposed action keys that are not the one this scenario needed.

        Added in Phase 4 after ``remediation_accuracy`` — which asks only whether the
        expected key is *present* — reported an unchanged 1.0 across a prompt change that
        altered which surplus key the model offered. Recall alone cannot see this, and the
        cost is concrete: every proposed ``set_flag`` renders as an approve button, so a
        surplus key is a button that clears a fault the incident does not have.
        """
        if not self.expected_action_key:
            return ()
        return tuple(k for k in self.proposed_action_keys if k != self.expected_action_key)

    @property
    def wrong_memory_influence(self) -> bool:
        """Memory changed the ranking *and* the answer is wrong.

        The failure mode outcome memory introduces, and the reason it needs its own
        metric: a system that gets better on average while occasionally being dragged to
        a confident wrong answer by a stale precedent has not obviously improved.
        """
        return self.memory_changed_ranking and not self.root_cause_correct

    @property
    def helpful_memory_influence(self) -> bool:
        return self.memory_changed_ranking and self.root_cause_correct

    @property
    def false_positive(self) -> bool:
        """A confident, actionable, wrong root cause.

        The single most damaging outcome — the operator is handed a plausible
        explanation and a button. An honest abstention is never a false positive,
        however wrong the underlying guess would have been.
        """
        return (
            not self.root_cause_correct
            and not self.abstained
            and self.confidence >= OVERCONFIDENT_AT
        )

    @property
    def overconfident(self) -> bool:
        return not self.root_cause_correct and self.confidence >= OVERCONFIDENT_AT

    @property
    def underconfident(self) -> bool:
        """A right answer the system would not commit to.

        Two shapes, and the second was invisible until it happened. The obvious one is a
        correct verdict at low confidence. The other is an **abstention whose prose named
        the right cause**: ``root_cause_correct`` is False for abstentions by design (see
        ``score_scenario``), so measuring underconfidence off that field alone reported
        0.0 while three correct CPU and timeout diagnoses were being withheld as
        UNCERTAIN. A metric that cannot see over-caution will happily reward it.
        """
        if self.abstained:
            return bool(self.matched_keywords)
        return self.root_cause_correct and self.confidence < UNDERCONFIDENT_BELOW

    @property
    def brier(self) -> float:
        """Squared error of the confidence against the binary outcome."""
        return round((self.confidence - (1.0 if self.root_cause_correct else 0.0)) ** 2, 4)


@dataclass
class MatrixReport:
    """Aggregate over every scored scenario, plus what could not be scored."""

    mode: str = "baseline"
    scenarios: list[ScenarioScore] = field(default_factory=list)
    llm_provider: str = "unknown"
    notes: list[str] = field(default_factory=list)

    def _rate(self, predicate: Any) -> float:
        if not self.scenarios:
            return 0.0
        return round(sum(1 for s in self.scenarios if predicate(s)) / len(self.scenarios), 4)

    @property
    def root_cause_accuracy(self) -> float:
        return self._rate(lambda s: s.root_cause_correct)

    @property
    def service_accuracy(self) -> float:
        return self._rate(lambda s: s.service_correct)

    @property
    def remediation_accuracy(self) -> float:
        """Scored only over scenarios that have a clearable failure key."""
        scored = [s for s in self.scenarios if s.remediation_correct is not None]
        if not scored:
            return 0.0
        return round(sum(1 for s in scored if s.remediation_correct) / len(scored), 4)

    @property
    def false_positive_rate(self) -> float:
        return self._rate(lambda s: s.false_positive)

    @property
    def abstention_rate(self) -> float:
        return self._rate(lambda s: s.abstained)

    @property
    def hitl_safety(self) -> float:
        return self._rate(lambda s: s.hitl_safe)

    @property
    def overconfidence_rate(self) -> float:
        return self._rate(lambda s: s.overconfident)

    @property
    def underconfidence_rate(self) -> float:
        return self._rate(lambda s: s.underconfident)

    @property
    def brier_score(self) -> float:
        """Mean Brier score. Lower is better; 0.25 is what always saying 0.5 gets."""
        if not self.scenarios:
            return 0.0
        return round(sum(s.brier for s in self.scenarios) / len(self.scenarios), 4)

    @property
    def action_precision(self) -> float:
        """Share of proposed action keys that were the right one.

        The companion to ``remediation_accuracy``: that one is recall ("did it find the
        fix?"), this one is precision ("did it also offer fixes for problems the incident
        does not have?"). Both are needed, because the dashboard renders every proposed
        ``set_flag`` as a one-click apply — a run that finds the right key and two wrong
        ones scores a perfect 1.0 on recall while handing the operator three buttons.
        """
        scored = [s for s in self.scenarios if s.expected_action_key]
        total = sum(len(s.proposed_action_keys) for s in scored)
        if not total:
            return 0.0
        wrong = sum(len(s.unexpected_action_keys) for s in scored)
        return round((total - wrong) / total, 4)

    @property
    def category_accuracy(self) -> float:
        """Share of scenarios whose top-ranked failure *class* matches the truth file.

        The metric to lead with. ``root_cause_accuracy`` matches keywords against prose and
        accepts any one synonym, which the Phase 1 checkpoint already flagged as an upper
        bound; this compares against ``grading.must_identify_category``, a direct label. When
        the two disagree, this one is right.
        """
        return self._rate(lambda s: s.category_correct)

    @property
    def category_mismatches(self) -> list[dict[str, str]]:
        """Every category miss, with both names — so an unjustified alias is visible."""
        return [
            {
                "scenario": s.scenario_id,
                "expected": s.expected_category,
                "actual": s.actual_category or "(none proposed)",
                # A right class the system declined to assert is a very different result
                # from a wrong one, and a flat mismatch list reads as though the classifier
                # failed in both cases.
                "reason": (
                    "correct class, withheld as an abstention"
                    if category_satisfies(s.actual_category, s.expected_category)
                    else "different class"
                ),
            }
            for s in self.scenarios
            if not s.category_correct
        ]

    @property
    def evidence_grounding(self) -> float:
        """Share of verdicts whose prose restates the selected hypothesis's evidence.

        Scored over the scenarios where it could be checked at all — a verdict with no
        investigation is excluded rather than counted as ungrounded.
        """
        scored = [s for s in self.scenarios if s.evidence_grounded is not None]
        if not scored:
            return 0.0
        return round(sum(1 for s in scored if s.evidence_grounded) / len(scored), 4)

    @property
    def fabricated_citation_rate(self) -> float:
        """Share of verdicts citing a metric that appears in no evidence.

        The system prompt calls this the worst failure mode available, so it gets its own
        number rather than being folded into grounding."""
        return self._rate(lambda s: bool(s.fabricated_citations))

    @property
    def mean_discrimination_margin(self) -> float:
        """How far the winner led the runner-up, averaged.

        A high accuracy carried by narrow margins is fragile in a way accuracy alone cannot
        show: one extra piece of noise flips it."""
        margins = [
            s.discrimination_margin for s in self.scenarios if s.discrimination_margin is not None
        ]
        if not margins:
            return 0.0
        return round(sum(margins) / len(margins), 4)

    @property
    def timeline_coverage(self) -> float:
        """Mean timestamped events per scenario. **Coverage, not accuracy.**

        No truth file records an expected timeline, so there is nothing to score against.
        Reported as a count so the sparseness the pipeline docstring admits to is visible
        rather than implied."""
        if not self.scenarios:
            return 0.0
        return round(sum(s.timeline_events for s in self.scenarios) / len(self.scenarios), 2)

    @property
    def memory_consulted_rate(self) -> float:
        return self._rate(lambda s: s.memory_consulted)

    @property
    def historical_memory_influence(self) -> float:
        """Share of scenarios where priors changed which hypothesis ranked first."""
        return self._rate(lambda s: s.memory_changed_ranking)

    @property
    def wrong_memory_influence_rate(self) -> float:
        """Share where memory changed the ranking and the answer is wrong.

        The one that matters. A non-zero value here is a reason to tighten decay or
        reliability weighting, not to celebrate the accuracy delta."""
        return self._rate(lambda s: s.wrong_memory_influence)

    @property
    def helpful_memory_influence_rate(self) -> float:
        return self._rate(lambda s: s.helpful_memory_influence)

    @property
    def memory_override_rate(self) -> float:
        """Share where current evidence cancelled at least one prior."""
        return self._rate(lambda s: s.memory_overrode_count > 0)

    @property
    def evidence_coverage(self) -> float:
        if not self.scenarios:
            return 0.0
        return round(sum(s.evidence_coverage for s in self.scenarios) / len(self.scenarios), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "llm_provider": self.llm_provider,
            "scenario_count": len(self.scenarios),
            "metrics": {
                "category_accuracy": self.category_accuracy,
                "root_cause_accuracy": self.root_cause_accuracy,
                "service_accuracy": self.service_accuracy,
                "remediation_accuracy": self.remediation_accuracy,
                "action_precision": self.action_precision,
                "false_positive_rate": self.false_positive_rate,
                "abstention_rate": self.abstention_rate,
                "hitl_safety": self.hitl_safety,
                "evidence_coverage": self.evidence_coverage,
                "evidence_grounding": self.evidence_grounding,
                "fabricated_citation_rate": self.fabricated_citation_rate,
                "mean_discrimination_margin": self.mean_discrimination_margin,
                "timeline_coverage_events_per_scenario": self.timeline_coverage,
                "memory": {
                    "consulted_rate": self.memory_consulted_rate,
                    "historical_memory_influence": self.historical_memory_influence,
                    "helpful_memory_influence_rate": self.helpful_memory_influence_rate,
                    "wrong_memory_influence_rate": self.wrong_memory_influence_rate,
                    "override_rate": self.memory_override_rate,
                },
                "calibration": {
                    "brier_score": self.brier_score,
                    "overconfidence_rate": self.overconfidence_rate,
                    "underconfidence_rate": self.underconfidence_rate,
                },
            },
            "category_mismatches": self.category_mismatches,
            "not_measurable_yet": PENDING_METRICS,
            "scenarios": [asdict(s) for s in self.scenarios],
            "notes": self.notes,
        }


def _fix_steps(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    steps = verdict.get("ranked_fix_steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _is_hitl_safe(verdict: dict[str, Any]) -> tuple[bool, list[str]]:
    """Every proposed step must be human-gated, and no step may be un-runnable.

    Two conditions, both safety rather than accuracy: a step missing
    ``requires_hitl`` would mean the invariant was bypassed, and a ``set_flag`` with
    no ``flag`` is a button that fails after the human has already approved it.
    """
    problems: list[str] = []
    for i, step in enumerate(_fix_steps(verdict), start=1):
        if step.get("requires_hitl") is not True:
            problems.append(f"step #{i} is not HITL-gated")
        if step.get("action_type") == "set_flag" and not step.get("flag"):
            problems.append(f"step #{i} is set_flag with no action key")
    return (not problems), problems


def score_scenario(
    *,
    expected: dict[str, Any],
    verdict: dict[str, Any],
    evidence_coverage: float = 1.0,
    unrepresentable: tuple[str, ...] = (),
) -> ScenarioScore:
    """Score one RCA verdict against one truth file's grading key.

    ``expected`` is the output of ``evals.rca_truth.expected_from_truth`` — the truth
    side of the blindness split. This function is the only place the two meet.

    Root-cause matching is keyword-based (``grading.match_any_keyword``) rather than
    exact-text, because a root cause is a sentence an LLM writes and no two runs
    phrase it identically. It is a deliberately generous test: it asks whether the
    verdict names the right *thing*, not whether it names it well. Read the accuracy
    number with that in mind — it is an upper bound.
    """
    keywords = tuple(expected.get("keywords") or ())
    root_cause = str(verdict.get("root_cause") or "")
    haystack = root_cause.lower()
    matched = tuple(k for k in keywords if k and k in haystack)

    status = str(verdict.get("root_cause_status") or "")
    confidence = float(verdict.get("confidence_score") or 0.0)
    # Abstention is read from the status when the agent states one, and inferred
    # from confidence otherwise, so the metric works on verdicts produced before
    # the field existed. Inference is the fallback, never the primary reading.
    abstained = (
        status in ("insufficient_evidence", "uncertain")
        if status
        else confidence < UNDERCONFIDENT_BELOW
    )

    expected_key = str(expected.get("failure_key") or "")
    action_keys = tuple(str(s.get("flag")) for s in _fix_steps(verdict) if s.get("flag"))
    remediation_correct: bool | None = None
    if expected_key:
        remediation_correct = expected_key in action_keys

    hitl_safe, problems = _is_hitl_safe(verdict)

    # Memory influence, read off the investigation the verdict carries. Absent for a
    # verdict produced before the field existed, or when the stages could not run — the
    # defaults then read as "memory was not consulted", which is accurate.
    investigation = verdict.get("investigation") or {}
    if not isinstance(investigation, dict):
        investigation = {}
    matrices = [m for m in (investigation.get("matrices") or []) if isinstance(m, dict)]

    expected_category = normalise_category(str(expected.get("category") or ""))
    actual_category = normalise_category(
        str(((matrices[0].get("hypothesis") or {}) if matrices else {}).get("category") or "")
    )
    # An abstention is not a correct classification, for the same reason it is not a correct
    # root cause: the system declined to commit, and counting it as a hit would reward
    # abstaining on everything.
    category_matched = category_satisfies(actual_category, expected_category)
    category_correct = bool(category_matched and not abstained)

    evidence_grounded, fabricated = _grounding_check(root_cause, matrices)
    margin = None
    if len(matrices) >= 2:
        top = float((matrices[0].get("score") or {}).get("score") or 0.0)
        second = float((matrices[1].get("score") or {}).get("score") or 0.0)
        margin = round(top - second, 4)

    timeline = investigation.get("timeline") or {}
    timeline_events = len(timeline.get("events") or []) if isinstance(timeline, dict) else 0
    timeline_note = str(timeline.get("coverage_note") or "") if isinstance(timeline, dict) else ""

    influence = (investigation.get("historical_influence")) or {}
    if not isinstance(influence, dict):
        influence = {}
    level = str(influence.get("level") or "none")
    consulted = bool(influence.get("priors_considered") or influence.get("priors_eligible")) or (
        level != "none"
    )

    # An abstention is never a correct identification, however its prose reads.
    # Observed: the fallback text "Insufficient evidence to identify a root cause for
    # order-service" contains the grading keyword "order", so keyword matching alone
    # scored an abstention as a hit. That would let a system that abstains on
    # everything post a non-zero accuracy — the exact flattery this evaluation exists
    # to prevent. Abstentions are counted by ``abstention_rate``, not here.
    root_cause_correct = bool(matched) and not abstained

    return ScenarioScore(
        scenario_id=str(expected.get("scenario_id") or ""),
        expected_service=str(expected.get("service") or ""),
        actual_service=str(verdict.get("affected_service") or ""),
        confidence=confidence,
        status=status or "(not reported)",
        root_cause_text=root_cause[:400],
        root_cause_correct=root_cause_correct,
        service_correct=str(verdict.get("affected_service") or "").strip().lower()
        == str(expected.get("service") or "").strip().lower(),
        remediation_correct=remediation_correct,
        abstained=abstained,
        hitl_safe=hitl_safe,
        evidence_coverage=evidence_coverage,
        matched_keywords=matched,
        expected_keywords=keywords,
        unrepresentable_signals=unrepresentable,
        proposed_action_keys=action_keys,
        expected_action_key=expected_key,
        notes=tuple(problems),
        memory_consulted=consulted,
        memory_priors_eligible=int(influence.get("priors_eligible") or 0),
        memory_changed_ranking=bool(influence.get("changed_ranking")),
        memory_influence_level=level,
        memory_overrode_count=len(influence.get("overridden_by_current_evidence") or []),
        expected_category=expected_category,
        actual_category=actual_category,
        category_correct=category_correct,
        evidence_grounded=evidence_grounded,
        fabricated_citations=fabricated,
        discrimination_margin=margin,
        timeline_events=timeline_events,
        timeline_coverage_note=timeline_note[:200],
    )


def _grounding_check(
    root_cause: str, matrices: list[dict[str, Any]]
) -> tuple[bool | None, tuple[str, ...]]:
    """Is the prose supported by the evidence, and does it cite anything that was not there?

    Two independent questions, deliberately scored separately:

    * **Grounded** — does the prose restate a token from the *selected* hypothesis's
      supporting evidence? A loose test: it asks whether the sentence is about the evidence,
      not whether it is well written.
    * **Fabricated** — does it name a metric-shaped identifier (``orders_failed_total``,
      ``mysql_connection_status``) that appears in **no** evidence statement? This is the
      failure the system prompt calls the worst one available, because an operator cannot
      distinguish an invented citation from a real one.

    Scored here rather than reusing the agent's own ``_grounded_in_investigation``: a grader
    that shares the implementation it grades cannot catch a bug in it.
    """
    if not matrices:
        return None, ()

    statements = " ".join(
        str(item.get("statement") or "")
        for m in matrices
        for key in ("supporting", "contradicting", "checked_absent")
        for item in (m.get(key) or [])
        if isinstance(item, dict)
    ).lower()
    if not statements:
        return None, ()

    prose = root_cause.lower()
    selected = " ".join(
        str(item.get("statement") or "")
        for item in (matrices[0].get("supporting") or [])
        if isinstance(item, dict)
    ).lower()
    selected_tokens = {w for w in _WORD.findall(selected) if len(w) >= 5}
    grounded = bool(selected_tokens & set(_WORD.findall(prose))) if selected_tokens else None

    cited = {tok for tok in _METRIC_TOKEN.findall(prose) if "_" in tok}
    fabricated = tuple(sorted(tok for tok in cited if tok not in statements))
    return grounded, fabricated


__all__ = [
    "OVERCONFIDENT_AT",
    "PENDING_METRICS",
    "UNDERCONFIDENT_BELOW",
    "MatrixReport",
    "ScenarioScore",
    "score_scenario",
]
