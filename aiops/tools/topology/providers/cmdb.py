"""CMDB topology provider — delegates to the ``itsm.cmdb.dependencies`` capability.

This is the tier that reproduces RA-007's historical behavior exactly: before
this package existed, ``_resolve_topology`` called
``get_registry().call("itsm.cmdb.dependencies", service=...)`` directly. That
call is preserved here verbatim, including its two distinct empty cases, so the
default chain is behaviour-preserving.

Deliberately goes *through the capability* rather than importing a concrete
provider: whichever provider is active for ``itsm.cmdb.dependencies`` is the one
consulted, so swapping that capability's provider (mock -> real CMDB) upgrades
this tier with no change here. That is also why this tier is distinct from the
``mock`` tier below it, which reads the static table directly — same data today,
different indirection, and only this one follows the registry's provider
selection.

Note on the ServiceNow tier: a *separate* ``snow`` provider will query
``cmdb_rel_ci`` for real CI relationships. It is not this module, and it is not
in the default chain, because adding a ServiceNow round-trip to the default path
would not be behaviour-preserving.
"""

from __future__ import annotations

import logging
import time

from aiops.tools.registry import get_registry
from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult

logger = logging.getLogger(__name__)

_CAPABILITY = "itsm.cmdb.dependencies"


class CmdbTopologyProvider:
    """Resolve dependencies via the registry's active CMDB provider."""

    name = "cmdb"

    def health(self) -> HealthStatus:
        """Healthy iff the capability has an active provider.

        Cheap and side-effect free — no network call. A missing registration is
        reported as unhealthy so the resolver can skip straight to the next tier
        instead of paying for a lookup that will raise ``KeyError``.
        """
        try:
            get_registry().by_capability(_CAPABILITY)
        except KeyError:
            return HealthStatus(healthy=False, detail=f"{_CAPABILITY} not registered")
        return HealthStatus(healthy=True, detail=f"{_CAPABILITY} registered")

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Query the CMDB capability for ``service``'s downstream dependencies.

        ``timeout_s`` is accepted for interface conformance but not enforced
        here: the registry ``call`` is synchronous and the underlying provider
        owns its own timeout (the mock is in-process; a real CMDB provider
        applies ``AIOPS_SERVICENOW_TIMEOUT``). Wrapping it in a thread purely to
        enforce a deadline would add a thread per correlate for no benefit while
        the only in-tree provider cannot block.
        """
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000.0

        try:
            res = get_registry().call(_CAPABILITY, service=service)
        except KeyError:
            # Capability not registered. Historically this produced
            # "topology: itsm.cmdb.dependencies not registered; no topology".
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                note=f"{_CAPABILITY} not registered",
                latency_ms=elapsed(),
            )
        except Exception as exc:
            # Defensive: never fail correlation on a topology lookup. The
            # registry already converts provider exceptions into
            # ToolResult(ok=False), so reaching here means the registry itself
            # misbehaved — still not worth failing the incident over.
            logger.warning("topology cmdb: lookup raised for %r: %s", service, exc)
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=f"{type(exc).__name__}",
                latency_ms=elapsed(),
            )

        if not res.ok:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=res.error or "call not ok",
                latency_ms=elapsed(),
            )

        # Two distinct empty cases, deliberately kept apart.
        #
        # ``payload_present`` mirrors the historical ``if res.ok and res.data:``
        # truthiness test: the mock/CMDB payload ``{"service": ..,
        # "dependencies": []}`` is a *truthy dict* even with no dependencies, so
        # "known service, no downstream deps" took the counted branch and traced
        # "0 downstream dep(s) from cmdb". Only a genuinely falsy body traced
        # "cmdb returned no dependencies". Callers reproduce that wording from
        # this flag rather than parsing ``note``.
        payload_present = bool(res.data)
        deps = list((res.data or {}).get("dependencies", []) or [])
        status = ProviderStatus.RESOLVED if deps else ProviderStatus.EMPTY
        return TopologyResult(
            provider=self.name,
            status=status,
            dependencies=deps,
            latency_ms=elapsed(),
            payload_present=payload_present,
            note=None if deps else ("zero dependencies" if payload_present else "empty body"),
        )
