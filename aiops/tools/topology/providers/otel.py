"""OpenTelemetry-derived topology provider (observed call graph).

Derives real caller -> callee edges from gRPC client metrics in Prometheus,
which the OTel Collector already exports for the demo. Unlike the CMDB tiers
this reflects what services *actually called* in the window, not what someone
declared.

Why rpc_client_* and not the servicegraph connector
---------------------------------------------------
The idiomatic source would be ``traces_service_graph_request_total`` from the
collector's ``servicegraph`` connector. A live check of this cluster found that
connector is **not enabled** (no ``service_graph_*`` series exist), and Jaeger's
``/api/dependencies`` returns 404 on this build. What *is* available is::

    rpc_client_duration_milliseconds_count{service_name="checkout",
                                           rpc_service="oteldemo.PaymentService"}

``service_name`` is the caller and ``rpc_service`` is the callee, which is a
complete edge. Verified against the running cluster::

    checkout -> CartService, CurrencyService, PaymentService, ProductCatalogService
    ad       -> flagd.evaluation.v1.Service

Enabling the servicegraph connector later is a drop-in swap behind this same
provider interface — only ``_EDGE_QUERY`` and the label names change.

HTTP edges were evaluated and rejected: ``http_client_duration_milliseconds``
mostly lacks a populated ``server_address``, so it yields callers with no
identifiable callee.

Name normalization is the risk
------------------------------
``rpc_service`` is an RPC *interface* name, not a Kubernetes service name, so it
must be mapped (``oteldemo.PaymentService`` -> ``payment``). A wrong mapping
invents a dependency that never existed, and suspects derived from it flow
straight into the RCA agent's prompt. Unmappable names are therefore **dropped,
never guessed** — a missing edge degrades the evidence pack, a fabricated one
corrupts it.
"""

from __future__ import annotations

import logging
import os
import re
import time

from aiops.tools.registry import get_registry
from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult

logger = logging.getLogger(__name__)

_CAPABILITY = "observability.metrics.query"

# Rate window for edge discovery. Long enough that a low-traffic dependency still
# registers, short enough that a removed dependency ages out within an incident.
_RATE_WINDOW = os.environ.get("AIOPS_TOPOLOGY_OTEL_WINDOW", "5m")

# ``> 0`` drops series that exist but are idle in the window — a dependency with
# no traffic is not evidence of a current call path.
_EDGE_QUERY = (
    "sum by (service_name, rpc_service) "
    "(rate(rpc_client_duration_milliseconds_count[{window}])) > 0"
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_rpc_service(rpc_service: str) -> str | None:
    """Map an RPC interface name to a service name, or ``None`` if unmappable.

    Rules, derived from the shapes actually present in this cluster:

    - ``oteldemo.PaymentService``        -> ``payment``
    - ``oteldemo.ProductCatalogService`` -> ``product-catalog``
    - ``flagd.evaluation.v1.Service``    -> ``flagd``

    The last rule is the one worth spelling out. The naive implementation takes
    the final dot-segment and strips a ``Service`` suffix — which for
    ``flagd.evaluation.v1.Service`` leaves the **empty string**, and an empty
    node id would then join the graph as a real service. When the final segment
    is a bare ``Service`` (carrying no name of its own) we fall back to the first
    segment, which is the package root and in practice the service name.

    Returns ``None`` rather than a best guess for anything that does not reduce
    to a usable name.
    """
    raw = (rpc_service or "").strip()
    if not raw:
        return None

    segments = [s for s in raw.split(".") if s]
    if not segments:
        return None

    candidate = segments[-1]
    # A bare "Service" tail names nothing; the package root does.
    if candidate.lower() == "service":
        candidate = segments[0]
    else:
        candidate = re.sub(r"Service$", "", candidate)

    if not candidate:
        return None

    # CamelCase -> kebab-case (ProductCatalog -> product-catalog).
    kebab = _CAMEL_BOUNDARY.sub("-", candidate).lower()
    kebab = kebab.replace("_", "-").strip("-")
    return kebab or None


def _query_edges() -> tuple[list[tuple[str, str, float]], str | None]:
    """Return ([(caller, callee, call_rate), ...], error).

    One query returns the whole graph, so building an N-node graph costs one
    round-trip rather than N — which is what makes the Phase-2 graph builder
    affordable on this tier.
    """
    promql = _EDGE_QUERY.format(window=_RATE_WINDOW)
    res = get_registry().call(_CAPABILITY, promql=promql)
    if not res.ok:
        return [], res.error or "metrics query failed"

    edges: list[tuple[str, str, float]] = []
    for row in (res.data or {}).get("results", []) or []:
        labels = row.get("metric") or {}
        caller = str(labels.get("service_name") or "").strip().lower()
        callee_raw = str(labels.get("rpc_service") or "").strip()
        if not caller or not callee_raw:
            continue
        callee = normalize_rpc_service(callee_raw)
        if callee is None:
            logger.debug("topology otel: dropping unmappable rpc_service %r", callee_raw)
            continue
        if callee == caller:
            # Self-edges are an artifact of internal instrumentation, not a
            # dependency; keeping them would make every service its own suspect.
            continue
        try:
            rate = float((row.get("value") or [None, "0"])[1])
        except (TypeError, ValueError, IndexError):
            rate = 0.0
        edges.append((caller, callee, rate))
    return edges, None


def fetch_edges() -> tuple[list[tuple[str, str, float]], str | None]:
    """Public edge accessor for the graph builder (Phase 2).

    Exposed separately from ``resolve`` so the graph builder can obtain the whole
    edge set in one query instead of walking the chain per node.
    """
    return _query_edges()


class OtelTopologyProvider:
    """Resolve dependencies from observed gRPC client traffic."""

    name = "otel"

    def health(self) -> HealthStatus:
        """Healthy iff the metrics capability is registered.

        No network probe: the Prometheus provider owns its own timeout and
        circuit breaker, so a probe here would duplicate that machinery and pay
        an extra round-trip on every cache miss.
        """
        try:
            get_registry().by_capability(_CAPABILITY)
        except KeyError:
            return HealthStatus(healthy=False, detail=f"{_CAPABILITY} not registered")
        return HealthStatus(healthy=True, detail="prometheus metrics capability registered")

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Return the callees observed for ``service`` in the rate window."""
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000.0

        target = (service or "").strip().lower()
        if not target:
            # EMPTY, not FAILED. A blank service name is a caller-input error, and
            # FAILED trips the breaker — so one malformed request would disable this
            # tier for every service for _CIRCUIT_OPEN_SECONDS. The provider is
            # working fine; there is simply nothing to look up.
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                note="empty service name; nothing to resolve",
                latency_ms=elapsed(),
            )

        try:
            edges, error = _query_edges()
        except KeyError:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                note=f"{_CAPABILITY} not registered",
                latency_ms=elapsed(),
            )
        except Exception as exc:
            logger.warning("topology otel: query raised for %r: %s", service, exc)
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=f"{type(exc).__name__}",
                latency_ms=elapsed(),
            )

        if error is not None:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=error,
                latency_ms=elapsed(),
            )

        deps: list[str] = []
        for caller, callee, _rate in edges:
            if caller == target and callee not in deps:
                deps.append(callee)

        if deps:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.RESOLVED,
                dependencies=deps,
                latency_ms=elapsed(),
                payload_present=True,
            )

        # Query succeeded; this service made no observed outbound RPC in the
        # window. A leaf service, or simply idle traffic — a real answer, so the
        # chain falls through without treating Prometheus as broken.
        return TopologyResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            latency_ms=elapsed(),
            payload_present=True,
            note=f"no outbound rpc observed in {_RATE_WINDOW}",
        )
