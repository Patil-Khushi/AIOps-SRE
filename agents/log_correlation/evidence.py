"""Structured evidence objects for the Log Correlation agent (RA-007).

Why this exists
---------------
``CorrelatedSignal`` is a raw observation: a source, a fingerprint, a timestamp,
a severity. That is enough to rank signatures, but not enough for a downstream
agent to *reason* about. It carries no identity (so two agents cannot refer to
the same finding), no topology context (so "payment errored" does not say
*payment is checkout's direct dependency*), and no per-item confidence (so a
single log line and a cross-source recurrence look equally strong).

``Evidence`` wraps a signal with exactly those missing dimensions. Signals stay
untouched and keep flowing in ``CorrelationResult.timeline``; evidence is an
additional, richer view over the same observations.

Immutability
------------
Every model here is ``frozen=True``. Evidence is a record of what was observed at
a point in time and is handed to other agents (the RCA agent reads the
correlation payload). If a consumer could mutate it, two agents reasoning about
"the same" evidence could be looking at different content, and an audit trail
that can be edited after the fact is not an audit trail. Frozen models make that
a type error rather than a debugging session.

Deterministic identifiers
-------------------------
``evidence_id`` and ``correlation_id`` are derived by hashing, not random UUIDs.
Two reasons. First, the eval harness calls ``run()`` with ``force_synthetic=True``
specifically so the golden gate is a *reproducible* regression test — random ids
would make every run differ. Second, a stable id means the same observation
carries the same identity across re-runs, so a verdict can be compared with its
predecessor instead of merely replaced.

**They are identity hashes, not content hashes.** ``evidence_id`` covers the
identity triple — ``correlation_id``, ``source``, ``signature`` — and nothing
else. Severity, occurrence count and timestamps are *not* in the digest, so two
runs over the same incident whose underlying signals differ in those fields
produce the same ``evidence_id``. That is the intended behaviour: the id answers
"is this the same finding?", which is what lets a re-run be compared against its
predecessor at all. It does **not** answer "is this the same data?", so do not use
it as a change detector or an integrity check — compare the fields for that.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SignalType = Literal[
    "error_log",
    "warning_log",
    "log_line",
    "error_span",
    "slow_span",
    "trace_summary",
    "metric_anomaly",
    "metric_sample",
]
"""What kind of observation this is, independent of which backend it came from.

``source`` says *where* it came from (logs / traces / metrics); ``signal_type``
says *what it is*. A slow span and an error span both arrive from ``traces`` but
mean different things, and a consumer filtering for "actual failures" needs the
distinction.
"""

TopologyRelation = Literal["self", "dependency", "dependent", "unrelated", "unknown"]


def _digest(*parts: Any) -> str:
    """Short, stable hash of whatever identity parts the caller passes.

    SHA-256 truncated to 16 hex chars: collision-resistant enough to identify
    findings within one incident while staying readable in a log line or a
    prompt. Not security-relevant — this is an identifier, not a signature, and it
    covers only the parts given to it rather than a whole object's content.
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def make_correlation_id(service: str, window_start: Any, window_end: Any) -> str:
    """Identifier shared by every piece of evidence from one correlation.

    Derived from the incident's own coordinates (service + window) rather than
    the wall clock, so re-correlating the same incident produces the same id and
    two verdicts for it can be compared rather than just accumulated.
    """
    return _digest("corr", str(service).strip().lower(), window_start, window_end)


class SupportingTelemetry(BaseModel):
    """The raw material behind one piece of evidence.

    Kept as a nested object rather than loose fields so the *claim* (signature,
    severity, confidence) stays visually separate from the *proof* (raw sample,
    counts). A reviewer should be able to see the assertion and the backing data
    without untangling which is which.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample: str = ""
    """The observed line / span summary / metric reading, already sanitized."""

    occurrences: int = 1
    """How many observations collapsed into this evidence under one signature."""

    sources_agreeing: list[str] = Field(default_factory=list)
    """Which signal sources carry this same signature. Cross-source agreement is
    the strongest correlation rule the agent has, so it is recorded per-evidence
    rather than only in the aggregate verdict."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


class TopologyContext(BaseModel):
    """Where the evidence's service sits relative to the incident's service.

    This is what turns "payment is erroring" into "payment is a direct dependency
    of the failing service, reached via checkout -> payment". Populated from the
    topology chain and path discovery built in earlier phases; every field is
    optional because topology is best-effort and an unreachable provider must
    degrade the evidence, not invalidate it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    relation: TopologyRelation = "unknown"
    implicated_service: str | None = None
    """The service this evidence points *at*, when it differs from where the
    telemetry was observed.

    RA-007 queries one service's logs/traces/metrics, so ``Evidence.service`` is
    always that service — it is where the observation came from, which is a fact.
    But a checkout log line reading "payment charge error" is evidence *about*
    payment. Recording that separately keeps the factual origin and the inferred
    target from being conflated, which is the difference between "checkout is
    broken" and "checkout is reporting that payment is broken"."""

    depth: int | None = None
    """Hops from the incident service. 0 = the service itself."""

    path: list[str] = Field(default_factory=list)
    """Ordered chain from the incident service to this one, when known."""

    upstream_complete: bool | None = None
    """Whether the topology source could see *all* callers. ``False`` means an
    empty dependent set is "none observed", not "none" — the distinction that
    stops a gRPC-only view being read as the whole picture."""


class Evidence(BaseModel):
    """One structured, immutable finding.

    Immutable by construction (``frozen=True``): this is a record of an
    observation handed to other agents, so it must not be editable after the
    fact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    correlation_id: str
    timestamp: datetime
    source: Literal["logs", "traces", "metrics"]
    service: str
    signal_type: SignalType
    normalized_signature: str
    severity: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_telemetry: SupportingTelemetry
    topology_context: TopologyContext

    @property
    def is_failure(self) -> bool:
        """Whether this evidence describes an actual failure rather than context.

        A consumer building a root-cause narrative wants the failures; the
        informational lines are corroborating detail. Deriving it here keeps the
        error-severity vocabulary in one place.
        """
        return self.signal_type in {"error_log", "error_span", "metric_anomaly"}
