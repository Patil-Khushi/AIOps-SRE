"""The small, reusable pieces of the Context Engineering Layer.

``Observation`` is the common shape every collector normalises into, and
``ContextSection`` is one source's contribution to a context. The aggregate that
holds them — ``IncidentContext``, the "Context Pack" — lives in ``pack.py``,
because it is what every agent touches and deserves its own file and its own
focused tests.

Why an aiops-owned Observation rather than reusing RA-007's ``Evidence``
-----------------------------------------------------------------------
``agents/log_correlation/evidence.py`` already has a rich, frozen, identity-hashed
evidence model. It is deliberately not reused here for two reasons, one structural
and one about ownership:

* ``aiops/`` may never import ``agents/`` (enforced by
  ``tests/test_layering.py``). The dependency arrow is ``demo/ → agents/ → aiops/``
  and reversing it for one model would break the layering the whole platform rests
  on.
* ``Evidence`` carries RA-007's own vocabulary — ``SupportingTelemetry``,
  ``TopologyRelation``, its ``SignalType`` literals. Those encode how the Log
  Correlation agent reasons about a signal. Hoisting them into the platform would
  make every other agent inherit one agent's reasoning model.

So ``Observation`` is a flatter, source-agnostic counterpart. RA-007 keeps
``Evidence`` as its own richer view; nothing here replaces it.

Status vocabulary
-----------------
``SectionStatus`` follows the four-way split this repo uses everywhere
(``aiops/tools/topology/base.py::ProviderStatus``,
``change_context/base.py``, ``incident_history/base.py``): "asked and the answer is
genuinely nothing" is a different fact from "could not ask". It adds a fifth state,
``NOT_REQUESTED``, that those seams do not need — because a caller here names the
subset of sections it wants, so "nobody asked for this" is a real and common
outcome that must not be confused with "asked and got nothing".
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SectionStatus(StrEnum):
    """Outcome of one collector's attempt at one section.

    ``StrEnum`` so the value serialises straight into a decision trace or a
    ``ToolResult.metadata`` dict without a cast (repo targets Python 3.12).
    """

    COLLECTED = "collected"
    """Queried successfully and got data."""

    EMPTY = "empty"
    """Queried successfully; the source genuinely has nothing for this incident.
    A legitimate answer, not a failure — an idle service with no error logs is
    ``EMPTY``, and a consumer may treat that as positive evidence *against* a
    cause that would have produced them."""

    UNAVAILABLE = "unavailable"
    """Could not query: capability not registered, credentials absent, provider
    disabled. A clean skip — a provider that was never configured has not
    malfunctioned."""

    FAILED = "failed"
    """The query was attempted and errored (timeout, HTTP error, bad payload).
    The only status that should trip a circuit breaker."""

    NOT_REQUESTED = "not_requested"
    """The caller's ``ContextRequest`` did not name this section, so nothing was
    attempted. Distinct from ``EMPTY`` and ``UNAVAILABLE``: no cost was paid and
    no claim is being made about the world."""

    @property
    def attempted(self) -> bool:
        """Whether a collector actually tried to reach a provider."""
        return self not in (SectionStatus.NOT_REQUESTED, SectionStatus.UNAVAILABLE)

    @property
    def usable(self) -> bool:
        """Whether the section carries a trustworthy answer, empty or not."""
        return self in (SectionStatus.COLLECTED, SectionStatus.EMPTY)


Source = Literal[
    "metrics",
    "logs",
    "traces",
    "k8s_events",
    "topology",
    "dependencies",
    "deployments",
    "incident_history",
    "runbooks",
    "oncall",
    "cmdb",
]
"""Where an observation came from.

Deliberately names the *kind of source*, not the vendor: ``metrics`` rather than
``prometheus``, ``traces`` rather than ``jaeger``. The vendor is already recorded
in ``SourceProvenance.provider``, and an agent reasoning about "a slow span"
should not have to care which backend served it — that is the whole point of the
tool registry sitting underneath.
"""


def digest(*parts: Any) -> str:
    """Short, stable hash of the identity parts a caller passes.

    SHA-256 truncated to 16 hex chars: collision-resistant enough to identify
    findings within one incident while staying readable in a log line or a prompt.
    Not security-relevant — this is an identifier, not a signature.

    Deterministic by design. The eval harness needs a re-run over the same
    incident to produce the same ids, so a verdict can be *compared* with its
    predecessor rather than merely replacing it. Random UUIDs would make every
    run differ.
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def make_observation_id(correlation_id: str, source: str, category: str, signature: str) -> str:
    """Identity hash for one observation.

    Covers the identity tuple — which incident, which source, which category,
    which normalised signature — and nothing else. Timestamps, severity and
    confidence are **not** in the digest, so two runs over the same incident whose
    underlying samples differ in those fields still produce the same id. That is
    intended: the id answers "is this the same finding?", not "is this the same
    data?". Do not use it as a change detector.
    """
    return digest("obs", correlation_id, source, category, signature)


class SectionSpec(BaseModel):
    """What a caller wants from one section.

    ``params`` is the *caller's* query, passed through to the underlying
    capability untouched. This is the design decision that lets four agents share
    transport without sharing query semantics: RCA's ``orders_failed_total`` PromQL
    and Log Correlation's ``http_server_duration_milliseconds_count`` PromQL are
    measuring different things, and collapsing them would change both agents'
    numbers. The platform owns the round-trip, the retry, the cache and the
    error-to-status mapping; the agent keeps its own query.

    ``query_id`` names the query so a section can hold several results and a
    consumer can find its own. Two callers issuing the identical ``params`` share
    one round-trip regardless of what they called it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Source
    query_id: str
    params: dict[str, Any] = Field(default_factory=dict)

    capability: str | None = None
    """Override the collector's default capability, within the same source family.

    Needed because a source is not always one capability. ``metrics`` covers both
    ``observability.metrics.query`` (a PromQL query) and
    ``observability.metrics.alerts`` (what is currently firing) — the RCA agent uses
    both, and they belong in the same section because they are the same *kind* of
    evidence from the same provider.

    The alternative was a twelfth ``Source`` value for alerts, which would have split
    one provider's evidence across two sections purely because of an endpoint
    boundary, and left every consumer to remember to ask for both. Deliberately not a
    free-for-all: the collector still refuses anything on the denylist, so this cannot
    be used to reach a mutation.
    """

    def fingerprint(self) -> str:
        """Stable hash of the *query*, ignoring ``query_id``.

        Two callers naming the same underlying query differently must still hit
        one cache entry — otherwise the deduplication this whole layer exists for
        silently does not happen.

        ``capability`` is part of the hash: two specs with identical (empty) params
        against ``metrics.query`` and ``metrics.alerts`` are different questions, and
        collapsing them would serve one's answer for the other.
        """
        canonical = "&".join(f"{k}={self.params[k]!r}" for k in sorted(self.params))
        return digest("spec", self.source, self.capability or "", canonical)


class Observation(BaseModel):
    """One normalised finding, independent of which backend produced it.

    Immutable by construction (``frozen=True``, matching ``Evidence``,
    ``ChangeRecord`` and ``TopologyResult``): this is a record of something
    observed at a point in time and handed to several agents at once. If a
    consumer could mutate it, two agents reasoning about "the same" observation
    could be looking at different content — and an audit trail that can be edited
    after the fact is not an audit trail.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    """Deterministic identity hash — see ``make_observation_id``."""

    correlation_id: str
    """Ties every observation from one incident together. Derived from the
    incident's own coordinates, not the wall clock, so re-building the same
    incident's context yields the same id."""

    source: Source
    timestamp: datetime
    service: str
    """Where the observation was *made*. A fact about origin, not about blame —
    a checkout log line reading "payment charge failed" has ``service="checkout"``
    and points at payment via ``metadata``."""

    severity: str
    """Provider-reported severity, lower-cased but otherwise not remapped. Kept as
    a free string rather than an enum because the sources disagree about their
    vocabularies (Loki levels, Prometheus alert severities, K8s event types) and
    forcing a single ladder here would destroy information the adapters need."""

    category: str
    """What kind of finding this is within its source — ``dependency_health``,
    ``error_rate``, ``restart``, ``commit``. This is the field an adapter groups
    on when projecting a context back into an agent's own shape."""

    signature: str
    """Normalised, variable-stripped form of the observation, used for identity
    and cross-source agreement. Two log lines differing only by a UUID or a
    latency number share a signature."""

    evidence: str
    """Human-readable observation text, already redacted and length-bounded.
    What actually reaches a prompt or a war-room body."""

    confidence: float = Field(ge=0.0, le=1.0)
    """How much weight this single observation carries on its own, before ranking.
    Set by the collector from source-intrinsic facts (a firing alert outranks one
    info log line); the ranker later combines it with recency, topology distance
    and cross-source agreement."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Small, provider-specific extras an adapter may need. Deliberately a plain
    dict — the same shallow-immutability compromise ``SupportingTelemetry`` and
    ``ChangeRecord`` already make — because deep-freezing a handful of read-only
    scalars would be machinery nothing else in this codebase uses."""
