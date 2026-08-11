"""Stage 4 — deterministic, explainable relevance ranking.

Why this stage exists
---------------------
Stage 7 has to throw evidence away: eleven sources' worth of observations do not
fit in any agent's prompt. Something must decide what survives, and if nothing
decides, the survivors are whichever sections the collectors happened to finish
first. This module is that decision, made once for every agent instead of four
times in four subtly different ways — which is what the RCA agent and RA-007 did
before this layer existed, and why they could reason about the same failure from
different evidence.

The ordering is not cosmetic. A ranking that puts a routine info log above the
cross-source error signature costs an agent its best evidence *silently*: the
prompt still looks full, the verdict is still confident, and nothing in the output
says the trigger was trimmed away.

Purity is the point
-------------------
Nothing here reads a clock, an environment variable or the registry. ``now`` and
``incident_service`` arrive as parameters so a ranking can be re-derived from a
stored context and compared against the ranking that shipped. A ``datetime.now()``
inside this module would make every eval run differ from the previous one for
reasons unrelated to the code under test, and "the ranking changed" would stop
being a signal.

Why ties are broken explicitly
------------------------------
The collectors fan out concurrently, so the order observations arrive in varies
between runs over the *same* incident. Python's sort is stable, which means an
unbroken tie silently inherits that arrival order — and the flake does not surface
here, it surfaces two stages later as an eval whose top-5 evidence set changes with
no code change behind it. Every tie is therefore broken on fields that are
properties of the observation rather than of the schedule: id first, then source,
signature and timestamp for the case where two collectors emit the same identity
twice.

The four factors, and what is deliberately missing
--------------------------------------------------
Confidence, recency, topology distance, cross-source agreement. Severity is
**not** a fifth factor: the collectors already fold it into
``Observation.confidence`` (a firing alert arrives more confident than one info
line), so scoring it again here would count the same fact twice and quietly double
the weight of whichever source happens to report the loudest severities.

Every score explains itself
---------------------------
``rationale`` is mandatory, following the convention
``agents/log_correlation/confidence.py`` established: a number handed to a human or
to a prompt must be able to say where it came from. An unexplainable 0.83 cannot be
reviewed, and a ranking nobody can audit is a ranking nobody should trust with an
incident. The rationale therefore names all four factors — including the ones that
did *not* move the score, because "corroboration unchecked" is the most useful line
in the explanation when stage 3 was skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aiops.context.models import Observation
from aiops.context.pack import RankedObservation

logger = logging.getLogger(__name__)

# --- weights -------------------------------------------------------------
#
# The four weights sum to exactly 1.0, which is what bounds a score to [0, 1]
# without a clamp doing the work — every factor below is itself normalised to
# [0, 1]. ``tests/test_context_ranker.py`` pins the sum, so retuning one weight
# without rebalancing the others fails a test rather than quietly producing
# scores that no longer compare against yesterday's.

WEIGHT_AGREEMENT = 0.35
"""Cross-source agreement is the strongest signal this pipeline can produce, so it
carries the largest weight. One backend reporting an error can be that backend's
instrumentation; the same signature in logs *and* traces cannot be. This agrees
with RA-007, where cross-source agreement is also the single biggest increment."""

WEIGHT_CONFIDENCE = 0.30
"""The collector's own judgement, second-heaviest. It is the only factor derived
from the raw payload — a firing alert, an error-severity line, a span that actually
failed — and stage 1 is the only stage that ever sees that payload. Weighting it
below agreement keeps a single strong-looking sample from outranking a corroborated
pattern."""

WEIGHT_TOPOLOGY = 0.20
"""Proximity to the failing service. Deliberately below confidence: topology
answers "could this be related?", not "is this a problem?", and a healthy direct
dependency is nearer than a broken distant one without being more relevant."""

WEIGHT_RECENCY = 0.15
"""Smallest on purpose, and the choice most likely to be questioned.

Recency is *not* a proxy for relevance during an incident. The earliest error in
the window is very often the trigger — RA-007 scores exactly that fact
(``error_severity_first``) — so an aggressive recency term would bury the cause
under its own downstream symptoms. Recency's real job here is to demote stale
carry-over evidence (an hour-old deployment record, a cached incident-history hit)
below the signals from the failure itself, and 0.15 is enough for that."""

SCORE_PRECISION = 6
"""Decimal places every score is rounded to.

Not for determinism — IEEE arithmetic over the same inputs is already reproducible.
It is so a score reads as a number a human can compare: an eval diff showing
``0.7000000000000001`` where the previous run wrote ``0.7`` reports a change that
did not happen."""

MAX_SCORE = round(
    WEIGHT_AGREEMENT + WEIGHT_CONFIDENCE + WEIGHT_TOPOLOGY + WEIGHT_RECENCY, SCORE_PRECISION
)
"""1.0, and rounded rather than summed for a reason worth knowing.

The four weights are decimal literals, and in binary floating point
``0.35 + 0.30 + 0.20 + 0.15 == 0.9999999999999999``. Left unrounded, a maximal
observation's total rounds to ``1.0`` at ``SCORE_PRECISION`` and therefore *exceeds*
the ceiling this constant is supposed to define — a one-ULP inequality that would
surface as a consumer's ``score <= MAX_SCORE`` assertion failing on exactly the best
evidence in the incident. Rounding the ceiling the same way the scores are rounded
keeps the two comparable.

Named rather than hard-coded so the clamp cites the weights instead of a literal
that would drift the moment someone retunes one."""

# --- recency -------------------------------------------------------------

RECENCY_HALF_LIFE = timedelta(minutes=15)
"""Matched to the incident window this repo actually queries over.

RA-007's CLI defaults to ``--window-minutes 15`` and ``rca_agent/evidence.py``
queries ``now - 15min``, so 15 minutes is the width of the evidence set this stage
ranks. A half-life equal to the window means the oldest observation *in* the window
keeps about half its recency term while the newest keeps all of it — recency
separates within the window without dominating it. A much shorter half-life would
turn ranking into "newest first" and bury the trigger; a much longer one would
flatten the term to a constant and stop distinguishing this incident's signals from
evidence that predates it.

Exponential rather than a cliff edge: decay asymptotes towards zero instead of
reaching it, so a two-hour-old deployment commit is demoted but still ranked by its
other three factors rather than erased."""

# --- topology ------------------------------------------------------------

TOPOLOGY_RELATION_SCORES: dict[str, float] = {
    "self": 1.0,
    "dependency": 0.85,
    "dependent": 0.55,
    "unrelated": 0.10,
}
"""Vocabulary borrowed from RA-007's ``TopologyRelation`` (self / dependency /
dependent / unrelated / unknown) so the correlator, the ranker and the Log
Correlation agent all mean the same thing by "dependency". ``unknown`` is absent
here on purpose — it is not a distance, it is the absence of one, and it resolves
to ``TOPOLOGY_UNKNOWN_SCORE`` below.

Why ``self`` outranks ``dependency``, where RA-007's per-item confidence does the
opposite (+0.1 for depth 1 versus +0.05 for depth 0): the two scores answer
different questions. RA-007 asks "which signal is most diagnostic", and a fault in
a dependency points somewhere. This score decides *what survives a token budget*,
and dropping the failing service's own telemetry in favour of a neighbour's is
never a defensible trim — an agent asked about checkout must see checkout.

``dependent`` sits well below ``dependency`` because a caller of the failing
service is usually a victim reporting the symptom, not a candidate cause."""

TOPOLOGY_UNKNOWN_SCORE = 0.50
"""Neutral, because "we could not place this service" is not "this service is
unrelated". Scoring an unplaced observation as unrelated would let a skipped or
failed stage 3 read as a finding about the topology, which is the same
absent-is-not-empty error ``SectionStatus`` exists to prevent one stage earlier."""

TOPOLOGY_HOP_PENALTY = 0.10
"""Per hop beyond the first, when the correlator recorded a depth. Two hops of
indirection is real distance and worth reflecting, but the penalty is small because
hop counts come from whatever topology provider answered and a gRPC-only view
routinely reports a longer path than the truth."""

TOPOLOGY_FLOOR_SCORE = 0.15
"""Floor for a connected-but-distant service. Kept strictly above ``unrelated``:
collapsing the two would assert that a service six hops away is no more relevant
than one with no path at all, and the topology data does not support that claim."""

# --- cross-source agreement ---------------------------------------------

AGREEMENT_MULTI_SOURCE = 1.00
"""Three or more sources carrying the same signature. Only marginally above two,
because the second independent source is what rules out a per-backend artifact; the
third mostly confirms what the second already established."""

AGREEMENT_TWO_SOURCES = 0.85
"""Two sources agreeing. The jump from one source (0.30) to two is the largest step
in this module, and intentionally so — it is the strongest inference the pipeline
draws."""

AGREEMENT_SINGLE_SOURCE = 0.30
"""The correlator looked and found no other source carrying this signature. A real,
if weak, claim about the evidence set."""

AGREEMENT_UNKNOWN_SCORE = 0.40
"""No agreement metadata at all — stage 3 was skipped, or it did not annotate this
observation.

Deliberately *above* ``AGREEMENT_SINGLE_SOURCE``: reporting an unchecked
observation as single-source would assert that nothing corroborates it, which is a
claim nobody made. This matters even though the shift is uniform when stage 3 is
skipped entirely, because a correlator that annotates some sections and not others
would otherwise rank its own annotated single-source findings *below* the ones it
never looked at."""

UNSCORABLE_RATIONALE = "not scorable (malformed observation); ranked last"
"""Rationale for an observation this module could not read at all.

Ranked last rather than dropped or scored neutrally. Dropped would lose evidence a
consumer might still render from ``ContextSection.raw``; neutral would let data we
demonstrably cannot parse outrank evidence we can."""

_RATIONALE_NAME_LIMIT = 60
"""Cap on any provider-supplied name interpolated into a rationale."""


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def _clean(text: object) -> str:
    """Flatten an untrusted name into one bounded, single-line token.

    Rationales are rendered into prompts, Slack bodies and audit lines, and stage 6
    redacts ``Observation`` fields rather than ranking rationales — so a service
    name arriving from an alert payload with a newline in it would be free to forge
    structure in a prompt built from these strings. Collapsing whitespace and
    bounding the length here is cheaper than teaching every consumer to distrust
    its own rationale.
    """
    flat = " ".join(str(text).split())
    if not flat:
        return "?"
    return flat if len(flat) <= _RATIONALE_NAME_LIMIT else flat[:_RATIONALE_NAME_LIMIT] + "…"


def _signed_age_seconds(now: datetime, timestamp: datetime) -> float:
    """Seconds from ``timestamp`` to ``now``; negative when the stamp is in the future.

    Returns the *signed* value so the caller can tell clock skew from freshness —
    both clamp to a zero age, but only one is worth saying out loud in a rationale.

    Mixed awareness is the trap this function exists for. Prometheus and Loki hand
    back timezone-aware stamps, while a fixture or a provider that formats without
    an offset produces naive ones, and subtracting one from the other raises
    ``TypeError``. On the incident path that would turn a single badly-formatted
    sample into a ranking-wide exception, so a naive value is read as UTC — the only
    reading consistent with this repo, where every provider queries in UTC.
    """
    if now.tzinfo is None and timestamp.tzinfo is not None:
        now = now.replace(tzinfo=UTC)
    elif now.tzinfo is not None and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (now - timestamp).total_seconds()


def _format_age(seconds: float) -> str:
    """Age in the coarsest unit that still says something ("4m", "3h", "2d").

    Coarse on purpose: a rationale is read by a human deciding whether to trust the
    ordering, and "4m" answers that question while "247.318s" does not. The buckets
    are wide enough that a sub-second difference between two runs cannot change the
    string, which keeps the rationale as reproducible as the score.
    """
    if seconds < 1:
        return "0s"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 90 * 60:
        return f"{int(seconds // 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _agreement_factor(metadata: dict[str, object]) -> tuple[float, str]:
    """Score ``metadata["sources_agreeing"]``, written by stage 3.

    Accepts the three shapes a correlator plausibly writes — a sequence of source
    names, an integer count, or a bare cross-source boolean — because this module
    is on the incident path and a shape it does not recognise must cost precision,
    not raise. Anything else degrades to "unchecked".
    """
    if "sources_agreeing" not in metadata:
        return AGREEMENT_UNKNOWN_SCORE, "corroboration unchecked"

    raw = metadata.get("sources_agreeing")
    names: tuple[str, ...] = ()
    count: int

    # ``bool`` before ``int``, because bool *is* an int subclass: a correlator that
    # wrote the RA-007-style ``cross_source=True`` flag would otherwise be read as
    # "one source agrees", inverting the very fact it was trying to record.
    if isinstance(raw, bool):
        count = 2 if raw else 1
    elif isinstance(raw, list | tuple | set | frozenset):
        # Sorted so the rationale does not vary with the order stage 3 happened to
        # write its set in — two runs over identical evidence must produce identical
        # strings, not merely identical scores.
        names = tuple(sorted({_clean(item) for item in raw if str(item).strip()}))
        count = len(names)
    elif isinstance(raw, int | float):
        count = int(raw)
    else:
        return AGREEMENT_UNKNOWN_SCORE, "corroboration unchecked (unrecognised metadata)"

    if count >= 3:
        return AGREEMENT_MULTI_SOURCE, f"cross-source agreement ({_join_sources(names, count)})"
    if count == 2:
        return AGREEMENT_TWO_SOURCES, f"cross-source agreement ({_join_sources(names, count)})"
    return AGREEMENT_SINGLE_SOURCE, "single-source only"


def _join_sources(names: tuple[str, ...], count: int) -> str:
    """Name the agreeing sources when stage 3 recorded them, else state the count."""
    return "+".join(names) if names else f"{count} sources"


def _recency_factor(observation: Observation, *, now: datetime) -> tuple[float, str]:
    """Exponential decay on the observation's age, with clock skew handled explicitly.

    A provider whose clock runs ahead of this process yields a negative age. Left
    alone that would raise the decay above 1.0 and push the total score past
    ``MAX_SCORE``, so a future stamp is treated as "now": the freshest an
    observation is allowed to be, never fresher.
    """
    signed = _signed_age_seconds(now, observation.timestamp)
    age = max(signed, 0.0)
    decay = _clamp(0.5 ** (age / RECENCY_HALF_LIFE.total_seconds()))
    if signed < 0:
        return decay, f"future-dated by {_format_age(-signed)} (clock skew); treated as current"
    return decay, f"{_format_age(age)} old"


def _relation_of(metadata: dict[str, object], observation: Observation, incident: str) -> str:
    """The topology relation to score, falling back to what is knowable without stage 3.

    When the correlator did not run, an observation *made on* the failing service is
    still definitionally zero hops from it — that fact comes from
    ``Observation.service``, not from topology. Inferring ``self`` there keeps the
    ranking useful in a degraded build instead of flattening every observation onto
    the same neutral distance.

    Nothing beyond that is guessed: a different service name proves nothing about
    the path between the two, so it stays ``unknown`` rather than defaulting to
    ``unrelated``.
    """
    raw = metadata.get("topology_relation")
    if isinstance(raw, str):
        relation = raw.strip().lower()
        if relation in TOPOLOGY_RELATION_SCORES or relation == "unknown":
            return relation
    if incident.strip() and observation.service.strip().casefold() == incident.strip().casefold():
        return "self"
    return "unknown"


def _depth_of(metadata: dict[str, object]) -> int | None:
    """Hop count from the correlator, or ``None`` when it recorded none.

    Reads ``topology_depth`` and ``topology_hops`` because RA-007's
    ``TopologyContext`` calls the field ``depth`` while the topology providers talk
    in hops, and this module should not be the reason the two vocabularies have to
    be reconciled upstream first.
    """
    for key in ("topology_depth", "topology_hops"):
        if key not in metadata:
            continue
        raw = metadata.get(key)
        # ``bool`` is an int subclass and a depth of ``True`` is nobody's intent.
        if isinstance(raw, bool) or not isinstance(raw, int | float | str):
            continue
        try:
            depth = int(raw)
        except (TypeError, ValueError):
            continue
        return max(depth, 0)
    return None


def _topology_factor(
    metadata: dict[str, object], observation: Observation, incident_service: str
) -> tuple[float, str]:
    """Score how near this observation sits to the failing service."""
    incident = _clean(incident_service)
    relation = _relation_of(metadata, observation, incident_service)
    depth = _depth_of(metadata)

    if relation == "unknown":
        return TOPOLOGY_UNKNOWN_SCORE, "topology unplaced"

    score = TOPOLOGY_RELATION_SCORES[relation]
    # Only the through-the-graph relations can be more than one hop away; "self" is
    # zero by definition and "unrelated" has no path to measure, so applying a hop
    # penalty to either would be scoring a number that means nothing.
    if depth is not None and depth > 1 and relation in ("dependency", "dependent"):
        score = max(score - TOPOLOGY_HOP_PENALTY * (depth - 1), TOPOLOGY_FLOOR_SCORE)

    if relation == "self":
        return score, f"observed on {incident} itself"
    if relation == "unrelated":
        return score, f"unrelated to {incident}"
    hops = depth if depth is not None else 1
    plural = "" if hops == 1 else "s"
    side = "dependency of" if relation == "dependency" else "caller of"
    return score, f"{hops} hop{plural} from {incident} ({side} the failing service)"


def _confidence_factor(observation: Observation) -> tuple[float, str]:
    """The collector's own strength score, named in the rationale so the arithmetic adds up.

    Included in every rationale even though the example format omits it: it is a
    weighted term on every single observation, so a reader who cannot see it cannot
    reproduce the total.
    """
    value = _clamp(float(observation.confidence))
    return value, f"{observation.source} confidence {value:.2f}"


def score_one(
    observation: Observation, *, now: datetime, incident_service: str
) -> tuple[float, str]:
    """(score, rationale) for a single observation.

    Pure and total: no clock, no config, no exceptions. ``now`` is a parameter so
    the same observation always scores the same at the same instant, which is what
    lets the eval harness compare two runs at all.
    """
    try:
        # ``metadata`` is a plain dict by model contract, but this stage reads a
        # payload three stages downstream of a provider, and defending the read is
        # cheaper than a ranking that dies on one surprising section.
        metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
        factors = (
            (WEIGHT_AGREEMENT, _agreement_factor(metadata)),
            (WEIGHT_RECENCY, _recency_factor(observation, now=now)),
            (WEIGHT_TOPOLOGY, _topology_factor(metadata, observation, incident_service)),
            (WEIGHT_CONFIDENCE, _confidence_factor(observation)),
        )
    except Exception:  # pragma: no cover - the helpers above are individually total
        logger.debug(
            "ranker could not score observation %s; ranking it last",
            getattr(observation, "observation_id", "?"),
            exc_info=True,
        )
        return 0.0, UNSCORABLE_RATIONALE

    raw = sum(weight * value for weight, (value, _note) in factors)
    # Round *then* clamp, not the other way round. Rounding a total that already sits
    # at the ceiling can lift it one ULP above ``MAX_SCORE`` (see that constant for
    # the arithmetic), so clamping first would leave the published invariant
    # "score <= MAX_SCORE" false for precisely the best evidence in the incident.
    score = _clamp(round(raw, SCORE_PRECISION), 0.0, MAX_SCORE)
    # Agreement first, then age, then topology, then confidence — the order the
    # design's example rationale uses, and stable so two rationales can be diffed
    # clause by clause.
    rationale = "; ".join(note for _weight, (_value, note) in factors)
    return score, rationale


def _sort_key(item: tuple[Observation, float, str]) -> tuple[float, str, str, str, str]:
    """Total, input-order-independent ordering: best score first, ties by identity.

    ``timestamp`` enters the key as an ISO *string*, never as a datetime. Sorting
    mixed naive and aware datetimes raises ``TypeError``, and a raise from inside
    ``sorted`` would take down the ranking for the whole incident — exactly the
    failure this layer promises never to have.
    """
    observation, score, _rationale = item
    return (
        -score,
        observation.observation_id,
        str(observation.source),
        observation.signature,
        observation.timestamp.isoformat(),
    )


def rank(
    observations: Sequence[Observation],
    *,
    now: datetime,
    incident_service: str,
) -> tuple[RankedObservation, ...]:
    """Score and order observations, highest first. Deterministic.

    Ranks are 1-based and contiguous over what came back, so a consumer can treat
    ``rank <= n`` as "the top n" without checking for gaps.

    Input order never reaches the output: every tie is broken on the observation's
    own fields (see ``_sort_key``). This is the property the eval harness depends
    on, because the collectors fan out concurrently and hand this function a
    different order on every run over the same incident.
    """
    scored: list[tuple[Observation, float, str]] = []
    for observation in observations:
        try:
            score, rationale = score_one(observation, now=now, incident_service=incident_service)
        except Exception:  # pragma: no cover - score_one is total; belt and braces
            logger.debug("skipping unrankable observation", exc_info=True)
            continue
        scored.append((observation, score, rationale))

    return tuple(
        RankedObservation(
            observation_id=observation.observation_id,
            score=score,
            rank=position,
            rationale=rationale,
        )
        for position, (observation, score, rationale) in enumerate(
            sorted(scored, key=_sort_key), start=1
        )
    )
