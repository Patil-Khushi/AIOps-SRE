"""TTL cache for topology lookups and provider health checks.

Mirrors ``aiops/llm/health.py``: a plain module-level dict with per-entry
expiry, and *different TTLs for success and failure* — a healthy answer is
worth holding onto, a failure should be retried soon after the operator fixes
whatever broke.

Why topology is worth caching at all
------------------------------------
``correlate()`` resolves topology on every call, and RA-008 Incident Commander
calls ``correlate()`` inside a larger flow. Service dependency graphs change on
deploy timescales (minutes to days), not per-request, so re-querying
ServiceNow or Prometheus for every correlation is pure latency. A short TTL
keeps the graph fresh enough to reflect a deploy while collapsing the burst of
lookups a single incident generates.

``EMPTY`` gets its own TTL, shorter than ``RESOLVED``. On a stock PDI "no CI
records" is the steady state, but it is also exactly what a half-populated CMDB
looks like mid-import — so we re-check it more eagerly than a positive answer
without hammering it like a hard failure.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Seconds. Env-tunable to match the loki/jaeger provider convention, where every
# timeout and breaker window is overridable without a code change.
_RESOLVED_TTL = float(os.environ.get("AIOPS_TOPOLOGY_CACHE_TTL", "60"))
_EMPTY_TTL = float(os.environ.get("AIOPS_TOPOLOGY_CACHE_EMPTY_TTL", "30"))
_FAILURE_TTL = float(os.environ.get("AIOPS_TOPOLOGY_CACHE_FAILURE_TTL", "10"))
_HEALTH_TTL = float(os.environ.get("AIOPS_TOPOLOGY_HEALTH_TTL", "60"))

# key -> (expires_at_monotonic, value)
_entries: dict[str, tuple[float, Any]] = {}


def ttl_for_status(status_value: str) -> float:
    """TTL to use for a given ``ProviderStatus`` value.

    Takes the raw ``str`` value rather than the enum to keep this module free of
    an import back into ``base`` (``base`` is the lower layer; a cycle here
    would be gratuitous).
    """
    if status_value == "resolved":
        return _RESOLVED_TTL
    if status_value == "empty":
        return _EMPTY_TTL
    # unavailable / failed — retry soon.
    return _FAILURE_TTL


def get(key: str) -> Any | None:
    """Return the cached value, or ``None`` when absent or expired.

    Expired entries are evicted on read rather than by a background sweep —
    the keyspace is bounded by (providers x services), so lazy eviction is
    sufficient and keeps this dependency-free.
    """
    hit = _entries.get(key)
    if hit is None:
        return None
    expires_at, value = hit
    if time.monotonic() >= expires_at:
        _entries.pop(key, None)
        return None
    return value


def put(key: str, value: Any, ttl_seconds: float) -> None:
    """Cache ``value`` under ``key`` for ``ttl_seconds``.

    A non-positive TTL is treated as "do not cache" so callers can disable
    caching by setting the env var to 0 without a separate branch.
    """
    if ttl_seconds <= 0:
        return
    _entries[key] = (time.monotonic() + ttl_seconds, value)


def health_ttl() -> float:
    return _HEALTH_TTL


def clear() -> None:
    """Drop every cached entry.

    Test seam, mirroring ``loki._reset_circuit_for_tests``: this is
    process-global state, so without an autouse fixture calling it one test's
    cached topology would leak into the next and make ordering matter.
    """
    _entries.clear()
