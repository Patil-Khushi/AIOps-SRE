"""Stage 7 — fit one incident's context into one consumer's token allowance.

Why this stage exists
---------------------
Nothing else in the stack bounds the size of a *prompt*. ``AIOPS_LLM_MAX_TOKENS_PER_CALL``
looks like a cap but is not this one: ``aiops/llm/gateway.py::_enforce_caps`` clamps
``LLMRequest.max_tokens``, which every provider forwards as the **response** limit
(``anthropic_provider.py`` passes it straight through as ``max_tokens``). So the
platform bounds what the model may write and nothing bounds what it is shown.

That was survivable while each agent gathered its own narrow evidence. It stops
being survivable here: a context fans in eleven sources, and ``ContextSection.raw``
deliberately carries whole provider payloads so an adapter can reproduce a legacy
prompt string byte-for-byte. Handed to a model unbudgeted, that overflows a context
window, and overflow does not fail cleanly — depending on the provider you get a
400, or a middle silently dropped, and the second one produces a confident verdict
built on evidence the model never saw.

Loud truncation is the whole point
----------------------------------
A model handed a trimmed evidence set with **no indication that it was trimmed**
reasons as though it saw everything. It will say "no deployment preceded this
failure" when the deployments section was evicted rather than empty. So this module
never trims quietly: every projection carries a ``TokenBudget`` naming the profile,
the limit, the resulting estimate, and the exact ids and sections that were removed
— including when nothing was removed, so "under budget" is a recorded fact rather
than an absence of evidence.

For the same reason budgeting never rewrites a status. Evicting every observation
from a ``COLLECTED`` section leaves it ``COLLECTED`` with zero observations plus a
coverage note, because "we found things and had to drop them" and "we could not
look" are different facts and the rest of this layer is built on keeping them apart
(see ``SectionStatus``). Turning the first into the second here would hand the RCA
prompt a "this signal was checked and was absent" line it has no right to print.

Estimating without tiktoken
---------------------------
``tiktoken`` is not a dependency and may not become one (CI runs ``uv sync --locked``
and the tokenizer differs per provider anyway, so a vendored BPE table would be
accurate for exactly one backend). ``CHARS_PER_TOKEN`` is the documented stand-in;
see its comment for the divisor and for how to calibrate it against
``LLMResponse.input_tokens``, which already reports every provider's true count.

Purity
------
No I/O, no clock, no env reads. Stage 1 (``collectors/``) is the only impure stage;
everything from normalisation onward — including this — is a pure function over data
structures, which is what lets the eval harness compare two runs over one incident
and what lets these tests run with zero mocks.
"""

from __future__ import annotations

import json
import logging
import math
import typing
from typing import Any

from aiops.context.models import Observation, Source
from aiops.context.pack import ContextSection, IncidentContext, TokenBudget

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
"""Characters per token, for the estimate this module budgets against.

Four is the usual rule of thumb for English prose under the BPE vocabularies the
hosted providers use (cl100k/o200k and Anthropic's are all close enough at this
resolution). Our payloads are *denser* than prose — JSON punctuation, PromQL,
service identifiers, base64-ish trace ids all tokenise closer to 3 chars/token — so
this constant systematically **under**-estimates, and the error is in the unsafe
direction. That is accepted deliberately rather than fudged with a hidden safety
factor, because guessing twice is worse than guessing once:
``aiops.llm.base.LLMResponse.input_tokens`` already carries the true count from
every provider response, so the honest fix is to calibrate this divisor against
observed usage rather than to pad it now.

Not read from the environment on purpose. A budget that changes with an operator's
``.env`` would make two runs over the same incident produce different evidence,
which is exactly the determinism the eval harness compares against.
"""


PROFILES: dict[str, tuple[str, ...]] = {
    # RCA reasons backwards from a symptom to a change, so the two things it cannot
    # work without are the failure signal and what moved. ``deployments`` leads
    # despite being a *small* section precisely because a correlated deploy is the
    # highest-yield-per-token evidence in the whole pack: keeping it costs almost
    # nothing and losing it costs the answer. Ownership (``oncall``, ``cmdb``) sits
    # last — RCA explains a failure, it does not page anyone.
    "rca": (
        "deployments",
        "logs",
        "metrics",
        "traces",
        "k8s_events",
        "dependencies",
        "topology",
        "runbooks",
        "incident_history",
        "cmdb",
        "oncall",
    ),
    # RA-007 correlates log lines *across services*, so after the logs themselves the
    # valuable sections are the ones that say which services are even candidates:
    # traces give the request path, topology and dependencies the static graph.
    # Metrics are demoted relative to RCA — a rate curve does not help decide whether
    # two log lines are the same event.
    "log_correlation": (
        "logs",
        "traces",
        "topology",
        "dependencies",
        "k8s_events",
        "metrics",
        "deployments",
        "incident_history",
        "cmdb",
        "oncall",
        "runbooks",
    ),
    # Triage answers "how bad, whose, and have we seen it before" in seconds. Cheap
    # decisive signals first (alert/metric state, pod churn, a few error lines), then
    # ownership and history for the dedup decision. Traces and runbooks are last:
    # they are the most expensive sections per unit of *triage* value, and nothing
    # triage outputs depends on a span tree.
    "triage": (
        "metrics",
        "k8s_events",
        "logs",
        "cmdb",
        "incident_history",
        "dependencies",
        "oncall",
        "deployments",
        "topology",
        "traces",
        "runbooks",
    ),
    # A notification is about *who* and *how wide*, not about why. Ownership and the
    # blast-radius graph outrank every telemetry section, and logs sit near the
    # bottom on purpose — a war-room body wants one line per signal, and a truncated
    # log dump in Slack is noise that pushes the join link off the screen.
    "notification": (
        "oncall",
        "cmdb",
        "topology",
        "dependencies",
        "deployments",
        "runbooks",
        "metrics",
        "k8s_events",
        "incident_history",
        "traces",
        "logs",
    ),
    # Postmortem/knowledge synthesis wants breadth: the narrative is "what changed,
    # what broke, has it happened before", with a representative sample from each
    # source rather than depth in any one. Same head as RCA, but history is promoted
    # (a recurrence is the headline of a postmortem) and depth-heavy sections are
    # demoted since a summary quotes a handful of lines at most.
    "summary": (
        "deployments",
        "incident_history",
        "metrics",
        "logs",
        "k8s_events",
        "traces",
        "topology",
        "dependencies",
        "runbooks",
        "cmdb",
        "oncall",
    ),
    # The no-opinion ordering: telemetry, then structure and change, then ownership —
    # which is the grouping ``IncidentContext`` already declares its fields in. An
    # unrecognised profile should behave like "nobody expressed a preference", not
    # like some *other* consumer's preference, so this deliberately is not an alias
    # of "rca" even though rca is the heaviest consumer.
    "default": (
        "metrics",
        "logs",
        "traces",
        "k8s_events",
        "deployments",
        "topology",
        "dependencies",
        "incident_history",
        "oncall",
        "cmdb",
        "runbooks",
    ),
}
"""Consumer name → section priority, highest first.

Read-only by convention: it is a plain ``dict`` because the public signature says so,
not because mutating it is supported. The orderings drive *section* dropping only;
which individual observations go is decided by ``pack.evidence_ranking`` (see
``_eviction_plan``). Those two axes are kept separate rather than blended into one
score because a blended one cannot explain itself, and this layer holds itself to
the rule ``RankedObservation.rationale`` encodes — a ranking nobody can audit is a
ranking nobody should trust with an incident.
"""


_ALL_SECTIONS: tuple[str, ...] = tuple(typing.get_args(Source))
"""Every section name, from the ``Source`` literal — the one declaration of record."""

_NOTE_PREFIX = "token budget:"
"""Marker that makes ``_with_note`` idempotent; see there."""


# --- estimation ----------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Approximate token count for one string. See ``CHARS_PER_TOKEN``.

    Rounds up, so any non-empty string costs at least one token. An estimator that
    returned 0 for short strings would let an unbounded number of tiny observations
    look free.
    """
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _json_tokens(value: Any) -> int:
    """Cost of a value in the shape it actually crosses a boundary in.

    The context is defined as JSON-serialisable (``IncidentContext`` is cached,
    logged and carried over HTTP/MCP as ``model_dump(mode="json")``), so charging for
    the JSON rendering both matches the real wire size and gives a sane upper bound
    on whatever subset an adapter renders into a prompt.

    ``sort_keys`` because a provider payload built from set iteration or a dict whose
    insertion order varies between runs must still cost the same on every run —
    otherwise the estimate, and therefore which observations survive, would not be
    reproducible. ``default=str`` and the ``except`` are the "never raise on the
    incident path" rule: ``raw`` holds untouched provider payloads and may contain
    anything at all, and a context must never be lost to a budgeting crash. Falling
    back to ``repr`` keeps a hostile payload *expensive* rather than free, which is
    the safe direction — free would let it evade eviction entirely.
    """
    if value is None:
        return 0
    try:
        rendered = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        logger.debug("unserialisable payload; estimating from repr", exc_info=True)
        rendered = repr(value)
    return estimate_tokens(rendered)


def _observation_tokens(obs: Observation) -> int:
    """Cost of one observation, including its metadata."""
    return _json_tokens(obs.model_dump())


def _section_frame_tokens(section: ContextSection) -> int:
    """Cost of everything about a section *except* its observations and ``raw``.

    Split out because it is the part budgeting cannot remove: a section field is
    required, so its status and provenance are a floor. Keeping it separate is what
    makes the totals below composable.

    **The projection note is excluded**, for the same reason ``_envelope_tokens``
    excludes ``token_budget``: it is a record *of* this computation, so charging for
    it would make the estimate depend on its own result. That is not a rounding
    concern here, it is a termination one. An earlier version charged the note at the
    moment it was applied, which meant emptying a section pushed the running total
    back *up* — often over the limit again — so the act of recording a truncation
    caused a further truncation, and the loop gave up one or more observations more
    than the limit actually required. Worse, the fixed point could sit above
    ``max_tokens``, so a context reported as trimmed-to-fit did not fit, and a caller
    that trusted the claim stopped checking.

    Excluding it makes eviction monotonic — every step strictly lowers the total —
    which is what makes ``max_tokens`` reachable whenever it is above the floor.
    """
    provenance = section.provenance.model_dump()
    provenance["coverage_note"] = _without_budget_note(section.provenance.coverage_note)
    return _json_tokens({"status": section.status.value, "provenance": provenance})


def _section_tokens(section: ContextSection) -> int:
    """Total cost of one section."""
    return (
        _section_frame_tokens(section)
        + _json_tokens(section.raw)
        + sum(_observation_tokens(obs) for obs in section.observations)
    )


def _envelope_tokens(pack: IncidentContext) -> int:
    """Cost of the parts of a context that budgeting never touches.

    Identity, security metadata and the ranking survive trimming intact — the
    ranking especially, because it is how a consumer discovers that the thing ranked
    third is missing from the evidence it was handed. They are therefore a floor:
    a ``max_tokens`` below this cannot be met, and ``budget`` reports that honestly
    instead of deleting the audit trail to hit a number.

    ``token_budget`` is excluded, and not by oversight: it is a record *of* this
    computation, so including it would make the estimate depend on its own result.
    """
    return (
        _json_tokens({"schema_version": pack.schema_version, "built_at": pack.built_at})
        + _json_tokens(pack.incident.model_dump())
        + _json_tokens(pack.security.model_dump())
        + _json_tokens([ranked.model_dump() for ranked in pack.evidence_ranking])
    )


def estimate_context_tokens(pack: IncidentContext) -> int:
    """Approximate token cost of a whole context.

    Deliberately **compositional**: the total is the sum of the individual item
    costs, not the estimate of one big concatenated string. That is what makes
    ``TokenBudget`` add up — removing one observation lowers the total by exactly
    that observation's cost, so "did this eviction help, and by how much" is
    answerable without a full recompute and the numbers a reviewer sees reconcile.
    The price is that per-item round-up makes this a few tokens higher than
    estimating the concatenation, which is the harmless direction.
    """
    return _envelope_tokens(pack) + sum(_section_tokens(s) for s in pack.sections.values())


# --- profiles ------------------------------------------------------------


def _resolve_profile(profile: str) -> str:
    """Normalise a caller's profile name. Case and padding only — never validated.

    An unknown name is returned as given rather than replaced with ``"default"``. The
    *ordering* falls back (see ``_section_priority``) so a typo cannot take the
    incident path down, but the name recorded in ``TokenBudget.profile`` stays the
    caller's, because recording ``"default"`` for a typo'd ``"rcaa"`` hides the bug
    forever: the projection would look correctly budgeted for a consumer that never
    asked for it. A wrong name in a decision trace is a bug someone can find.
    """
    return (profile or "").strip().lower() or "default"


def _section_priority(profile: str) -> tuple[str, ...]:
    """The section ordering for a profile, guaranteed total over every section.

    Two defences, both about failure modes that would otherwise surface as a
    ``KeyError`` on the incident path rather than as a test failure:

    * a section missing from a profile is appended in ``Source`` declaration order,
      so adding a twelfth source to the literal degrades that source's priority
      instead of crashing the consumer that budgets first;
    * a name in a profile that is not a real section is dropped, so a typo in
      ``PROFILES`` costs nothing at runtime.

    ``tests/test_context_tokenizer.py`` asserts every profile is an exact permutation
    of the sections, so neither defence should ever fire in a released build.
    """
    declared = PROFILES.get(profile)
    if declared is None:
        logger.debug("unknown context budget profile %r; using the default ordering", profile)
        declared = PROFILES["default"]
    known = tuple(name for name in declared if name in _ALL_SECTIONS)
    return known + tuple(name for name in _ALL_SECTIONS if name not in known)


# --- eviction ------------------------------------------------------------


def _eviction_plan(pack: IncidentContext, order: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    """The order observations are given up in: least valuable first.

    Identified by ``(section name, position)`` rather than by observation id, because
    an id is only unique per source and nothing forbids a section holding the same
    finding twice — evicting "by id" would then silently take both.

    Two groups, unranked first. An observation the ranker did not place is one nobody
    has judged worth keeping, so it is cheaper to lose than anything that earned a
    rank. Within each group the order is fully determined:

    * unranked — lowest-priority section first (the profile's only say in *which*
      observations go), then least confident, then oldest, then id;
    * ranked — highest ``rank`` number first (rank 1 is the most relevant, so the
      tail goes first), then lowest score, then id.

    The fallback for a context with no ranking at all is the unranked branch, which
    is emphatically **not** input order: input order is an artefact of collector
    scheduling, so budgeting against it would make the surviving evidence depend on
    which provider happened to answer first.

    Timestamps enter the key as ISO strings, not ``datetime`` objects. Comparing a
    naive datetime with an aware one raises ``TypeError``, and a sort key that can
    raise while trimming an incident's context is not acceptable; the string form
    orders identically for consistently-stamped data and never raises.
    """
    ranking = {ranked.observation_id: ranked for ranked in pack.evidence_ranking}
    priority = {name: index for index, name in enumerate(order)}

    unranked: list[tuple[tuple[int, float, str, str, int], str, int]] = []
    ranked_entries: list[tuple[tuple[int, float, str, int], str, int]] = []
    for name, section in pack.sections.items():
        for index, obs in enumerate(section.observations):
            ranked = ranking.get(obs.observation_id)
            if ranked is None:
                unranked.append(
                    (
                        (
                            -priority[name],
                            obs.confidence,
                            obs.timestamp.isoformat(),
                            obs.observation_id,
                            index,
                        ),
                        name,
                        index,
                    )
                )
            else:
                ranked_entries.append(
                    ((-ranked.rank, ranked.score, obs.observation_id, index), name, index)
                )

    unranked.sort(key=lambda entry: entry[0])
    ranked_entries.sort(key=lambda entry: entry[0])
    plan = [(name, index) for _, name, index in unranked]
    plan.extend((name, index) for _, name, index in ranked_entries)
    return tuple(plan)


def _budget_note(profile: str) -> str:
    """The coverage note stamped on a section that budgeting emptied.

    Deliberately says nothing about counts or which items went — ``TokenBudget`` is
    the record for that, and a note that varied with the details could not be applied
    twice idempotently. Its only job is to stop a ``COLLECTED`` section holding
    nothing from *reading* like a section nobody could query.
    """
    return f"{_NOTE_PREFIX} content trimmed to fit the {profile} projection"


def _with_note(section: ContextSection, note: str) -> ContextSection:
    """Attach the budget note, preserving any note the collector already left.

    Skips its own work when a budget note is already present, which is what keeps
    ``budget`` idempotent — a context projected twice must not accumulate notes, and
    an operator-facing string that grows on every pass is how a coverage note stops
    being read.
    """
    existing = section.provenance.coverage_note
    if existing and _NOTE_PREFIX in existing:
        return section
    combined = f"{existing}; {note}" if existing else note
    return section.model_copy(
        update={"provenance": section.provenance.model_copy(update={"coverage_note": combined})}
    )


def _without_budget_note(coverage_note: str | None) -> str | None:
    """A coverage note with any projection note removed.

    Lets ``_section_frame_tokens`` charge for what the *collector* said about
    coverage while ignoring what *this stage* appended — see that function for why
    charging for the latter breaks termination. Keyed on ``_NOTE_PREFIX`` rather than
    on position, so it works whether the projection note stands alone or was appended
    to a collector's own note.
    """
    if not coverage_note or _NOTE_PREFIX not in coverage_note:
        return coverage_note
    kept = [
        part
        for part in (segment.strip() for segment in coverage_note.split(";"))
        if part and _NOTE_PREFIX not in part
    ]
    return "; ".join(kept) or None


# --- the stage -----------------------------------------------------------


def budget(pack: IncidentContext, *, profile: str, max_tokens: int) -> IncidentContext:
    """Return a NEW context trimmed to fit ``max_tokens``. Never mutates ``pack``.

    Trimming happens in two phases, and the order between them is a correctness
    requirement rather than a preference:

    1. **Evict observations**, least valuable first, per ``_eviction_plan``.
    2. **Drop whole sections** — release their ``raw`` payload — lowest-priority
       section first, and *only* sections whose observations are already exhausted.

    A section's ``raw`` payload cannot be released while any of its observations
    survive, because the two are coupled: ``raw`` is the untouched provider payload
    an adapter rebuilds its prompt strings from (RCA's ``f"pod {pod}: cpu=..."``,
    RA-007's stream-order-dependent log truncation). Freeing it under a surviving
    observation would leave an adapter rendering that observation from a payload that
    is no longer there — which is worse than dropping the observation too, and is why
    phase 2 waits.

    Nothing is trimmed quietly. The returned context always carries a
    ``TokenBudget``, including when it was already under budget, so "this fits" is a
    recorded fact. A model given a trimmed evidence set with no indication that it
    was trimmed reasons as though it saw everything, and will report a signal as
    absent when it was merely evicted; ``dropped_sections`` and
    ``evicted_observation_ids`` exist so a consumer can always tell.

    Statuses are never rewritten. A ``COLLECTED`` section can come back holding zero
    observations, keeping its status and gaining a coverage note, because "we found
    things and had to drop them" is a different claim about the world from "we could
    not look" — the distinction the whole layer rests on.

    An unknown ``profile`` falls back to the ``"default"`` ordering rather than
    raising; a typo'd profile name must not cost an incident its context. The name is
    still recorded as given — see ``_resolve_profile``.

    The removal lists are **cumulative**: budgeting an already-budgeted context
    carries the earlier projection's ids and sections forward. A second pass must not
    launder away the first one's truncation record, and it cannot rediscover it — the
    evidence is already gone. That is also what makes this idempotent: re-budgeting
    to the same limit finds nothing left to do and returns the same context with the
    same record.
    """
    resolved = _resolve_profile(profile)
    order = _section_priority(resolved)
    note = _budget_note(resolved)

    sections = pack.sections  # a fresh dict per call — safe to read, cannot reach the pack
    frame_cost = {name: _section_frame_tokens(s) for name, s in sections.items()}
    raw_cost = {name: _json_tokens(s.raw) for name, s in sections.items()}
    obs_cost = {
        (name, index): _observation_tokens(obs)
        for name, s in sections.items()
        for index, obs in enumerate(s.observations)
    }
    remaining = {name: len(s.observations) for name, s in sections.items()}

    # Running total, kept incrementally so the loop's stop condition is O(1) per step
    # rather than a full re-estimate; equal by construction to
    # estimate_context_tokens(pack) because both are the same composition.
    total = (
        _envelope_tokens(pack)
        + sum(frame_cost.values())
        + sum(raw_cost.values())
        + sum(obs_cost.values())
    )

    evicted: set[tuple[str, int]] = set()
    evicted_ids: list[str] = []
    dropped: list[str] = []
    stripped: set[str] = set()
    noted: set[str] = set()

    # Both loops are monotonic: every step strictly lowers ``total``, because the
    # projection note they imply is not charged for (see ``_section_frame_tokens``).
    # That is what makes ``max_tokens`` reachable whenever it is at or above the floor,
    # and it is why neither loop needs a fixed-point re-check.
    for name, index in _eviction_plan(pack, order):
        if total <= max_tokens:
            break
        evicted.add((name, index))
        evicted_ids.append(sections[name].observations[index].observation_id)
        total -= obs_cost[(name, index)]
        remaining[name] -= 1
        if remaining[name] == 0:
            noted.add(name)

    for name in reversed(order):
        if total <= max_tokens:
            break
        # Not droppable while observations survive (see the docstring), and pointless
        # when there is no payload to release: listing a section in dropped_sections
        # that gave nothing up would report a loss that did not happen.
        if remaining[name] > 0 or raw_cost[name] == 0:
            continue
        dropped.append(name)
        stripped.add(name)
        total -= raw_cost[name]
        noted.add(name)

    # Walked in ``Source`` declaration order, not in set order: a set of strings
    # iterates in hash order, and this stage is required to be byte-reproducible.
    touched = {name for name, _ in evicted} | stripped | noted
    updates: dict[str, Any] = {}
    for name in (n for n in _ALL_SECTIONS if n in touched):
        section = sections[name]
        kept = tuple(
            obs for index, obs in enumerate(section.observations) if (name, index) not in evicted
        )
        rebuilt = section.model_copy(
            update={"observations": kept, "raw": None if name in stripped else section.raw}
        )
        updates[name] = _with_note(rebuilt, note) if name in noted else rebuilt

    trimmed = pack.model_copy(update=updates) if updates else pack

    previous = pack.token_budget
    all_dropped = tuple(
        dict.fromkeys((previous.dropped_sections if previous else ()) + tuple(dropped))
    )
    all_evicted = tuple(
        dict.fromkeys((previous.evicted_observation_ids if previous else ()) + tuple(evicted_ids))
    )
    return trimmed.model_copy(
        update={
            "token_budget": TokenBudget(
                profile=resolved,
                max_tokens=max_tokens,
                # Re-estimated from the result rather than reported from the running
                # total: this number is what the consumer is actually holding, and
                # deriving it independently means a bookkeeping drift shows up as
                # "trimmed but still over budget" instead of hiding behind its own
                # arithmetic.
                estimated_tokens=estimate_context_tokens(trimmed),
                truncated=bool(all_dropped or all_evicted)
                or (previous.truncated if previous else False),
                dropped_sections=all_dropped,
                evicted_observation_ids=all_evicted,
            )
        }
    )
