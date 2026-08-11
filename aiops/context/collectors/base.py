"""The collector interface and the one guarded round-trip every collector shares.

Stage 1 of the pipeline is the only impure stage — everything after it is a pure
function over data structures. So all the I/O, all the failure interpretation and
all the caching live here, once.

Why a Protocol rather than a base class
---------------------------------------
Follows ``aiops/tools/topology/base.py::TopologyProvider``. A collector has no
state or lifecycle worth inheriting, and a ``Protocol`` keeps implementations as
plain objects with no coupling back to this package — the same
wrap-the-dependency-behind-a-thin-interface posture the tool registry takes.
``incident_history`` chose a base class instead, but only because it had scoring
helpers worth inheriting; there is no equivalent here.

Why one generic collector instead of one module per source
---------------------------------------------------------
Eight of the eleven sources are the same operation: call a capability with the
caller's parameters, keep the payload, map the failure. Writing that eight times
would be eight chances to interpret ``metadata["missing_provider"]`` differently —
which is precisely the class of bug ``resilience.py``'s docstring was written about.
``CapabilityCollector`` is configured by composition (source, capability, an
emptiness predicate) rather than subclassed. The three sources that genuinely differ
— topology, incident history, k8s events — wrap existing chained seams and live in
``seams.py``.

The status mapping is the important part
----------------------------------------
``registry.call()`` never raises; it reports everything as a ``ToolResult``. Four
distinct outcomes hide in that one object, and collapsing any two of them loses
information a consumer needs:

* ``metadata["missing_provider"]`` → ``UNAVAILABLE``. Nobody could ask. Not an
  error: a capability that was never configured has not malfunctioned.
* ``metadata["blocked_by"] == "hitl_gate"`` → ``UNAVAILABLE``. Should never happen
  for a read (every capability collected here is ``AutonomyLevel.NONE``), but if a
  policy change ever makes one gated, this reports it honestly instead of looking
  like a backend failure.
* ``ok=False`` → ``FAILED``. Asked and errored. The only status that should trip a
  breaker, and the only one worth retrying.
* ``ok=True`` → ``COLLECTED`` or ``EMPTY``, decided by the per-capability
  ``is_empty`` predicate. This is the distinction the RCA prompt depends on: it
  renders "NONE — this signal was checked and was absent" and instructs the model
  to treat that as evidence *against* a cause, which is only true for ``EMPTY``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from aiops.context import cache
from aiops.context.denylist import ensure_allowed
from aiops.context.models import SectionSpec, SectionStatus
from aiops.context.pack import ContextSection, SourceProvenance
from aiops.tools import get_registry
from aiops.tools.registry import ToolResult
from aiops.tools.resilience import ResiliencePolicy, guard

logger = logging.getLogger(__name__)

# --- in-flight request coalescing ----------------------------------------
#
# The cache alone only stops a REPEATED request from re-fetching; it does
# nothing for two requests that are CONCURRENT, because "check the cache, then
# fetch" is a race, not an atomic operation. The builder's own fan-out makes
# this the common case, not an edge case: alert_triage requests three
# trace-search candidates that are frequently the identical string (a service
# name with no space or "-api" suffix to strip), and the shared context unions
# that with notification's own identical-params trace search into ONE build —
# so four specs with the same fingerprint can legitimately be in flight at
# once. Without coalescing, all four can see a cache miss before any of them
# finishes and cache a result, so the count of *actual* live calls for one
# duplicated query becomes a timing accident instead of a guarantee — which
# defeats the deduplication this whole package exists for under exactly the
# load pattern it is supposed to help with most.
#
# One ``threading.Event`` per (correlation_id, fingerprint) in flight. The
# first caller to observe a cache miss becomes the leader and fetches; every
# other caller for the same key waits on that event and then re-reads the
# cache, which the leader has by then populated (for FAILED/UNAVAILABLE
# results, the cache legitimately declines to store them — see
# ``cache.ttl_for_status`` — so a waiter that still misses becomes a new leader
# and retries live, which is correct: an uncached failure should be retried,
# not silently swallowed).
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def reset_for_tests() -> None:
    """Clear in-flight bookkeeping. Process-global state, so this must be wired
    into ``tests/conftest.py`` alongside ``resilience.reset_for_tests()`` — a
    leaked entry from a test that crashed mid-fetch would otherwise leave every
    later request for that exact (correlation_id, fingerprint) waiting on an
    ``Event`` nothing will ever set.
    """
    with _inflight_lock:
        _inflight.clear()


def _policy() -> ResiliencePolicy:
    """Protection settings for a context collection.

    Read per call so a fixture or an operator can retune without a reimport.
    Deliberately tighter than ``ResiliencePolicy``'s defaults on retries: a context
    build fans out over a dozen sections while someone waits, and a slow complete
    answer is worth less than a fast partial one. Caching is handled by
    ``aiops.context.cache`` rather than by ``guard``, because only the former knows
    a section's status and can therefore refuse to cache a failure.
    """
    return ResiliencePolicy(
        timeout=float(os.environ.get("AIOPS_CONTEXT_TIMEOUT", "3")),
        retries=int(os.environ.get("AIOPS_CONTEXT_RETRIES", "1")),
    )


def _leader_worst_case_seconds(policy: ResiliencePolicy) -> float:
    """Upper bound on how long the leader's own ``guard()`` call can run.

    Mirrors ``resilience.guard``'s retry loop exactly, because a bound that does not
    is worse than none — it looks like safety margin while a waiter can still time
    out before the leader finishes. The loop runs ``policy.retries + 1`` attempts,
    each allowed the full ``policy.timeout``; every attempt after the first is
    preceded by a jittered sleep of up to ``policy.backoff * 2 ** (attempt - 1)``
    (full jitter — see ``resilience.guard``'s own comment — so this ceiling, not the
    average, is what a bound must use). At the context layer's own defaults
    (timeout=3, retries=1, backoff=0.2) that is ``2*3 + 0.2 = 6.2s``, not the ``2*3
    = 6.0s`` a bound keyed on ``timeout`` alone would assume.
    """
    attempts = policy.retries + 1
    backoff_ceiling = sum(policy.backoff * (2 ** (attempt - 1)) for attempt in range(1, attempts))
    return attempts * policy.timeout + backoff_ceiling


@runtime_checkable
class Collector(Protocol):
    """What the builder requires of a collector."""

    name: str
    """Stable identifier used in provenance, breaker keys and the collector chain."""

    source: str
    """Which ``Source`` section this collector fills."""

    def collect(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        """Fetch one section. **Must not raise.**

        Every failure mode is expressed as a ``SectionStatus`` on the returned
        section, so the builder never needs a bare ``except`` around collector code
        — the same contract ``TopologyProvider.resolve`` and every provider in this
        repo holds itself to.
        """
        ...


def not_requested(source: str, provider: str = "none") -> ContextSection:
    """The section for a source the caller did not ask about.

    Distinct from ``EMPTY`` on purpose: no cost was paid and no claim is being made
    about the world.
    """
    return ContextSection(
        status=SectionStatus.NOT_REQUESTED,
        provenance=SourceProvenance(provider=provider, status=SectionStatus.NOT_REQUESTED),
    )


def unavailable(source: str, provider: str, reason: str) -> ContextSection:
    """A section that could not be attempted."""
    return ContextSection(
        status=SectionStatus.UNAVAILABLE,
        provenance=SourceProvenance(
            provider=provider,
            status=SectionStatus.UNAVAILABLE,
            coverage_note=reason,
        ),
    )


def _status_for(
    result: ToolResult, is_empty: Callable[[Any], bool]
) -> tuple[SectionStatus, str | None]:
    """Map a ``ToolResult`` onto a section status plus a coverage note.

    See the module docstring for why all four outcomes stay distinct.
    """
    metadata = result.metadata or {}
    if metadata.get("missing_provider"):
        return SectionStatus.UNAVAILABLE, "capability not registered"
    if metadata.get("blocked_by") == "hitl_gate":
        return SectionStatus.UNAVAILABLE, "blocked by the HITL gate"
    if not result.ok:
        return SectionStatus.FAILED, None
    try:
        empty = is_empty(result.data)
    except Exception:  # pragma: no cover - a predicate bug must not lose the payload
        logger.debug("emptiness predicate raised; treating payload as collected", exc_info=True)
        empty = False
    if empty:
        return SectionStatus.EMPTY, "queried successfully; the source reported nothing"
    return SectionStatus.COLLECTED, None


class CapabilityCollector:
    """Collects one section by calling one registry capability.

    Satisfies ``Collector`` structurally without inheriting from it.

    ``is_empty`` inspects the provider payload to separate "asked and got nothing"
    from "asked and got something". It is per-capability because the payloads
    disagree about how they say nothing — Prometheus returns ``{"results": []}``,
    Loki ``{"streams": []}``, Jaeger ``{"trace_count": 0}``. Passing it in rather
    than switching on the capability name keeps this class closed to modification
    and open to a new source.
    """

    def __init__(
        self,
        *,
        name: str,
        source: str,
        capability: str,
        is_empty: Callable[[Any], bool] | None = None,
    ) -> None:
        # Refuse a denied capability when the collector is *constructed*, not when
        # it runs. A denylisted collector that only failed on first use could sit
        # in a chain through a whole test suite and surface in production.
        ensure_allowed(capability)
        self.name = name
        self.source = source
        self.capability = capability
        self._is_empty = is_empty or _payload_is_empty

    def collect(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        """Fetch this section, serving from the intra-incident cache when possible.

        Coalesces concurrent duplicate requests — see the module docstring for
        why a cache alone cannot do that on its own.
        """
        hit, cached_section = self._cached(correlation_id, spec)
        if hit and cached_section is not None:
            return cached_section

        key = cache.section_key(correlation_id, spec)
        leader_event, existing_event = self._claim_or_join(key)

        if leader_event is None:
            # Someone else is already fetching this exact key — wait for them
            # rather than racing a duplicate live call. Bounded by the leader's
            # own worst-case ``guard()`` duration — retries and backoff included,
            # not just one ``timeout`` — doubled for scheduling slack on top of
            # that. A bound keyed on ``timeout`` alone undercounts the leader by
            # a full retry: at this layer's own defaults the leader's worst case
            # is 6.2s (two 3s attempts plus backoff) while ``timeout * 2`` gives
            # only 6.0s, so a waiter could time out first on every single retry
            # — not a scheduling accident, the ordinary path ``guard``'s retry
            # exists to cover. ``existing_event`` is never ``None`` here — see
            # ``_claim_or_join``.
            assert existing_event is not None
            existing_event.wait(timeout=_leader_worst_case_seconds(_policy()) * 2)
            hit, cached_section = self._cached(correlation_id, spec)
            if hit and cached_section is not None:
                return cached_section
            # The leader's result was not cacheable (a FAILED/UNAVAILABLE
            # answer, or the leader timed out waiting on its own fetch) — try
            # once more to become the leader ourselves rather than serving
            # nothing.
            leader_event, _ = self._claim_or_join(key)
            if leader_event is None:
                # Someone else claimed leadership in the gap above — with three
                # or more genuinely concurrent waiters this is not rare, since
                # every waiter past the first can land here. Still correct as a
                # fallback (a second live call beats serving nothing), just not
                # a coalescing failure: the wait bound above is now sized to the
                # leader's real worst case, so reaching this line means the
                # leader actually finished (or gave up) in that window, not that
                # the bound was too tight.
                return self._fetch_and_cache(spec, correlation_id)

        try:
            return self._fetch_and_cache(spec, correlation_id)
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
            leader_event.set()

    @staticmethod
    def _claim_or_join(key: str) -> tuple[threading.Event | None, threading.Event | None]:
        """Atomically either claim leadership of ``key`` or read who already has it.

        Returns ``(leader_event, None)`` when this caller becomes the leader, or
        ``(None, existing_event)`` when someone else already is — a single
        locked check-and-set/check-and-read so a caller never has to make two
        separate dict accesses that could race against the leader's cleanup.
        """
        with _inflight_lock:
            existing = _inflight.get(key)
            if existing is not None:
                return None, existing
            event = threading.Event()
            _inflight[key] = event
            return event, None

    def _fetch_and_cache(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        section = self._fetch(spec)
        cache.put(correlation_id, spec, section, section.status)
        return section

    def _cached(self, correlation_id: str, spec: SectionSpec) -> tuple[bool, ContextSection | None]:
        hit, cached_section = cache.get(correlation_id, spec)
        if not hit or not isinstance(cached_section, ContextSection):
            return False, None
        # Re-stamp provenance so a consumer can tell a cache hit from a fresh
        # call. This flag is the measurable proof that the deduplication this
        # whole layer exists for is actually happening, so it must not be
        # inherited from the original miss.
        return True, cached_section.model_copy(
            update={
                "provenance": cached_section.provenance.model_copy(
                    update={"cached": True, "latency_ms": 0.0}
                )
            }
        )

    def _fetch(self, spec: SectionSpec) -> ContextSection:
        registry = get_registry()
        params = dict(spec.params)

        # A spec may select a sibling capability within its source family — RCA needs
        # both observability.metrics.query and .alerts in the metrics section. Checked
        # against the denylist here as well as in __init__, because the override
        # arrives at call time and would otherwise bypass the constructor's guard.
        capability = spec.capability or self.capability
        if capability != self.capability:
            ensure_allowed(capability)

        outcome = guard(
            f"context.{capability}",
            lambda: registry.call(capability, **params),
            policy=_policy(),
            # Retry a genuine backend error, never a missing provider: the answer
            # to "is this configured?" will not change on a second attempt, so
            # retrying it is pure added latency on the incident path.
            is_transient=lambda r: not r.ok and not (r.metadata or {}).get("missing_provider"),
        )

        if outcome.value is None:
            # guard exhausted its attempts without a result: timeout, an open
            # breaker, or pool starvation. Reported as FAILED with the reason
            # rather than silently empty — "we could not look" must never render as
            # "there was nothing to see".
            note = "breaker open" if outcome.breaker_open else None
            if outcome.starved:
                # Distinct from a timeout: the provider was never called because no
                # worker slot came free, so this is evidence about the shared pool,
                # not about the backend. Naming the lever here saves the next person
                # tuning the wrong knob.
                note = "worker pool saturated (raise AIOPS_RESILIENCE_WORKERS)"
            elif outcome.timed_out:
                note = f"timed out after {_policy().timeout}s"
            return ContextSection(
                status=SectionStatus.FAILED,
                provenance=SourceProvenance(
                    provider=self.name,
                    status=SectionStatus.FAILED,
                    latency_ms=outcome.latency_ms,
                    error=outcome.error,
                    coverage_note=note,
                ),
            )

        result = outcome.value
        status, note = _status_for(result, self._is_empty)
        provider = str((result.metadata or {}).get("provider") or self.name)
        return ContextSection(
            status=status,
            provenance=SourceProvenance(
                provider=provider,
                status=status,
                latency_ms=outcome.latency_ms,
                cached=outcome.from_cache,
                error=result.error,
                coverage_note=note,
            ),
            # Keyed by query id so one section can hold several distinct queries and
            # each consumer can find its own. The payload itself is stored byte-for-
            # byte: an adapter reproducing a legacy prompt string needs the original
            # rows, not a normalised view of them.
            raw={spec.query_id: result.data} if status.usable else None,
        )


def _payload_is_empty(data: Any) -> bool:
    """Default emptiness test: no payload at all.

    Conservative on purpose. A collector whose capability has a meaningful empty
    shape passes its own predicate; without one, anything non-``None`` counts as
    collected, because wrongly reporting ``EMPTY`` is the more damaging error — it
    tells a consumer "this signal was checked and was absent", which the RCA prompt
    treats as positive evidence against a cause.
    """
    if data is None:
        return True
    if isinstance(data, dict | list | str | tuple | set):
        return len(data) == 0
    return False
