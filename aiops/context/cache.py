"""Intra-incident caching for the Context Engineering Layer.

This module is deliberately thin. It builds keys and picks TTLs; the storage,
locking and eviction all belong to ``aiops.tools.resilience``.

Why reuse the resilience cache instead of writing one
----------------------------------------------------
``resilience.py`` exists because three provider seams each hand-rolled their own
protections and each forgot a different one — its module docstring carries the
table. Adding a fourth cache here would be exactly that mistake again. But the
concrete, decisive reason is hermeticity: ``tests/conftest.py::_hermetic_resilience``
already resets that cache around every test. Reusing it means the context layer's
cache is hermetic for free and needs no eleventh autouse fixture. A private dict
here would leak between tests until someone noticed and wrote the fixture.

Why every key is incident-scoped
--------------------------------
``correlation_id`` is a mandatory argument, not an optional one. The resilience
cache defaults to a 60-second TTL, and a key like ``oncall.schedule.lookup:payment``
would happily serve one incident's on-call engineer to an unrelated incident a
minute later — across a shift boundary that pages the wrong human. Scoping by
incident means the deduplication only ever happens *within* the incident that
earned it, which is the only place it is safe.
"""

from __future__ import annotations

from typing import Any

from aiops.context.models import SectionSpec, SectionStatus
from aiops.tools import resilience

_KEY_PREFIX = "ctx"


def section_key(correlation_id: str, spec: SectionSpec) -> str:
    """Cache key for one section spec within one incident.

    Keyed on the spec's query *fingerprint* rather than its ``query_id``, so two
    agents that ask the identical question under different names still share one
    round-trip. Naming the key after the label instead would leave the duplication
    this layer exists to remove quietly in place.
    """
    return f"{_KEY_PREFIX}:{correlation_id}:{spec.source}:{spec.fingerprint()}"


def ttl_for_status(status: SectionStatus) -> float:
    """How long a result of this status stays cached.

    Follows the status-aware model already established by
    ``aiops/tools/topology/cache.py``:

    * ``COLLECTED`` keeps the standard TTL — a positive answer is unlikely to
      change within one incident's lifetime.
    * ``EMPTY`` gets a shorter one. An empty answer is more likely to become
      non-empty soon (a service that has not emitted yet, a half-populated index)
      than a positive answer is to change, so it is rechecked sooner without being
      treated as a failure.
    * ``FAILED`` / ``UNAVAILABLE`` / ``NOT_REQUESTED`` are **not cached at all**
      (TTL 0). Caching a failure means a transient blip is replayed for the whole
      window, which is how one dropped packet turns into a whole incident's worth
      of missing evidence.
    """
    policy = resilience.ResiliencePolicy()
    if status is SectionStatus.COLLECTED:
        return policy.cache_ttl
    if status is SectionStatus.EMPTY:
        return policy.cache_empty_ttl
    return 0.0


def get(correlation_id: str, spec: SectionSpec) -> tuple[bool, Any]:
    """``(hit, value)`` for a section spec.

    Returns a hit flag rather than ``None`` so a legitimately cached empty section
    is not mistaken for a miss — the same reasoning as
    ``resilience.cache_get``.
    """
    return resilience.cache_get(section_key(correlation_id, spec))


def put(correlation_id: str, spec: SectionSpec, value: Any, status: SectionStatus) -> None:
    """Cache a section result, with a TTL chosen by its status.

    A zero TTL is a no-op in ``resilience.cache_put``, so failures simply do not
    land — the caller does not have to remember to check first.
    """
    resilience.cache_put(section_key(correlation_id, spec), value, ttl_for_status(status))
