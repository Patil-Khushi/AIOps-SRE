"""Tests for stage 7 of the Context Engineering Layer — token budgeting.

Every test here builds its context by hand and calls a pure function. There is not a
mock in the file, which is the property the pipeline's staging was designed for: only
``collectors/`` touches I/O, so from normalisation onward a stage can be pinned with
plain data.

The tests that matter most are the ones about *visibility*, not about arithmetic.
``estimate_tokens`` is an approximation and its exact numbers are allowed to move
(``CHARS_PER_TOKEN`` is explicitly there to be recalibrated), so asserting specific
counts would just pin the guess. What must never move is that a trimmed context says
so — ``test_truncation_records_every_field``, ``test_a_fully_evicted_section_keeps_its_status``
and ``test_re_budgeting_does_not_launder_the_first_truncation`` are the regression
guards for the failure mode this stage exists to prevent: a model handed less
evidence than it thinks it has.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta

import pytest

from aiops.context.models import Observation, SectionStatus, Source, make_observation_id
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    RankedObservation,
    SecurityMetadata,
    SourceProvenance,
)
from aiops.context.tokenizer import (
    PROFILES,
    budget,
    estimate_context_tokens,
    estimate_tokens,
)

WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(minutes=15)
CORRELATION_ID = "corr1"


# --- builders ------------------------------------------------------------


def _observation(source: str = "logs", signature: str = "db timeout", **overrides) -> Observation:
    base = {
        "observation_id": make_observation_id(CORRELATION_ID, source, "error_log", signature),
        "correlation_id": CORRELATION_ID,
        "source": source,
        "timestamp": WINDOW_START,
        "service": "payment-service",
        "severity": "error",
        "category": "error_log",
        "signature": signature,
        # Long enough that evicting one observation moves the estimate by a
        # comfortable margin, so the tests are not sensitive to rounding.
        "evidence": f"connection to mysql timed out after 5s :: {signature} :: " + "x" * 200,
        "confidence": 0.8,
    }
    return Observation(**{**base, **overrides})


def _section(status: SectionStatus = SectionStatus.NOT_REQUESTED, **overrides) -> ContextSection:
    base = {
        "status": status,
        "provenance": SourceProvenance(provider="mock", status=status),
    }
    return ContextSection(**{**base, **overrides})


def _pack(**overrides) -> IncidentContext:
    base: dict[str, object] = {
        "incident": IncidentIdentity(
            service="payment-service",
            severity="Sev-2",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            correlation_id=CORRELATION_ID,
        ),
        "built_at": WINDOW_END,
        "security": SecurityMetadata(redaction_applied=False),
    }
    for name in typing.get_args(Source):
        base[name] = _section()
    return IncidentContext(**{**base, **overrides})


def _ranked_pack() -> IncidentContext:
    """A context with three logs and two metrics observations, all ranked.

    Ranks are assigned deliberately *against* both section order and input order so a
    test asserting eviction order cannot pass by accident: the rank-5 (least relevant)
    observation is the first log line, which is also the first observation in the pack.
    """
    logs = [_observation("logs", f"log-{i}") for i in range(3)]
    metrics = [_observation("metrics", f"metric-{i}") for i in range(2)]
    ranks = {
        logs[0].observation_id: 5,
        logs[1].observation_id: 2,
        logs[2].observation_id: 4,
        metrics[0].observation_id: 1,
        metrics[1].observation_id: 3,
    }
    return _pack(
        logs=_section(SectionStatus.COLLECTED, observations=tuple(logs)),
        metrics=_section(SectionStatus.COLLECTED, observations=tuple(metrics)),
        evidence_ranking=tuple(
            RankedObservation(
                observation_id=obs_id,
                score=1.0 - rank / 10,
                rank=rank,
                rationale=f"rank {rank} for test",
            )
            for obs_id, rank in ranks.items()
        ),
    )


def _budget_for(pack: IncidentContext, *, keep: int, profile: str = "rca"):
    """Budget ``pack`` down to exactly ``keep`` of its observations.

    The limit is the pack's estimate *minus the measured cost of the observations
    that should go*, rather than minus an average cost per observation. An average
    is off by one whenever observations differ in size: eviction removes the least
    valuable first, and if that one happens to be smaller than the mean the total
    stays above the limit and a second observation is given up — so a test written
    against the average asserts an eviction count the implementation is right to
    disagree with.

    Uses the module's own eviction plan to decide *which* observations those are, so
    the limit and the assertion are talking about the same set. That deliberately
    couples this helper to a private function: the alternative is duplicating the
    ordering rules here, which would let the two drift and quietly stop testing the
    real one.

    Still derived from the pack rather than hard-coded, so these tests keep testing
    eviction *order* after ``CHARS_PER_TOKEN`` is recalibrated against real counts.
    """
    from aiops.context.tokenizer import (
        _eviction_plan,
        _observation_tokens,
        _section_priority,
    )

    sections = pack.sections
    plan = _eviction_plan(pack, _section_priority(profile))
    going = plan[: max(len(pack.observations) - keep, 0)]
    cost = sum(_observation_tokens(sections[name].observations[i]) for name, i in going)
    return budget(pack, profile=profile, max_tokens=estimate_context_tokens(pack) - cost)


def _stripped(pack: IncidentContext) -> IncidentContext:
    """The floor of the estimate: every observation *and* every ``raw`` payload gone.

    Both, because both are removable — eviction gives up observations and section
    dropping releases ``raw``. An earlier version removed only the observations,
    which made the "floor" of a raw-only pack equal to its full size, so a limit
    computed as ``floor + n`` was never actually below the pack and the section-
    dropping tests were asserting against a context that was already under budget.
    """
    return pack.model_copy(
        update={
            name: section.model_copy(update={"observations": (), "raw": None})
            for name, section in pack.sections.items()
        }
    )


# --- estimation ----------------------------------------------------------


def test_estimate_tokens_rounds_up_and_is_zero_only_for_empty():
    """A non-empty string must never be free, or tiny observations look costless."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_estimate_grows_with_content():
    small = _pack()
    large = _pack(logs=_section(SectionStatus.COLLECTED, observations=(_observation(),)))
    assert estimate_context_tokens(large) > estimate_context_tokens(small) > 0


def test_estimate_is_compositional():
    """Removing one observation lowers the total by exactly that observation's cost.

    The property ``TokenBudget``'s numbers rely on: without it "how much did this
    eviction buy" is unanswerable and the recorded estimate stops reconciling with
    the context it describes.
    """
    one = _observation("logs", "a")
    two = _observation("logs", "b")
    with_both = _pack(logs=_section(SectionStatus.COLLECTED, observations=(one, two)))
    with_one = _pack(logs=_section(SectionStatus.COLLECTED, observations=(one,)))
    none = _pack(logs=_section(SectionStatus.COLLECTED))

    first_delta = estimate_context_tokens(with_both) - estimate_context_tokens(with_one)
    second_delta = estimate_context_tokens(with_one) - estimate_context_tokens(none)
    assert first_delta > 0
    assert first_delta == second_delta  # identical payload sizes


def test_estimate_charges_for_the_raw_payload():
    """``raw`` reaches prompts verbatim (RCA rebuilds its strings from it), so an
    estimate that ignored it would under-budget the largest thing in the pack."""
    lean = _pack(logs=_section(SectionStatus.COLLECTED))
    fat = _pack(
        logs=_section(SectionStatus.COLLECTED, raw={"logs.recent": {"streams": ["x" * 4000]}})
    )
    assert estimate_context_tokens(fat) > estimate_context_tokens(lean) + 500


def test_estimate_survives_an_unserialisable_raw_payload():
    """Malformed provider payloads degrade the estimate, never raise (rule 5)."""

    class Hostile:
        def __repr__(self) -> str:
            return "<hostile>"

    pack = _pack(logs=_section(SectionStatus.COLLECTED, raw={"q": Hostile()}))
    assert estimate_context_tokens(pack) > 0
    result = budget(pack, profile="rca", max_tokens=10_000)
    assert result.token_budget is not None


def test_estimate_ignores_the_token_budget_field():
    """Otherwise the estimate would depend on its own result."""
    pack = _pack()
    projected = budget(pack, profile="rca", max_tokens=10_000)
    assert estimate_context_tokens(projected) == estimate_context_tokens(pack)


# --- profiles ------------------------------------------------------------


def test_every_profile_is_an_exact_permutation_of_the_sections():
    """A profile that omits a section would leave it unprioritised, and one that
    names a section that does not exist would be a lookup error on the incident
    path. ``_section_priority`` defends against both; this is what keeps the
    defences from ever needing to fire."""
    declared = set(typing.get_args(Source))
    for name, order in PROFILES.items():
        assert set(order) == declared, f"profile {name!r} is not a permutation of Source"
        assert len(order) == len(declared), f"profile {name!r} repeats a section"


def test_the_required_profiles_exist():
    assert {"rca", "log_correlation", "triage", "notification", "summary", "default"} <= set(
        PROFILES
    )


def test_profiles_disagree_about_priority():
    """If every profile ranked sections the same way the whole abstraction is dead
    weight — RCA wanting telemetry and notification wanting ownership is the point."""
    assert PROFILES["rca"] != PROFILES["notification"]
    assert PROFILES["notification"].index("oncall") < PROFILES["notification"].index("logs")
    assert PROFILES["rca"].index("logs") < PROFILES["rca"].index("oncall")


def test_unknown_profile_falls_back_to_the_default_ordering():
    """A typo'd profile name must not take the incident path down."""
    pack = _ranked_pack()
    typo = budget(pack, profile="rcaa", max_tokens=10_000)
    assert typo.token_budget is not None
    assert typo.token_budget.truncated is False

    # The fallback governs the *ordering*, and the ordering is what drops sections.
    raw_pack = _pack(
        **{
            name: _section(SectionStatus.COLLECTED, raw={"q": {"rows": ["y" * 2000]}})
            for name in ("logs", "runbooks")
        }
    )
    floor = estimate_context_tokens(_stripped(raw_pack))
    trimmed = budget(raw_pack, profile="not-a-profile", max_tokens=floor + 400)
    default_order = PROFILES["default"]
    # "runbooks" is last in the default ordering, so it is given up before "logs".
    assert default_order.index("runbooks") > default_order.index("logs")
    assert trimmed.token_budget is not None
    assert trimmed.token_budget.dropped_sections[0] == "runbooks"


def test_the_requested_profile_name_is_recorded_not_the_resolved_one():
    """Recording "default" for a typo would hide the typo forever: the projection
    would look correctly budgeted for a consumer that never asked for it."""
    result = budget(_pack(), profile="  RCAA ", max_tokens=10_000)
    assert result.token_budget is not None
    assert result.token_budget.profile == "rcaa"


# --- under budget --------------------------------------------------------


def test_under_budget_passes_through_intact_but_still_records_the_check():
    pack = _ranked_pack()
    result = budget(pack, profile="rca", max_tokens=1_000_000)

    assert result.token_budget is not None
    assert result.token_budget.truncated is False
    assert result.token_budget.dropped_sections == ()
    assert result.token_budget.evicted_observation_ids == ()
    assert result.token_budget.max_tokens == 1_000_000
    assert result.token_budget.estimated_tokens == estimate_context_tokens(pack)
    # Everything intact: same sections, same observations, same provenance.
    assert result.model_copy(update={"token_budget": None}) == pack


def test_a_context_under_budget_is_reported_even_when_it_holds_nothing():
    """ "This fits" must be a recorded fact, not an absent one."""
    result = budget(_pack(), profile="summary", max_tokens=10_000)
    assert result.token_budget is not None
    assert result.token_budget.truncated is False
    assert result.token_budget.estimated_tokens > 0


# --- eviction order ------------------------------------------------------


def test_eviction_follows_the_ranking_not_the_input_order():
    """Least relevant first, and specifically *not* first-collected first.

    ``_ranked_pack`` puts the rank-5 observation at the front of the pack precisely
    so an implementation that evicted in input order would still pass a naive test
    and fails this one.
    """
    pack = _ranked_pack()
    ranks = {r.observation_id: r.rank for r in pack.evidence_ranking}
    result = _budget_for(pack, keep=2)

    budget_record = result.token_budget
    assert budget_record is not None
    evicted = list(budget_record.evicted_observation_ids)
    assert len(evicted) == 3
    # Given up in descending rank: 5, then 4, then 3.
    assert [ranks[obs_id] for obs_id in evicted] == [5, 4, 3]
    # And the survivors are the two most relevant.
    assert sorted(ranks[obs.observation_id] for obs in result.observations) == [1, 2]


def test_survivors_stay_in_their_original_sections():
    """Eviction removes observations; it never relocates them.

    ``_ranked_pack`` ranks metrics[0] first and logs[1] second, so keeping two leaves
    exactly one survivor in each section. Asserting that both survivors land in
    ``metrics`` would contradict the ranking — and would pass only for an
    implementation that regrouped observations, which is the thing this test exists to
    forbid.
    """
    pack = _ranked_pack()
    result = _budget_for(pack, keep=2)

    assert len(result.metrics.observations) == 1
    assert len(result.logs.observations) == 1
    # Each survivor is still in the section that collected it.
    for name, section in result.sections.items():
        original_ids = {obs.observation_id for obs in pack.section(name).observations}
        for obs in section.observations:
            assert obs.observation_id in original_ids, f"{obs.observation_id} moved into {name}"


def test_unranked_observations_are_evicted_before_ranked_ones():
    """An observation the ranker never placed is one nobody judged worth keeping."""
    ranked = _observation("metrics", "ranked")
    unranked = _observation("logs", "unranked")
    pack = _pack(
        metrics=_section(SectionStatus.COLLECTED, observations=(ranked,)),
        logs=_section(SectionStatus.COLLECTED, observations=(unranked,)),
        evidence_ranking=(
            RankedObservation(
                observation_id=ranked.observation_id,
                score=0.1,
                rank=99,
                rationale="barely relevant but ranked",
            ),
        ),
    )
    result = _budget_for(pack, keep=1)
    assert result.token_budget is not None
    assert result.token_budget.evicted_observation_ids == (unranked.observation_id,)


def test_fallback_order_without_a_ranking_is_not_input_order():
    """With no ranking, the least-confident evidence in the lowest-priority section
    goes first. Input order is an artefact of collector scheduling, so budgeting
    against it would make the surviving evidence depend on which provider answered
    first."""
    first_in = _observation("metrics", "confident-metric", confidence=0.9)
    weak = _observation("metrics", "weak-metric", confidence=0.1)
    oncall = _observation("oncall", "who-is-up", confidence=0.5)
    pack = _pack(
        metrics=_section(SectionStatus.COLLECTED, observations=(first_in, weak)),
        oncall=_section(SectionStatus.COLLECTED, observations=(oncall,)),
    )
    # "rca" ranks oncall last, so its observation is given up before either metric,
    # and the weaker metric goes before the stronger one.
    result = _budget_for(pack, keep=1, profile="rca")
    assert result.token_budget is not None
    assert list(result.token_budget.evicted_observation_ids) == [
        oncall.observation_id,
        weak.observation_id,
    ]
    assert result.metrics.observations == (first_in,)


def test_eviction_never_raises_on_mixed_naive_and_aware_timestamps():
    """The unranked sort key would raise TypeError comparing a naive datetime with an
    aware one, and a crash while trimming an incident's context is not acceptable."""
    aware = _observation("logs", "aware", timestamp=WINDOW_START)
    naive = _observation("logs", "naive", timestamp=WINDOW_START.replace(tzinfo=None))
    pack = _pack(logs=_section(SectionStatus.COLLECTED, observations=(aware, naive)))
    result = _budget_for(pack, keep=1)
    assert result.token_budget is not None
    assert len(result.token_budget.evicted_observation_ids) == 1


# --- whole-section dropping ---------------------------------------------


def test_whole_sections_are_dropped_lowest_priority_first():
    pack = _pack(
        **{
            name: _section(SectionStatus.COLLECTED, raw={"q": {"rows": ["z" * 3000]}})
            for name in ("metrics", "logs", "cmdb", "runbooks")
        }
    )
    floor = estimate_context_tokens(_stripped(pack))
    # Room for roughly one of the four payloads.
    per_payload = (estimate_context_tokens(pack) - floor) / 4
    result = budget(pack, profile="rca", max_tokens=int(floor + per_payload * 1.5))

    record = result.token_budget
    assert record is not None
    order = PROFILES["rca"]
    dropped = list(record.dropped_sections)
    assert dropped, "a raw-only pack over budget must give up sections"
    # Dropped in ascending priority, and the highest-priority section survives.
    assert dropped == sorted(dropped, key=lambda name: -order.index(name))
    assert result.logs.raw is not None
    for name in dropped:
        assert result.sections[name].raw is None


def test_only_sections_that_actually_release_something_are_reported_as_dropped():
    """Listing a section that gave nothing up would report a loss that never happened
    — and ``NOT_REQUESTED`` sections have nothing to give."""
    pack = _pack(logs=_section(SectionStatus.COLLECTED, raw={"q": {"rows": ["z" * 4000]}}))
    result = budget(pack, profile="rca", max_tokens=1)
    record = result.token_budget
    assert record is not None
    assert record.dropped_sections == ("logs",)


def test_observations_are_exhausted_before_a_section_is_dropped():
    """``raw`` is the payload an adapter rebuilds a surviving observation's prompt
    string from, so it cannot be released while that observation is still there."""
    pack = _pack(
        logs=_section(
            SectionStatus.COLLECTED,
            observations=(_observation("logs", "keep-me"),),
            raw={"q": {"rows": ["z" * 4000]}},
        )
    )
    result = _budget_for(pack, keep=1)
    record = result.token_budget
    assert record is not None
    assert record.dropped_sections == ()
    assert result.logs.raw is not None


# --- the record ---------------------------------------------------------


def test_truncation_records_every_field():
    pack = _ranked_pack()
    result = _budget_for(pack, keep=2, profile="log_correlation")
    record = result.token_budget
    assert record is not None
    assert record.profile == "log_correlation"
    assert record.max_tokens > 0
    assert record.truncated is True
    assert record.evicted_observation_ids  # populated, not merely non-None
    assert record.estimated_tokens == estimate_context_tokens(
        result.model_copy(update={"token_budget": None})
    )
    assert record.estimated_tokens <= record.max_tokens


def test_trimming_actually_reaches_the_limit():
    """A "trimmed to fit" projection that does not fit is worse than no trimming:
    the caller stops checking. Also the regression guard for the incremental
    accounting drifting from the re-estimate."""
    pack = _ranked_pack()
    floor = estimate_context_tokens(_stripped(pack))
    for limit in range(floor, estimate_context_tokens(pack) + 1, 7):
        result = budget(pack, profile="rca", max_tokens=limit)
        record = result.token_budget
        assert record is not None
        assert record.estimated_tokens <= limit, f"failed to fit {limit}"


def test_an_impossible_budget_reports_honestly_instead_of_deleting_the_audit_trail():
    """Identity, security metadata and the ranking are a floor. A limit below it
    cannot be met, and the ranking is how a consumer discovers that the observation
    ranked third is missing — so it survives and the overshoot is reported."""
    pack = _ranked_pack()
    result = budget(pack, profile="rca", max_tokens=1)
    record = result.token_budget
    assert record is not None
    assert record.truncated is True
    assert record.estimated_tokens > record.max_tokens
    assert result.observations == ()
    assert result.evidence_ranking == pack.evidence_ranking


# --- statuses survive budgeting ----------------------------------------


def test_a_fully_evicted_section_keeps_its_status():
    """ "We found things and had to drop them" is not "we could not look".

    If eviction downgraded the status, RCA's prompt would print "NONE — this signal
    was checked and was absent" for evidence that was found and then trimmed, and the
    model is told to treat that as positive evidence *against* a cause.
    """
    pack = _ranked_pack()
    result = budget(pack, profile="rca", max_tokens=1)

    assert result.logs.status is SectionStatus.COLLECTED
    assert result.logs.observations == ()
    assert result.logs.usable
    note = result.logs.provenance.coverage_note
    assert note is not None and "token budget" in note


def test_a_dropped_section_keeps_its_status_too():
    pack = _pack(logs=_section(SectionStatus.COLLECTED, raw={"q": {"rows": ["z" * 4000]}}))
    result = budget(pack, profile="rca", max_tokens=1)
    assert result.logs.status is SectionStatus.COLLECTED
    assert result.logs.provenance.status is SectionStatus.COLLECTED


def test_budgeting_never_invents_or_erases_a_status():
    pack = _pack(
        logs=_section(SectionStatus.COLLECTED, observations=(_observation(),)),
        metrics=_section(SectionStatus.EMPTY),
        traces=_section(SectionStatus.FAILED),
        topology=_section(SectionStatus.UNAVAILABLE),
    )
    result = budget(pack, profile="triage", max_tokens=1)
    before = {name: section.status for name, section in pack.sections.items()}
    after = {name: section.status for name, section in result.sections.items()}
    assert before == after


def test_a_collector_coverage_note_is_preserved_not_overwritten():
    """The collector's note explains *why* an answer was partial; losing it to a
    budgeting note would trade one operator-facing explanation for another."""
    pack = _pack(
        logs=ContextSection(
            status=SectionStatus.COLLECTED,
            observations=(_observation(),),
            provenance=SourceProvenance(
                provider="loki",
                status=SectionStatus.COLLECTED,
                coverage_note="only 5 of 12 streams returned",
            ),
        )
    )
    result = budget(pack, profile="rca", max_tokens=1)
    note = result.logs.provenance.coverage_note
    assert note is not None
    assert "only 5 of 12 streams returned" in note
    assert "token budget" in note


# --- idempotency, determinism, immutability -----------------------------


def test_budgeting_is_idempotent():
    pack = _ranked_pack()
    once = _budget_for(pack, keep=2)
    limit = once.token_budget.max_tokens if once.token_budget else 0
    twice = budget(once, profile="rca", max_tokens=limit)
    assert twice == once


def test_re_budgeting_does_not_launder_the_first_truncation():
    """The removal lists are cumulative. A second pass cannot rediscover what the
    first one dropped — the evidence is gone — so if the record reset, a context
    would silently become "complete" again."""
    pack = _ranked_pack()
    once = _budget_for(pack, keep=2)
    assert once.token_budget is not None
    twice = budget(once, profile="notification", max_tokens=1_000_000)

    assert twice.token_budget is not None
    assert twice.token_budget.truncated is True
    assert twice.token_budget.evicted_observation_ids == once.token_budget.evicted_observation_ids
    assert twice.token_budget.profile == "notification"


def test_tightening_the_budget_accumulates_the_record():
    pack = _ranked_pack()
    loose = _budget_for(pack, keep=3)
    assert loose.token_budget is not None
    tight = budget(loose, profile="rca", max_tokens=1)
    assert tight.token_budget is not None
    assert set(loose.token_budget.evicted_observation_ids) <= set(
        tight.token_budget.evicted_observation_ids
    )
    assert len(tight.token_budget.evicted_observation_ids) == len(pack.observations)


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_budgeting_is_deterministic(profile: str):
    """Same inputs, byte-identical outputs — the eval harness compares runs."""
    pack = _ranked_pack()
    limit = int(estimate_context_tokens(pack) * 0.6)
    first = budget(pack, profile=profile, max_tokens=limit)
    second = budget(pack, profile=profile, max_tokens=limit)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_the_input_pack_is_never_mutated():
    pack = _ranked_pack()
    before = pack.model_dump_json()
    budget(pack, profile="rca", max_tokens=1)
    budget(pack, profile="notification", max_tokens=10)
    assert pack.model_dump_json() == before
    assert pack.token_budget is None
    assert len(pack.logs.observations) == 3


def test_the_result_still_round_trips_through_json():
    """A budgeted context is the one most likely to be cached or shipped over MCP."""
    result = _budget_for(_ranked_pack(), keep=2)
    assert IncidentContext.model_validate(result.model_dump(mode="json")) == result
