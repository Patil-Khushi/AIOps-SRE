"""Historical incident retrieval chain.

Walks a priority-ordered list of providers and returns the first that finds
matches, exactly as the topology resolver does — the reasoning is the same: one
active provider per capability cannot express "try the vector store, fall back to
the static corpus", and each backend has different coverage and failure modes.

Default chain is ``mock`` alone. The three real backends are opt-in via
``AIOPS_INCIDENT_HISTORY_PROVIDERS`` because none is configured in this
deployment, and a default that reaches for an absent database would add latency to
every correlation to learn nothing.

Retrieval is evidence, never a decision
---------------------------------------
Nothing here ranks causes or recommends actions. It returns past incidents and why
they matched. A caller that wants a conclusion has to draw it, which keeps the
inference visible and attributable instead of smuggled into a retrieval score.
"""

from __future__ import annotations

import logging
import os
import time

from aiops.tools.incident_history.base import (
    IncidentHistoryProvider,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStatus,
)
from aiops.tools.incident_history.providers.backends import (
    ElasticIncidentHistoryProvider,
    PostgresIncidentHistoryProvider,
    VectorIncidentHistoryProvider,
)
from aiops.tools.incident_history.providers.mock import MockIncidentHistoryProvider
from aiops.tools.resilience import ResiliencePolicy, breaker_open, guard
from aiops.tools.resilience import reset_for_tests as _reset_resilience

logger = logging.getLogger(__name__)

_DEFAULT_CHAIN = "mock"
_TOTAL_BUDGET = float(os.environ.get("AIOPS_INCIDENT_HISTORY_BUDGET", "3"))
_CIRCUIT_OPEN_SECONDS = float(os.environ.get("AIOPS_INCIDENT_HISTORY_BREAKER", "30"))

# This seam previously had a breaker but no timeout, no cache and no retries — so a
# slow vector store would stall every correlation with nothing memoised, and one
# transient blip would disable the tier for 30s. All four protections now come from
# the shared middleware rather than being re-implemented (and half-forgotten) here.
_POLICY = ResiliencePolicy(
    timeout=float(os.environ.get("AIOPS_INCIDENT_HISTORY_TIMEOUT", "3")),
    retries=int(os.environ.get("AIOPS_INCIDENT_HISTORY_RETRIES", "2")),
    breaker_seconds=_CIRCUIT_OPEN_SECONDS,
    cache_ttl=float(os.environ.get("AIOPS_INCIDENT_HISTORY_CACHE_TTL", "120")),
)

_PROVIDERS: dict[str, IncidentHistoryProvider] = {
    "vector": VectorIncidentHistoryProvider(),
    "elastic": ElasticIncidentHistoryProvider(),
    "postgres": PostgresIncidentHistoryProvider(),
    "mock": MockIncidentHistoryProvider(),
}

# In-process tiers that cannot block on the network stay eligible even after the
# budget is spent — the same lesson the topology chain learned when a slow tier
# starved the static fallback and left the caller with nothing.
_FREE_PROVIDERS = frozenset({"mock"})

_circuit_open_until: dict[str, float] = {}


def register_provider(provider: IncidentHistoryProvider) -> None:
    """Add or replace a provider by name (idempotent)."""
    _PROVIDERS[provider.name] = provider


def _chain() -> tuple[list[str], list[str]]:
    """Split the configured chain into ``(known, unknown)`` provider names.

    Unknown names are returned rather than only logged, matching
    ``change_context._chain``.

    This seam does not mis-report the way ``change_context`` did, because
    ``retrieve_similar`` keys its "no backend could be searched" fallback off the
    *absence of any EMPTY-status attempt* — not off ``attempts`` being empty, and not
    off a completeness flag. Adding UNAVAILABLE attempts therefore cannot break it:
    an unknown name is never EMPTY. (Worth stating precisely, because the obvious
    guess — that the fallback tests ``attempts`` for emptiness — would make this
    change look like a regression and send someone to "fix" ``history.py``.)

    That safety is still incidental to how the caller happens to be written rather
    than a property of the chain, so surfacing unknown names makes it structural and
    puts the typo in ``providers_attempted`` where a caller can see it.
    """
    raw = os.environ.get("AIOPS_INCIDENT_HISTORY_PROVIDERS", "").strip() or _DEFAULT_CHAIN
    names = [n.strip() for n in raw.split(",") if n.strip()]
    known: list[str] = []
    unknown: list[str] = []
    for n in names:
        if n in _PROVIDERS:
            known.append(n)
        else:
            logger.warning("incident_history: unknown provider %r; skipping", n)
            unknown.append(n)
    return known, unknown


def _breaker_open(name: str) -> bool:
    """Delegates to the shared middleware so one breaker governs each provider.

    Keeping a second, local breaker alongside the middleware's would mean two
    sources of truth about whether a backend is healthy — and they would disagree
    the first time one tripped.
    """
    return breaker_open(f"incident_history.{name}")


def search_similar(query: RetrievalQuery) -> list[RetrievalResult]:
    """Walk the chain, returning every attempt in order.

    Returns *all* attempts rather than only the winner, because a caller needs to
    distinguish "the vector store was down and the static corpus answered" from
    "the vector store answered" — the two carry very different evidential weight,
    and collapsing them would hide which backend a match actually came from.

    Never raises: a retrieval outage must not break a correlation.
    """
    attempts: list[RetrievalResult] = []
    started = time.monotonic()
    chain, unknown = _chain()

    # A configured name with no provider behind it is a coverage hole, recorded as
    # one so it reaches the caller instead of only the log.
    for name in unknown:
        attempts.append(
            RetrievalResult(
                provider=name,
                status=RetrievalStatus.UNAVAILABLE,
                note="unknown provider name; check AIOPS_INCIDENT_HISTORY_PROVIDERS",
            )
        )

    for name in chain:
        provider = _PROVIDERS[name]
        budget_spent = (time.monotonic() - started) >= _TOTAL_BUDGET
        if budget_spent and name not in _FREE_PROVIDERS:
            attempts.append(
                RetrievalResult(
                    provider=name,
                    status=RetrievalStatus.UNAVAILABLE,
                    note="retrieval budget exhausted before this tier",
                )
            )
            continue

        if _breaker_open(name):
            attempts.append(
                RetrievalResult(
                    provider=name, status=RetrievalStatus.UNAVAILABLE, note="circuit open"
                )
            )
            continue

        try:
            healthy, detail = provider.health()
        except Exception as exc:
            healthy, detail = False, f"health check raised: {type(exc).__name__}"
        if not healthy:
            attempts.append(
                RetrievalResult(provider=name, status=RetrievalStatus.UNAVAILABLE, note=detail)
            )
            continue

        # Guarded: this seam previously had a breaker but no timeout, no cache and
        # no retries, so a slow vector store would stall every correlation with no
        # memoisation and a single blip would break the tier. All four now come from
        # the shared middleware.
        outcome = guard(
            f"incident_history.{name}",
            lambda p=provider: p.search(query),
            policy=_POLICY,
            cache_key=f"history:{name}:{query.service}:{hash(tuple(query.signatures))}",
            is_transient=lambda r: r.status is RetrievalStatus.FAILED,
            is_cacheable=lambda r: r.status in (RetrievalStatus.MATCHED, RetrievalStatus.EMPTY),
            is_empty=lambda r: r.status is RetrievalStatus.EMPTY,
        )

        if outcome.value is not None:
            result = outcome.value
        else:
            result = RetrievalResult(
                provider=name,
                # Starvation joins breaker-open as an availability fact: the
                # provider was never called, so this is not evidence against it.
                status=RetrievalStatus.UNAVAILABLE
                if (outcome.breaker_open or outcome.starved)
                else RetrievalStatus.FAILED,
                error=outcome.error,
                note="; ".join(outcome.notes) or None,
                latency_ms=outcome.latency_ms,
            )

        attempts.append(result)
        if result.status is RetrievalStatus.FAILED:
            # Breaker tripping is the middleware's job now, and it only fires after
            # retries are exhausted — so a single transient failure no longer
            # disables the tier.
            continue
        if result.matched:
            break

    return attempts


def reset_for_tests() -> None:
    """Clear breaker and cache state.

    Both now live in the shared middleware, so resetting must go through it —
    clearing only the local dict would leave a tripped breaker armed and silently
    disable a tier for later tests.
    """
    _circuit_open_until.clear()
    _reset_resilience()
