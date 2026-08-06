"""Topology resolution chain.

Walks a priority-ordered list of providers and returns the first non-empty
dependency list. Owns the cross-cutting concerns so individual providers stay
thin: caching, per-provider health, per-provider timeouts, a total resolution
budget, a circuit breaker, and a structured attempt log.

Priority order
--------------
Explicit topology (handled by the caller, never reaches here) then, by default::

    cmdb -> mock

``AIOPS_TOPOLOGY_PROVIDERS`` overrides the chain, e.g. ``otel,snow,cmdb,mock``.
The default is deliberately the pre-existing behaviour: RA-007 previously called
``itsm.cmdb.dependencies`` and nothing else, so a default chain of ``cmdb,mock``
adds a guaranteed terminal fallback without introducing any new network call.
New tiers (``otel``, ``snow``) are opt-in precisely so enabling them is a
conscious act with its own eval run, rather than a silent change to how every
incident's suspects are derived.

Why a total budget as well as per-provider timeouts
--------------------------------------------------
``correlate()`` resolves topology *before* its logs/traces/metrics fan-out, so
this is serial latency on every correlation. Per-provider timeouts alone bound
each hop but not their sum — a four-tier chain of slow-but-not-timing-out
providers would still stall the incident. The budget caps the whole walk and
degrades to the terminal tier when exhausted. Same fail-fast reasoning as the
Loki provider's short connect cap.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, replace

from aiops.tools.topology.base import (
    HealthStatus,
    ProviderStatus,
    TopologyProvider,
    TopologyResult,
)
from aiops.tools.topology.cache import clear as _cache_clear
from aiops.tools.topology.cache import get as _cache_get
from aiops.tools.topology.cache import health_ttl, ttl_for_status
from aiops.tools.topology.cache import put as _cache_put
from aiops.tools.topology.providers.cmdb import CmdbTopologyProvider
from aiops.tools.topology.providers.k8s import K8sTopologyProvider
from aiops.tools.topology.providers.mock import MockTopologyProvider
from aiops.tools.topology.providers.otel import OtelTopologyProvider
from aiops.tools.topology.providers.snow import SnowTopologyProvider

logger = logging.getLogger(__name__)

_DEFAULT_CHAIN = "cmdb,mock"
_PER_PROVIDER_TIMEOUT = float(os.environ.get("AIOPS_TOPOLOGY_PROVIDER_TIMEOUT", "2"))
_TOTAL_BUDGET = float(os.environ.get("AIOPS_TOPOLOGY_TOTAL_BUDGET", "3"))
_CIRCUIT_OPEN_SECONDS = float(os.environ.get("AIOPS_TOPOLOGY_CIRCUIT_OPEN_SECONDS", "30"))

# provider name -> monotonic deadline until which we skip it. Mirrors the
# module-level breaker in loki.py/jaeger.py; keyed per provider so one bad tier
# does not disable the others.
_circuit_open_until: dict[str, float] = {}

# Registry of available providers. A dict rather than a hardcoded tuple so a new
# tier is a registration, not an edit to the walk logic (extension point E3).
#
# Being *available* is not the same as being *in the chain*: only the names in
# ``_DEFAULT_CHAIN`` (or AIOPS_TOPOLOGY_PROVIDERS) are walked. ``snow`` is
# available but opt-in, so registering it here cannot add a ServiceNow round-trip
# to the default path.
_PROVIDERS: dict[str, TopologyProvider] = {
    "otel": OtelTopologyProvider(),
    "snow": SnowTopologyProvider(),
    "k8s": K8sTopologyProvider(),
    "cmdb": CmdbTopologyProvider(),
    "mock": MockTopologyProvider(),
}

# Tiers that resolve in-process and cannot block on the network. These stay
# eligible even after the total budget is spent: consulting them costs
# microseconds, and skipping them would mean a slow network tier leaves the
# caller with no topology at all — strictly worse than the single-CMDB-lookup
# behaviour this chain replaced.
_FREE_PROVIDERS: frozenset[str] = frozenset({"cmdb", "mock"})


def register_provider(provider: TopologyProvider) -> None:
    """Add or replace a provider by name.

    Idempotent replacement (rather than the tool registry's raise-on-duplicate)
    because tests and opt-in tiers legitimately re-register; a duplicate here is
    not the configuration error it would be for a ticketing backend.
    """
    _PROVIDERS[provider.name] = provider


@dataclass
class TopologyResolution:
    """Outcome of a full chain walk.

    ``dependencies`` is the answer; everything else exists so the caller can
    explain *how* it got there without re-running the chain.
    """

    dependencies: list[str] = field(default_factory=list)
    winning_provider: str | None = None
    attempts: list[TopologyResult] = field(default_factory=list)
    budget_exhausted: bool = False

    @property
    def resolved(self) -> bool:
        return bool(self.dependencies)


def _chain_ordered() -> list[tuple[str, bool]]:
    """Configured provider names in priority order, each flagged ``(name, known)``.

    The single parse of ``AIOPS_TOPOLOGY_PROVIDERS``; ``_chain`` is derived from it.

    Order is preserved **including unknown names**, and that is the whole point.
    ``resolve`` records one attempt per configured name and RA-007 attributes its
    decision_trace to the highest-priority attempt, so hoisting unknown names into a
    pre-pass (which an earlier version of this did) promoted a typo to ``attempts[0]``
    and made the trace describe the typo while the primary tier that actually
    answered was masked — the very bug class recording unknown names was meant to
    prevent.
    """
    raw = os.environ.get("AIOPS_TOPOLOGY_PROVIDERS", "").strip() or _DEFAULT_CHAIN
    ordered: list[tuple[str, bool]] = []
    for n in (part.strip() for part in raw.split(",")):
        if not n:
            continue
        known = n in _PROVIDERS
        if not known:
            logger.warning("topology: unknown provider %r in AIOPS_TOPOLOGY_PROVIDERS; skipping", n)
        ordered.append((n, known))
    return ordered


def _chain() -> tuple[list[str], list[str]]:
    """Provider names as ``(known, unknown)``, priority order kept within each list.

    Unknown names are returned, not just logged: a log warning does not reach the
    caller, and the caller is what writes the operator-facing audit line.
    """
    ordered = _chain_ordered()
    return [n for n, known in ordered if known], [n for n, known in ordered if not known]


def _breaker_open(name: str) -> bool:
    until = _circuit_open_until.get(name, 0.0)
    return time.monotonic() < until


def _trip_breaker(name: str) -> None:
    _circuit_open_until[name] = time.monotonic() + _CIRCUIT_OPEN_SECONDS


def _cache_key(name: str, service: str) -> str:
    return f"deps:{name}:{service.lower().strip()}"


def _has_cached_answer(name: str, service: str) -> bool:
    """Whether this tier can answer from cache without a call.

    Lets the budget-exhaustion path consult a *remote* tier it would otherwise skip:
    serving a cached entry costs nothing and cannot blow the budget, so excluding
    non-free tiers there discarded good data for no benefit.
    """
    return isinstance(_cache_get(_cache_key(name, service)), TopologyResult)


def _run_provider(name: str, service: str, *, timeout_s: float | None = None) -> TopologyResult:
    """Invoke one provider with caching, health gating, and failure containment.

    Factored out of the chain walk so the budget-exhaustion path can still
    consult the free tiers without duplicating this logic (and without
    duplicating the cache writes, which is how the two paths would drift).
    """
    provider = _PROVIDERS[name]
    cache_key = _cache_key(name, service)

    cached = _cache_get(cache_key)
    if isinstance(cached, TopologyResult):
        # Re-stamp as cached so the attempt log distinguishes a cache hit from a
        # fresh query; latency of 0 would otherwise look like a suspiciously
        # fast backend.
        #
        # ``replace`` rather than re-listing the fields: an explicit constructor call
        # silently resets any field added later to its default on every cache hit,
        # which is a bug that would only surface as a subtly wrong attempt log.
        # ``dependencies`` is copied because the cached list is shared.
        return replace(cached, dependencies=list(cached.dependencies), cached=True)

    # Breaker is checked *after* the cache, deliberately. A tripped breaker means
    # "stop calling this provider", not "discard what it already told us". Checking
    # it first (which the chain walk used to do) meant one failure for service B
    # short-circuited every subsequent lookup on that tier — including service A,
    # whose fresh cached answer was sitting right there — forcing a fallthrough to
    # a lower-confidence tier for the full _CIRCUIT_OPEN_SECONDS window.
    if _breaker_open(name):
        return TopologyResult(
            provider=name,
            status=ProviderStatus.UNAVAILABLE,
            # Self-identifying: RA-007 renders this note straight into an
            # operator-facing decision_trace line, and a bare "circuit open" there
            # named neither the tier nor the original error.
            note=f"{name} circuit open",
        )

    health = _cached_health(provider)
    if not health.healthy:
        return TopologyResult(
            provider=name,
            status=ProviderStatus.UNAVAILABLE,
            note=health.detail or "unhealthy",
        )

    try:
        result = provider.resolve(service, timeout_s=timeout_s or _PER_PROVIDER_TIMEOUT)
    except Exception as exc:
        # The Protocol says resolve() must not raise. Defend anyway: a
        # third-party provider that breaks the contract should degrade this
        # tier, not the whole correlation.
        logger.warning("topology: provider %r raised for %r: %s", name, service, exc)
        result = TopologyResult(
            provider=name,
            status=ProviderStatus.FAILED,
            error=f"{type(exc).__name__}",
        )

    _cache_put(cache_key, result, ttl_for_status(result.status.value))

    if result.status is ProviderStatus.FAILED:
        _trip_breaker(name)
        logger.warning(
            "topology: provider %r failed for %r (%s); breaker open %.0fs",
            name,
            service,
            result.error,
            _CIRCUIT_OPEN_SECONDS,
        )
    return result


def _cached_health(provider: TopologyProvider) -> HealthStatus:
    """Health check with its own TTL, so a chain walked repeatedly during one
    incident does not re-probe every tier each time."""
    key = f"health:{provider.name}"
    hit = _cache_get(key)
    if isinstance(hit, HealthStatus):
        return hit
    try:
        status = provider.health()
    except Exception as exc:  # a health check must never break resolution
        status = HealthStatus(healthy=False, detail=f"health check raised: {type(exc).__name__}")
    _cache_put(key, status, health_ttl())
    return status


def resolve(service: str) -> TopologyResolution:
    """Walk the provider chain for ``service`` and return the first real answer.

    Never raises: every provider failure mode is a ``TopologyResult`` status, and
    an exhausted budget or empty chain simply yields an empty resolution. A
    caller that cannot get topology is expected to continue without it (RA-007
    treats no-topology as "the fault is service-internal"), so failing loudly
    here would convert a degraded signal into a broken incident response.
    """
    resolution = TopologyResolution()
    started = time.monotonic()
    ordered = _chain_ordered()
    chain = [name for name, known in ordered if known]

    for name, known in ordered:
        # An unrecognised name is recorded in its *configured* slot, interleaved with
        # the real tiers rather than hoisted into a pre-pass. RA-007 reports
        # attempts[0], so a typo in slot 2 must not displace the primary tier's
        # outcome from slot 0.
        if not known:
            resolution.attempts.append(
                TopologyResult(
                    provider=name,
                    status=ProviderStatus.UNAVAILABLE,
                    note=f"unknown provider {name!r}; check AIOPS_TOPOLOGY_PROVIDERS",
                )
            )
            continue

        if (time.monotonic() - started) >= _TOTAL_BUDGET:
            resolution.budget_exhausted = True
            logger.warning(
                "topology: budget %.1fs exhausted for %r after %d attempt(s)",
                _TOTAL_BUDGET,
                service,
                len(resolution.attempts),
            )
            # Don't let a slow upstream tier starve the whole chain. The budget
            # is checked *between* tiers, so a single provider that blocks (a
            # hibernating ServiceNow PDI, a Prometheus that refuses slowly) can
            # burn it entirely and leave us with no topology at all — strictly
            # worse than the pre-chain behaviour, which always reached the CMDB.
            # Free tiers below the cut-off are still worth consulting: they are
            # in-process lookups that cost microseconds and cannot block.
            for fallback_name in chain[chain.index(name) :]:
                # Consult a tier below the cut-off when it is either free (in-process,
                # microseconds, cannot block) OR already answerable from cache. The
                # second half closes the same bug class the breaker/cache reordering
                # fixed one level up: gating solely on _FREE_PROVIDERS discarded a
                # fresh cached answer from a remote tier and fell through to a
                # lower-confidence one, even though serving that cache costs nothing
                # and cannot blow the budget.
                #
                # No breaker pre-check here either: _run_provider consults the cache
                # before the breaker, so a tripped tier can still serve a fresh entry.
                if fallback_name in _FREE_PROVIDERS or _has_cached_answer(fallback_name, service):
                    result = _run_provider(fallback_name, service)
                    resolution.attempts.append(result)
                    if result.resolved:
                        resolution.dependencies = list(result.dependencies)
                        resolution.winning_provider = result.provider
                        return resolution
            break

        # The breaker is not checked here — _run_provider checks it after the
        # cache, so a fresh cached answer survives a tripped breaker.
        #
        # Give the provider whatever is left of the budget, capped by its own
        # per-provider timeout, so the last tier in a slow chain is not handed a
        # deadline it cannot meet.
        remaining = max(0.0, _TOTAL_BUDGET - (time.monotonic() - started))
        timeout_s = min(_PER_PROVIDER_TIMEOUT, remaining) or _PER_PROVIDER_TIMEOUT

        result = _run_provider(name, service, timeout_s=timeout_s)
        resolution.attempts.append(result)

        if result.status is ProviderStatus.FAILED:
            continue

        if result.resolved:
            resolution.dependencies = list(result.dependencies)
            resolution.winning_provider = result.provider
            logger.debug(
                "topology: %r resolved %d dep(s) for %r in %.1fms",
                name,
                len(result.dependencies),
                service,
                result.latency_ms,
            )
            return resolution

    return resolution


def reset_for_tests() -> None:
    """Clear breaker + cache state.

    Test seam mirroring ``loki._reset_circuit_for_tests``. Both are
    process-global, so ``tests/conftest.py`` must call this in an autouse fixture
    or a test that trips a breaker will silently disable that provider for the
    next 30s of unrelated tests.
    """
    _circuit_open_until.clear()
    _cache_clear()
