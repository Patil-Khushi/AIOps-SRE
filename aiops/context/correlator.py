"""Stage 3 — correlation: relate observations to each other and to the failing service.

A normalised ``Observation`` knows what it saw and nothing about whether it matters.
It cannot tell the ranker that "payment is erroring" happens to be a *direct
dependency* of the service that paged, nor that the same signature was independently
seen by both Loki and Jaeger. Without this stage the ranker has only recency,
severity and per-source confidence to work with, which reliably floats a fresh info
log from an unrelated service above a stale error in the one service the incident
actually flows through.

Why the results ride in ``Observation.metadata``
-----------------------------------------------
``Observation`` is frozen and its field set is deliberately source-agnostic and
fixed — adding ``topology_relation`` to it would push one consumer's reasoning
model into every collector's normalisation code, which is the exact coupling
``models.py`` refuses when it declines to reuse RA-007's ``Evidence``. A relation is
also a *judgement about a context*, not a property of a finding: the same log line is
``self`` in one incident and ``dependency`` in the next. So correlation output goes
in ``metadata`` under three keys, for the same reason ranking lives in a side-car
``RankedObservation`` rather than a ``score`` field.

Metadata keys written (stable — downstream adapters read these)
---------------------------------------------------------------
``topology_relation``
    One of ``self`` / ``dependency`` / ``dependent`` / ``unrelated`` / ``unknown``.
``sources_agreeing``
    Source names whose observations carry this signature, **including this
    observation's own source**. Own-source inclusion makes the cross-source test
    plainly ``len(sources_agreeing) > 1`` and means the tuple is never empty, so no
    consumer has to decide what an empty one meant. Note that a ``model_dump(mode=
    "json")`` round-trip returns it as a ``list`` — compare contents, not types.
``occurrences``
    How many observations in the whole context share this signature (``1`` = seen
    once). Counts repeats within a single source too: twenty identical timeout lines
    is a different fact from one.

Cross-source agreement is recorded per observation and not only in aggregate because
it is the strongest correlation signal this layer has — two independent backends
seeing one signature is far harder to explain away than either alone — and the
consumers that need it most (a ranker scoring one observation, an adapter rendering
one prompt line) hold an observation, not the whole index.

``unknown`` is not ``unrelated``
-------------------------------
These two are the same JSON string length and opposite claims about the world:

* ``unrelated`` — we had a dependency list, the observation names a service, and that
  service is not on it. A claim.
* ``unknown`` — we had no topology at all (empty ``dependencies``), or the
  observation names no service. The absence of an answer, not an answer.

Collapsing them would let "the CMDB has no CI record for this service" render to a
prompt as "this service is irrelevant to the incident" — a fabricated exculpation
that the ranker would then use to bury the evidence. Same discipline as
``SectionStatus``: absent is not empty.

One honest limitation: ``dependencies`` is one hop deep, so ``unrelated`` means "not a
direct neighbour by the topology we were given", not "provably irrelevant". A
consumer that needs transitive reach should ask ``aiops.tools.topology.paths`` rather
than read more into this label than it carries.

Why ``dependent`` needs the observation's own metadata
-----------------------------------------------------
``dependencies`` is one-directional — it is what the incident service *calls*. No
amount of reading it can reveal who calls the incident service, so a reverse edge has
to come from the observation itself: a checkout log line reading "payment charge
failed" carries ``service="checkout"`` plus an edge naming payment (see
``_UPSTREAM_EDGE_KEYS``), and that edge is the only evidence at this stage that
checkout sits *upstream*. When nothing declares such an edge, ``dependent`` is simply
never emitted — a coverage gap, not a claim that no dependents exist.

Purity
------
No clock, no registry, no environment. Every input arrives as a parameter, so the
same sections in produce byte-identical sections out; the eval harness compares runs
against their predecessors and cannot do that if a stage has a hidden input.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from typing import Any, Literal

from aiops.context.models import Observation
from aiops.context.pack import ContextSection

logger = logging.getLogger(__name__)

TopologyRelation = Literal["self", "dependency", "dependent", "unrelated", "unknown"]
"""How an observation's service sits relative to the incident's service.

Intentionally the same five values as ``agents/log_correlation/evidence.py``'s
``TopologyRelation``, so RA-007's context adapter can pass the label straight through
instead of translating a second vocabulary. Redeclared rather than imported because
``aiops/`` may never import ``agents/`` (``tests/test_layering.py``), and the two are
not required to stay equal — if RA-007 ever adds a relation of its own, its adapter
maps it, and neither side breaks.
"""

_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_SUFFIX_RE = re.compile(r"(?:service|svc)$")

_UPSTREAM_EDGE_KEYS: tuple[str, ...] = (
    "depends_on",
    "dependencies",
    "downstream",
    "downstream_service",
    "calls",
    "target_service",
)
"""Metadata keys under which an observation may declare an edge *out of* its own
service. Several spellings are accepted because the sources that produce such edges
disagree — the CMDB says ``dependencies``, a trace span says ``target_service``, a log
normaliser writing "checkout → payment" says ``depends_on`` — and a stage that
recognised only one spelling would silently never emit ``dependent``, which reads as
"this incident has no upstream callers" rather than as the parsing gap it is."""


def normalize_service_name(name: str) -> str:
    """Canonical spelling of a service name, for comparison rather than display.

    Lower-cases, collapses every separator to ``-``, and drops a trailing
    ``service``/``svc`` from the last token, so ``Payment_Service``,
    ``payment-service`` and ``payment`` all canonicalise to ``payment``.

    Deliberately *not* a full alias resolver: it keeps qualifying prefixes, so
    ``ecommerce-payment-service`` canonicalises to ``ecommerce-payment`` and stays
    distinguishable from a bare ``payment``. Tolerating that prefix is
    ``services_match``'s job, because equality of canonical strings is the wrong tool
    for it — see there.
    """
    return "-".join(_service_tokens(name))


def services_match(left: str, right: str) -> bool:
    """Whether two spellings plausibly name the same service.

    One service wears three names in this codebase, and all three are in live use at
    once: telemetry labels carry ``OTEL_SERVICE_NAME`` (``ecommerce-payment-service``),
    truth files and alert payloads use the bare form (``payment-service``), and parts
    of the CMDB graph use the short form (``payment``) — see ``_DEPENDENCIES_MAPPING``
    in ``aiops/tools/mock_providers.py``, which documents that its own lookup is an
    exact dict hit and therefore misses across those spellings. A naive ``==`` here
    would label a direct dependency ``unrelated`` and the ranker would bury the single
    most relevant piece of evidence in the incident.

    Two rules, both anchored so the tolerance cannot run away:

    1. Equal once separators are removed — ``product-catalog`` vs
       ``productcatalogservice``. Handles vocabularies that disagree only about
       punctuation.
    2. One name's tokens are a *suffix* of the other's — ``payment`` vs
       ``ecommerce-payment-service``. Anchoring at the tail is what keeps
       ``mock-payment-gateway`` from matching ``payment``: it shares a token but not
       the last one, and it is genuinely a different service.

    The tolerance is asymmetric on purpose. A false negative costs real evidence its
    rank; a false positive costs one extra service's evidence a place in a prompt.
    The known price is that a qualified name matches its unqualified twin even when
    two deployments legitimately share a tail — ``frontend`` (astronomy shop) and
    ``ecommerce-frontend`` are exactly that pair, which is why
    ``_DEPENDENCIES_MAPPING`` refuses to alias them. They never appear in one
    incident (the astronomy workloads are scaled to zero), and a correlation label is
    a ranking hint rather than a gate, so the trade is worth taking here even though
    it would not be worth taking in the dependency table.

    An unnamed service (``""``) matches nothing, including another ``""``: two
    observations that both failed to record a service are not evidence of a shared
    one.
    """
    return _tokens_match(_service_tokens(left), _service_tokens(right))


def correlate(
    sections: dict[str, ContextSection],
    *,
    incident_service: str,
    dependencies: Sequence[str] = (),
) -> dict[str, ContextSection]:
    """Return new sections whose observations carry correlation metadata.

    ``dependencies`` is the incident service's *direct, downstream* dependencies as
    resolved by the topology seam — one hop, the shape
    ``TopologyResult.dependencies`` already has. Empty means "no topology", which
    yields ``unknown`` rather than ``unrelated`` for everything that is not the
    incident service itself.

    Sections whose status is not ``usable`` are returned **as-is, unannotated**, and
    their observations are excluded from the signature index. Both halves matter:
    annotating them would dress content from a ``FAILED`` provider up with
    ``occurrences=3`` as though it had been corroborated, and counting them would let
    a half-delivered payload inflate the agreement score of a signature that only one
    source actually stands behind. A consumer therefore reads these keys with
    ``.get()`` — their absence marks an untrusted section, which is information.

    Idempotent: correlating an already-correlated context recomputes the same three
    keys from the same inputs and overwrites them, so a shadow-mode rebuild cannot
    accumulate stale relations.
    """
    carriers, occurrences = _index(sections)
    incident_tokens = _service_tokens(incident_service)
    # Drop entries that name nothing (``None``, ``""``, a stray dict from a schema
    # surprise) *before* the loop, so "we were handed a dependency list" is not
    # confused with "we were handed a list of unusable entries" a thousand times over.
    dependency_tokens = tuple(
        tokens for tokens in (_service_tokens(dep) for dep in dependencies) if tokens
    )

    correlated: dict[str, ContextSection] = {}
    # Iterates in the caller's key order rather than sorted order: this dict is fed
    # straight back into ``IncidentContext``'s field-per-source layout, and a stage
    # that quietly reordered sections would make two otherwise identical pipeline
    # runs serialise differently.
    for name, section in sections.items():
        if not section.status.usable or not section.observations:
            correlated[name] = section
            continue
        correlated[name] = section.model_copy(
            update={
                "observations": tuple(
                    _annotate(
                        observation,
                        incident=incident_tokens,
                        dependencies=dependency_tokens,
                        carriers=carriers,
                        occurrences=occurrences,
                    )
                    for observation in section.observations
                )
            }
        )
    return correlated


def cross_source_signatures(sections: dict[str, ContextSection]) -> dict[str, tuple[str, ...]]:
    """``signature -> sources carrying it``, for signatures present in 2+ sources.

    The aggregate companion to the per-observation ``sources_agreeing`` key: what a
    decision trace or a war-room summary needs in order to say "``db timeout`` was
    seen in logs and traces" without walking every observation itself.

    Keys are sorted, and so are the source tuples, so the mapping renders identically
    on every run over the same context. Only ``usable`` sections contribute — see
    ``correlate``.
    """
    carriers, _ = _index(sections)
    return {
        signature: sources for signature, sources in sorted(carriers.items()) if len(sources) > 1
    }


def _index(
    sections: dict[str, ContextSection],
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    """Build ``signature -> sorted sources`` and ``signature -> total occurrences``.

    Both indexes come out of one pass because they are two readings of the same
    grouping and computing them separately is how they drift apart.

    Signatures are grouped by exact string (after stripping surrounding whitespace),
    not re-normalised here. The normalisation stage owns what a signature means — it
    is the thing that strips UUIDs and latency numbers so two log lines collapse — and
    a second, different normalisation at this layer would make the two stages
    disagree about identity while both looked correct in isolation.

    A blank signature is skipped rather than indexed under ``""``. Otherwise every
    observation an adapter failed to signature would "agree" with every other one,
    manufacturing the strongest correlation signal the layer has out of missing data.
    """
    carriers: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for section in sections.values():
        if not section.status.usable:
            continue
        for observation in section.observations:
            signature = observation.signature.strip()
            if not signature:
                continue
            # Keyed on the observation's own ``source``, not on the caller's dict key:
            # ``source`` is what an adapter groups on downstream, so the two views of
            # "which source said this" stay consistent even if a caller keys the map
            # by something else.
            carriers.setdefault(signature, set()).add(observation.source)
            counts[signature] = counts.get(signature, 0) + 1
    return {signature: tuple(sorted(sources)) for signature, sources in carriers.items()}, counts


def _annotate(
    observation: Observation,
    *,
    incident: tuple[str, ...],
    dependencies: tuple[tuple[str, ...], ...],
    carriers: dict[str, tuple[str, ...]],
    occurrences: dict[str, int],
) -> Observation:
    """One observation, plus its three correlation keys.

    Builds a fresh ``metadata`` dict rather than updating the existing one: the input
    is frozen but its ``metadata`` dict is not, and mutating it in place would edit an
    observation that other sections, an earlier pipeline stage or a cache entry may
    still hold. Pre-existing keys survive; ours win on collision.
    """
    try:
        signature = observation.signature.strip()
        metadata: dict[str, Any] = dict(observation.metadata)
        metadata["topology_relation"] = _relation(
            observation, incident=incident, dependencies=dependencies
        )
        # Fall back to the observation's own source for an unsignatured observation:
        # it is truthfully the only source carrying that (absent) signature, and it
        # keeps "own source is always present" true for every annotated observation.
        metadata["sources_agreeing"] = carriers.get(signature) or (observation.source,)
        metadata["occurrences"] = occurrences.get(signature, 1)
        return observation.model_copy(update={"metadata": metadata})
    except Exception:  # pragma: no cover - defended below, not expected
        # Provider-echoed metadata is the one genuinely untrusted input here (the
        # section and observation shapes are pydantic-validated). A hostile or merely
        # weird value must cost this observation its correlation keys, never the
        # section or the incident. The unannotated observation still travels, and a
        # consumer reading with ``.get()`` sees exactly what happened.
        logger.debug("correlation failed for observation %s", observation.observation_id)
        return observation


def _relation(
    observation: Observation,
    *,
    incident: tuple[str, ...],
    dependencies: tuple[tuple[str, ...], ...],
) -> TopologyRelation:
    """Classify one observation's service against the incident's topology.

    Precedence, and why:

    1. ``self`` — the observation is about the failing service.
    2. ``dependency`` — its service is on the incident's dependency list. Checked
       before ``dependent`` so a cyclic pair (A calls B, B calls A) resolves toward
       the topology seam's answer about *this* incident rather than toward a
       single observation's metadata hint.
    3. ``dependent`` — the observation declares an edge into the incident service.
    4. ``unknown`` — nothing to relate: no service name, or no topology and no edge.
    5. ``unrelated`` — named service, topology present, not adjacent.
    """
    service = _service_tokens(observation.service)
    if not service:
        # No service name is a gap in the observation, not a fact about the topology.
        return "unknown"
    if _tokens_match(service, incident):
        return "self"
    if any(_tokens_match(service, dependency) for dependency in dependencies):
        return "dependency"
    if incident and any(
        _tokens_match(_service_tokens(target), incident)
        for target in _declared_edges(observation.metadata)
    ):
        return "dependent"
    if not dependencies:
        return "unknown"
    return "unrelated"


def _declared_edges(metadata: dict[str, Any]) -> Iterator[str]:
    """Service names this observation says its own service depends on.

    Accepts a bare string or any sequence of strings, and ignores anything else. The
    values come from provider payloads, so an ``int`` or a nested dict under one of
    these keys is a schema surprise to skip — never a reason to raise on the incident
    path.
    """
    for key in _UPSTREAM_EDGE_KEYS:
        value = metadata.get(key)
        if isinstance(value, str):
            yield value
        elif isinstance(value, list | tuple | set | frozenset):
            yield from (item for item in value if isinstance(item, str))


def _service_tokens(name: object) -> tuple[str, ...]:
    """Comparable tokens for a service name, tail-normalised.

    ``payment`` and ``payment-service`` → ``("payment",)``;
    ``ecommerce-payment-service`` → ``("ecommerce", "payment")``;
    ``productcatalogservice`` → ``("productcatalog",)``.

    Takes ``object`` rather than ``str`` because dependency lists and metadata edges
    are provider-echoed: a ``None`` in ``raw["dependencies"]`` is a payload bug, and
    the useful reading of it is "names no service" — which matches nothing — rather
    than an ``AttributeError`` two frames up on the incident path.

    Only the *last* token is suffix-stripped, so ``service-mesh`` keeps both tokens
    and only the ``-service`` that means "this thing is a service" is dropped.
    """
    if not isinstance(name, str):
        return ()
    tokens = [part for part in _SEPARATOR_RE.split(name.strip().lower()) if part]
    if not tokens:
        return ()
    trimmed = _SUFFIX_RE.sub("", tokens[-1])
    stripped = tokens[:-1] + ([trimmed] if trimmed else [])
    # A service named literally ``service`` strips to nothing; keep the original so it
    # can still match itself instead of silently becoming unnameable.
    return tuple(stripped) if stripped else tuple(tokens)


def _tokens_match(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Suffix-anchored token comparison — the mechanics behind ``services_match``."""
    if not left or not right:
        return False
    if "".join(left) == "".join(right):
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[-len(shorter) :] == shorter
