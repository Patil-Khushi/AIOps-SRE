"""ServiceNow CI-relationship topology provider (tier 3).

Resolves dependencies from real ``cmdb_rel_ci`` relationships via the
``itsm.cmdb.relationships`` capability, which is implemented in
``aiops.tools.itsm.servicenow`` on top of that module's shared ``_config()`` /
``_request()`` helpers. This provider therefore reuses the **existing single
ServiceNow connection and credential path** — including the OAuth2
client-credentials support added alongside it — rather than opening a second
client against the same instance.

Distinct from the ``cmdb`` tier
-------------------------------
``cmdb`` goes through ``itsm.cmdb.dependencies``, which today resolves to the
static mock table. This tier queries the real CMDB. Keeping them separate is the
point: collapsing both onto one source would mean the chain has only one real
opinion about topology while appearing to have two.

Expect EMPTY in the demo
------------------------
A read-only probe of the configured PDI found ``cmdb_ci_service`` has **zero
rows** for the OpenTelemetry demo services (``checkout``, ``payment``,
``product-catalog``). So on this instance the honest answer from this tier is
"queried successfully, no relationships", and the chain falls through to the
lower tiers. That is a correct outcome, not a failure — and it is why
``ProviderStatus.EMPTY`` exists separately from ``FAILED``. Treating it as an
error would hold this tier's breaker open permanently while nothing is wrong.
"""

from __future__ import annotations

import logging
import time

from aiops.tools.registry import get_registry
from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult

logger = logging.getLogger(__name__)

_CAPABILITY = "itsm.cmdb.relationships"


class SnowTopologyProvider:
    """Resolve dependencies from ServiceNow CI relationships."""

    name = "snow"

    def health(self) -> HealthStatus:
        """Healthy iff the capability is registered.

        Registration is itself gated on ``AIOPS_USE_MOCK_ITSM=false``, so an
        instance running in mock mode reports unhealthy here and the resolver
        skips straight past this tier — no wasted round-trip, and no misleading
        "ServiceNow returned nothing" when ServiceNow was never asked.

        Deliberately does not probe the network: a health check that costs an
        HTTP request would be paid on every cache-miss lookup, and the query
        itself already reports reachability.
        """
        try:
            get_registry().by_capability(_CAPABILITY)
        except KeyError:
            return HealthStatus(
                healthy=False,
                detail="itsm.cmdb.relationships not registered (AIOPS_USE_MOCK_ITSM=true?)",
            )
        return HealthStatus(healthy=True, detail="servicenow relationships capability registered")

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Query CI relationships for ``service``.

        ``timeout_s`` is accepted for interface conformance; the underlying
        provider enforces ``AIOPS_SERVICENOW_TIMEOUT`` on its own HTTP calls, and
        re-imposing a deadline here would need a thread per lookup for no gain.
        """
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000.0

        try:
            res = get_registry().call(_CAPABILITY, service=service)
        except KeyError:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                note=f"{_CAPABILITY} not registered",
                latency_ms=elapsed(),
            )
        except Exception as exc:
            logger.warning("topology snow: lookup raised for %r: %s", service, exc)
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=f"{type(exc).__name__}",
                latency_ms=elapsed(),
            )

        if not res.ok:
            # A real query failure (auth, network, ServiceNow error). This is the
            # only branch that should trip the resolver's breaker.
            error = res.error or "call not ok"
            configured = (res.metadata or {}).get("configured")
            if configured is False:
                # Credentials absent is a configuration state, not a malfunction —
                # skip cleanly so an unconfigured PDI doesn't breaker the tier.
                return TopologyResult(
                    provider=self.name,
                    status=ProviderStatus.UNAVAILABLE,
                    note="servicenow not configured",
                    latency_ms=elapsed(),
                )
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=error,
                latency_ms=elapsed(),
            )

        deps = list((res.data or {}).get("dependencies", []) or [])
        reason = (res.metadata or {}).get("reason")
        if deps:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.RESOLVED,
                dependencies=deps,
                latency_ms=elapsed(),
                payload_present=True,
            )

        # Queried fine, genuinely nothing. ``reason`` separates "this service has
        # no CI record" from "it has a CI with no relationships" — different
        # CMDB-hygiene problems, and worth keeping distinguishable in the trace.
        return TopologyResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            latency_ms=elapsed(),
            payload_present=reason != "no_ci_record",
            note=reason or "no relationships",
        )
