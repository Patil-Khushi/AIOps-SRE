"""Static-table topology provider — the terminal tier.

Reads ``aiops.tools.mock_providers._DEPENDENCIES_MAPPING`` directly rather than
going through the registry. That indirection is what distinguishes this tier
from the ``cmdb`` tier above it: ``cmdb`` consults whichever provider is active
for ``itsm.cmdb.dependencies`` (and so improves when that capability is
upgraded), while this tier is a fixed, always-available floor.

Its job is to guarantee the chain always terminates with a defensible answer, so
an offline demo or a CI run with no cluster still produces a meaningful evidence
pack. This is the same reasoning as RA-007's synthetic signal fallback and the
RCA agent's ``_fallback_verdict``.

Reusing the existing table rather than copying it keeps one source of truth for
demo dependencies — a second copy would drift from the truth files the eval
harness grades against.
"""

from __future__ import annotations

import time

from aiops.tools.mock_providers import _DEPENDENCIES_MAPPING
from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult


class MockTopologyProvider:
    """Look up dependencies in the static demo table."""

    name = "mock"

    def health(self) -> HealthStatus:
        """Always healthy — an in-process dict cannot be unreachable.

        Reported honestly rather than omitted so the resolver's health handling
        has no special case for "the tier that cannot fail".
        """
        return HealthStatus(healthy=True, detail=f"{len(_DEPENDENCIES_MAPPING)} services in table")

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Return the table's dependencies for ``service``.

        Key normalization matches ``mock_cmdb_dependencies`` (lowercase + strip)
        so this tier and the ``cmdb`` tier agree on lookups today; diverging here
        would make the fallback silently answer differently from the tier above
        it. ``timeout_s`` is unused — an in-process dict lookup cannot block.
        """
        started = time.monotonic()
        key = service.lower().strip()
        deps = list(_DEPENDENCIES_MAPPING.get(key, []))
        latency_ms = (time.monotonic() - started) * 1000.0
        if deps:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.RESOLVED,
                dependencies=deps,
                latency_ms=latency_ms,
            )
        # Unknown service: a definite "this table has nothing for you", not a
        # malfunction. Terminal tier, so the chain ends with an empty topology —
        # which downstream treats as "error is service-internal", the correct
        # reading when no dependency information exists.
        return TopologyResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            latency_ms=latency_ms,
            note=f"{key!r} not in static table",
        )
