"""Shared resilience middleware: timeout, retry, circuit breaker, cache.

Why this exists
---------------
Three provider seams were built in sequence — ``topology``, ``incident_history``,
``change_context`` — and each re-implemented its own protections. The later ones
inherited the *structure* but not all the safeguards:

    topology          timeout ✅  breaker ✅  cache ✅  retry ❌
    incident_history  timeout ❌  breaker ✅  cache ❌  retry ❌
    change_context    timeout ✅  breaker ❌  cache ❌  retry ❌

That table is the bug. Not any single missing feature, but the fact that adding a
provider means remembering four separate things, and forgetting one is invisible
until production. So the fix is to make correctness the default: a call wrapped in
``guard`` gets all four, and a new provider cannot silently skip one.

Retry before breaking — the ordering matters
--------------------------------------------
The original arrangement had breakers with no retries, which over-reacts badly: a
single dropped packet trips a 30-second breaker and degrades a whole tier. Here
retries are attempted *first*, and the breaker trips only once they are exhausted.
That way the breaker means "this backend is genuinely down", which is the only
reading that justifies skipping it.

Retries are attempted only for **transient** failures. Retrying "not configured"
or "queried fine, found nothing" is pure latency — the answer will not change.

Timeouts are enforced, not advertised
-------------------------------------
Passing a ``timeout`` value to a provider only works if the provider honours it.
Several do not, which is how a topology tier once burned a 3-second budget on one
call. ``guard`` runs the callable in a worker thread and abandons it at the
deadline, so the bound holds regardless of whether the callee cooperates.

The abandoned thread is *not* killed — Python cannot safely do that — so a hung
call still occupies a thread until it returns. That is a real limitation, recorded
here rather than glossed: the caller is protected, the thread is not reclaimed.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = float(os.environ.get("AIOPS_RESILIENCE_TIMEOUT", "3"))
_DEFAULT_RETRIES = int(os.environ.get("AIOPS_RESILIENCE_RETRIES", "2"))
_DEFAULT_BACKOFF = float(os.environ.get("AIOPS_RESILIENCE_BACKOFF", "0.2"))
_DEFAULT_BREAKER = float(os.environ.get("AIOPS_RESILIENCE_BREAKER", "30"))
_DEFAULT_CACHE_TTL = float(os.environ.get("AIOPS_RESILIENCE_CACHE_TTL", "60"))

# One executor shared by every guarded call. Threads are pooled rather than created
# per call because a hung provider leaks its thread until it returns (see module
# docstring) — an unbounded pool would let repeated hangs exhaust the process.
_MAX_WORKERS = int(os.environ.get("AIOPS_RESILIENCE_WORKERS", "8"))
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="aiops-guard")

# Shared state, guarded by one lock. The seams' original per-module dicts were
# mutated without synchronisation; measured clean under 12 threads, but that is
# absence of evidence rather than proof, and a lock here costs nothing at these
# call rates.
_lock = threading.RLock()
_breakers: dict[str, float] = {}
_cache: dict[str, tuple[float, Any]] = {}
_stats: dict[str, dict[str, int]] = {}


@dataclass(frozen=True)
class ResiliencePolicy:
    """Per-call protection settings.

    Defaults are deliberately conservative for the incident path: a short timeout
    and few retries, because correlation runs while someone is waiting and a slow
    answer is worth less than a fast degraded one.
    """

    timeout: float = _DEFAULT_TIMEOUT
    retries: int = _DEFAULT_RETRIES
    """Additional attempts after the first. ``0`` disables retrying."""

    backoff: float = _DEFAULT_BACKOFF
    """Base delay, doubled per attempt, with jitter applied.

    Jitter matters even at this scale: without it, several providers failing
    together retry in lockstep and hit a recovering backend simultaneously."""

    breaker_seconds: float = _DEFAULT_BREAKER
    cache_ttl: float = _DEFAULT_CACHE_TTL
    cache_empty_ttl: float = 30.0
    """Shorter TTL for a successful-but-empty answer.

    An empty result is more likely to become non-empty soon (a half-populated
    corpus, a service that has not yet emitted) than a positive answer is to
    change, so it is re-checked sooner without being treated as a failure."""


@dataclass
class GuardOutcome[T]:
    """What ``guard`` did, alongside the value."""

    value: T | None = None
    ok: bool = False
    from_cache: bool = False
    breaker_open: bool = False
    timed_out: bool = False
    starved: bool = False
    """The call never reached the provider — no worker slot came free in time.

    Distinct from ``timed_out``, which means the provider was called and did not
    answer. Separated because the two have opposite causes and opposite remedies:
    a timeout is evidence about *this* provider, while starvation is evidence that
    some *other* seam is hogging the shared pool. Treating them alike let one
    hung backend trip an unrelated provider's breaker."""

    attempts: int = 0
    error: str | None = None
    latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)


def _now() -> float:
    return time.monotonic()


def _record(name: str, key: str) -> None:
    with _lock:
        _stats.setdefault(name, {}).setdefault(key, 0)
        _stats[name][key] += 1


def stats() -> dict[str, dict[str, int]]:
    """Per-provider counters, keyed exactly as ``_record`` writes them:

    ``calls`` · ``cache_hits`` · ``breaker_skips`` · ``retries`` · ``starved`` ·
    ``timeouts`` · ``exceptions`` · ``breaks``

    Enumerated in full because the previous version of this list was wrong in both
    directions — it named a ``hits`` key that does not exist (it is ``cache_hits``,
    so a dashboard built from the docstring got a ``KeyError``) and omitted
    ``breaker_skips`` and ``exceptions``, which are the two that separate "we chose
    not to ask" from "the provider raised".

    Keys are created lazily on first increment, so read with ``.get(key, 0)`` rather
    than indexing: a provider that has never failed has no ``breaks`` key at all.

    Exposed because these protections are otherwise invisible: without counters,
    nobody can tell whether a breaker is saving the system or silently hiding a
    backend that has been down for a week.

    ``starved`` is the one to watch under load. It counts calls that never reached
    their provider because no worker slot came free, so a rising count means the
    shared pool is the bottleneck rather than any backend — the lever is
    ``AIOPS_RESILIENCE_WORKERS``, not the provider's timeout. A hung provider holds
    its thread until it returns (see the module docstring), so a few permanent hangs
    are enough to starve everything else.
    """
    with _lock:
        return {k: dict(v) for k, v in _stats.items()}


def breaker_open(name: str) -> bool:
    with _lock:
        return _now() < _breakers.get(name, 0.0)


def trip_breaker(name: str, seconds: float) -> None:
    with _lock:
        _breakers[name] = _now() + seconds
    _record(name, "breaks")


def cache_get(key: str) -> tuple[bool, Any]:
    """``(hit, value)``. Returns a flag rather than ``None`` so a legitimately
    cached ``None`` is not mistaken for a miss."""
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if _now() >= expires:
            _cache.pop(key, None)
            return False, None
        return True, value


def cache_put(key: str, value: Any, ttl: float) -> None:
    if ttl <= 0:
        return
    with _lock:
        _cache[key] = (_now() + ttl, value)


def guard[T](
    name: str,
    fn: Callable[[], T],
    *,
    policy: ResiliencePolicy | None = None,
    cache_key: str | None = None,
    is_transient: Callable[[T], bool] | None = None,
    is_cacheable: Callable[[T], bool] | None = None,
    is_empty: Callable[[T], bool] | None = None,
) -> GuardOutcome[T]:
    """Run ``fn`` with timeout, retry, circuit breaking and caching.

    ``is_transient`` inspects a *returned* value to decide whether it represents a
    retryable failure. Necessary because these providers report failure as a
    status object rather than by raising — without this hook a ``FAILED`` result
    would be cached and never retried, which is the opposite of what it needs.

    ``is_cacheable`` suppresses caching of failures: caching an error means a
    transient blip is served for the whole TTL.

    Never raises. A guarded call that cannot succeed returns ``ok=False`` with the
    reason, because every caller here is an enrichment path where a hard failure
    would cost a verdict.
    """
    pol = policy or ResiliencePolicy()
    started = _now()
    outcome: GuardOutcome[T] = GuardOutcome()
    _record(name, "calls")

    if cache_key:
        hit, value = cache_get(cache_key)
        if hit:
            _record(name, "cache_hits")
            outcome.value = value
            outcome.ok = True
            outcome.from_cache = True
            outcome.latency_ms = (_now() - started) * 1000.0
            return outcome

    if breaker_open(name):
        _record(name, "breaker_skips")
        outcome.breaker_open = True
        outcome.error = "circuit open"
        outcome.notes.append(f"{name}: circuit open, call skipped")
        outcome.latency_ms = (_now() - started) * 1000.0
        return outcome

    last_error: str | None = None
    # Whether the provider was actually invoked on any attempt. Starvation only
    # means "we never got to ask" while this stays False; once the provider has been
    # reached and failed, a starved retry is a retry we could not make, not evidence
    # that the provider is innocent.
    reached_provider = False
    for attempt in range(pol.retries + 1):
        outcome.attempts = attempt + 1
        if attempt:
            # Exponential with jitter. Full jitter (uniform 0..delay) rather than
            # equal jitter: with only 2-3 attempts it spreads retries more evenly
            # across a recovering backend.
            delay = pol.backoff * (2 ** (attempt - 1))
            time.sleep(random.uniform(0, delay))
            _record(name, "retries")

        future = _executor.submit(fn)
        try:
            value = future.result(timeout=pol.timeout)
        except FuturesTimeout:
            # cancel() succeeds only if the task never started. That distinction is
            # the whole point: a queued task means the shared pool was saturated by
            # *other* seams, so this provider was never called and the deadline says
            # nothing about it. Recording that as this provider's timeout is what
            # let one hung backend trip an unrelated provider's breaker.
            if future.cancel():
                _record(name, "starved")
                # Attempts are counted before submit, so a starved iteration would
                # otherwise inflate the count with a call that never happened.
                outcome.attempts = max(0, outcome.attempts - 1)
                if reached_provider:
                    # The provider WAS called earlier in this loop and failed; this
                    # retry merely could not get a slot — very often because that
                    # same call's thread is still holding one. Reporting "never
                    # called" here would be flatly false, and leaving the breaker
                    # closed would exempt a demonstrably broken provider. So keep the
                    # real failure and fall through to the normal exhausted path.
                    outcome.notes.append(
                        f"{name}: retry starved (no worker within {pol.timeout}s, pool of "
                        f"{_MAX_WORKERS} saturated — likely by this provider's own hung "
                        f"call); reporting the earlier failure instead"
                    )
                    break
                last_error = (
                    f"no worker available within {pol.timeout}s "
                    f"(shared pool of {_MAX_WORKERS} saturated; provider not called)"
                )
                outcome.starved = True
                logger.warning("resilience: %s starved — %s", name, last_error)
                # Don't retry: the next attempt queues behind the same backlog, and
                # burning the retry budget on it delays the caller for nothing.
                break
            reached_provider = True
            _record(name, "timeouts")
            last_error = f"timed out after {pol.timeout}s"
            outcome.timed_out = True
            logger.warning("resilience: %s %s (attempt %d)", name, last_error, attempt + 1)
            continue
        except Exception as exc:
            reached_provider = True
            last_error = f"{type(exc).__name__}: {exc}"
            _record(name, "exceptions")
            logger.warning("resilience: %s raised %s (attempt %d)", name, last_error, attempt + 1)
            continue

        # A returned value that represents a transient failure is retried, not
        # accepted — see is_transient above.
        if is_transient is not None and is_transient(value):
            last_error = "provider reported a transient failure"
            outcome.value = value
            continue

        outcome.value = value
        outcome.ok = True
        if cache_key and (is_cacheable is None or is_cacheable(value)):
            ttl = pol.cache_ttl
            if is_empty is not None and is_empty(value):
                ttl = pol.cache_empty_ttl
            cache_put(cache_key, value, ttl)
        outcome.latency_ms = (_now() - started) * 1000.0
        return outcome

    # Every attempt failed. Only now is the breaker justified — this is the
    # ordering that stops a single blip from disabling a tier.
    #
    # Starvation is the one exception. The provider was never called, so nothing
    # here is evidence about it; tripping would punish this seam for another seam's
    # hung backend and hide a healthy provider for breaker_seconds. Leave it closed
    # and say so in the notes, so the real cause (a saturated pool) is what shows up.
    outcome.ok = False
    outcome.error = last_error or "all attempts failed"
    if outcome.starved:
        outcome.notes.append(
            f"{name}: {outcome.error}; breaker left closed because the provider was never called"
        )
    else:
        trip_breaker(name, pol.breaker_seconds)
        outcome.notes.append(
            f"{name}: {outcome.attempts} attempt(s) failed ({outcome.error}); "
            f"circuit open {pol.breaker_seconds:.0f}s"
        )
    outcome.latency_ms = (_now() - started) * 1000.0
    return outcome


def reset_for_tests() -> None:
    """Clear breakers, cache and counters.

    Process-global state, so ``tests/conftest.py`` must call this per test or one
    tripped breaker silently disables a provider for unrelated later tests — the
    same leak class the observability breaker fixtures exist to prevent.
    """
    with _lock:
        _breakers.clear()
        _cache.clear()
        _stats.clear()
