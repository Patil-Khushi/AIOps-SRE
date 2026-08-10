"""Kubernetes topology provider — dependencies declared in container env vars.

What this actually reads
------------------------
The initial assumption was that Kubernetes can only expose *structural*
adjacency (Services, ownerRefs, NetworkPolicies) and therefore could not answer
"what does X depend on". Inspecting the live cluster disproved that: every
service declares its callees as environment variables on its Deployment::

    CART_ADDR=cart:8080              PAYMENT_ADDR=payment:8080
    CURRENCY_ADDR=currency:8080      PRODUCT_CATALOG_ADDR=product-catalog:8080
    EMAIL_ADDR=http://email:8080     SHIPPING_ADDR=http://shipping:8080

That is a real declared-dependency graph, and it has a property the OTel tier
lacks: it covers **every protocol**. The OTel tier derives edges from
``rpc_client_*`` metrics, so HTTP callees like ``email`` and ``shipping`` are
invisible to it, and it needs live traffic before an edge appears at all. This
tier needs neither — it reads intent, not observation.

The trade-off is the mirror image: declared dependencies can be stale (an env
var left behind after a refactor) and cannot show *call rates*. So this tier
sits below ``otel`` in the chain — observed beats declared — but above the
static table, and it fills OTel's protocol and traffic gaps.

Only hostnames that resolve to a real Service in the namespace are emitted. An
env var can hold anything (``KAFKA_ADDR``, a template like
``$(OTEL_COLLECTOR_NAME)``, an external URL), and inventing a dependency from an
unresolvable string would put a phantom node into the graph and a phantom
suspect into the RCA agent's evidence.
"""

from __future__ import annotations

import logging
import os
import re
import time

from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult

logger = logging.getLogger(__name__)

_NAMESPACE = os.environ.get("AIOPS_K8S_NAMESPACE", "otel-demo")
_TIMEOUT = float(os.environ.get("AIOPS_K8S_TIMEOUT", "5"))

# Env var names that conventionally carry a service address. Matched on the
# suffix so ``PRODUCT_CATALOG_ADDR`` and ``FLAGD_HOST`` both qualify.
_ADDR_SUFFIXES = ("_ADDR", "_ADDRESS", "_ENDPOINT", "_HOST", "_URL", "_SERVICE")

# Strip an optional scheme and any path/port, leaving the hostname.
_HOST_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?P<host>[^/:?#]+)")


def extract_host(value: str) -> str | None:
    """Pull the hostname out of an address-ish env value.

    Handles ``cart:8080``, ``http://email:8080``, and bare ``flagd``. Returns
    ``None`` for anything that is not a plain hostname — notably Kubernetes env
    templates like ``$(OTEL_COLLECTOR_NAME)``, which would otherwise become a
    node literally named ``$(otel_collector_name)``.
    """
    raw = (value or "").strip()
    if not raw or "$(" in raw or "${" in raw:
        return None
    m = _HOST_RE.match(raw)
    if not m:
        return None
    host = m.group("host").strip().lower()
    # A cross-namespace or cluster-qualified name reduces to its first label:
    # "payment.otel-demo.svc.cluster.local" -> "payment".
    host = host.split(".")[0]
    if not host or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", host):
        return None
    return host


class K8sTopologyProvider:
    """Resolve dependencies from a Deployment's declared service addresses."""

    name = "k8s"

    def __init__(self) -> None:
        # Client is built lazily: importing this module must not require a
        # kubeconfig, so that `import aiops.tools.topology` stays safe in CI and
        # on machines with no cluster (same posture as the feature-flags adapter).
        self._apps = None
        self._core = None
        self._init_error: str | None = None

    def _ensure_client(self) -> str | None:
        """Build the API clients on first use. Returns an error string or None."""
        if self._apps is not None:
            return None
        if self._init_error is not None:
            return self._init_error
        try:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            api_client = client.ApiClient()
            self._apps = client.AppsV1Api(api_client)
            self._core = client.CoreV1Api(api_client)
        except Exception as exc:
            # No kubeconfig, no cluster, or the package is absent. All of these
            # mean "this tier is not usable here", not "the cluster is broken".
            self._init_error = f"{type(exc).__name__}: {exc}"
            return self._init_error
        return None

    def health(self) -> HealthStatus:
        """Report whether a kube client can be constructed.

        Deliberately does not call the API: a per-lookup round-trip just to
        answer "are you there?" would double this tier's cost, and the lookup
        itself already reports reachability.
        """
        err = self._ensure_client()
        if err is not None:
            return HealthStatus(healthy=False, detail=f"kube client unavailable ({err})")
        return HealthStatus(healthy=True, detail=f"kube client ready (ns={_NAMESPACE})")

    def _service_names(self) -> set[str]:
        """Names of Services in the namespace — the whitelist for edges."""
        svcs = self._core.list_namespaced_service(_NAMESPACE, timeout_seconds=int(_TIMEOUT))
        return {
            s.metadata.name.lower() for s in (svcs.items or []) if s.metadata and s.metadata.name
        }

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Read ``service``'s Deployment env and return its declared callees."""
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000.0

        target = (service or "").strip().lower()
        if not target:
            # EMPTY, not FAILED — see the same guard in providers/otel.py. FAILED
            # trips the breaker, so a caller-input error would disable this tier for
            # every service rather than just returning nothing for this one.
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                note="empty service name; nothing to resolve",
                latency_ms=elapsed(),
            )

        err = self._ensure_client()
        if err is not None:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                note=f"kube client unavailable ({err})",
                latency_ms=elapsed(),
            )

        try:
            dep = self._apps.read_namespaced_deployment(target, _NAMESPACE)
            known_services = self._service_names()
        except Exception as exc:
            name = type(exc).__name__
            # A 404 is a definite answer — this service has no Deployment here —
            # not a malfunction, so it must not trip the resolver's breaker.
            if getattr(exc, "status", None) == 404 or "NotFound" in name:
                return TopologyResult(
                    provider=self.name,
                    status=ProviderStatus.EMPTY,
                    note=f"no Deployment {target!r} in namespace {_NAMESPACE!r}",
                    latency_ms=elapsed(),
                )
            logger.warning("topology k8s: lookup failed for %r: %s", target, exc)
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.FAILED,
                error=name,
                latency_ms=elapsed(),
            )

        deps: list[str] = []
        containers = (dep.spec.template.spec.containers if dep.spec else None) or []
        for container in containers:
            for env in container.env or []:
                if not env.name or not env.value:
                    continue
                if not env.name.upper().endswith(_ADDR_SUFFIXES):
                    continue
                host = extract_host(env.value)
                # Whitelisted against real Services, and self-references dropped:
                # a service's own address in its env is config, not a dependency.
                if host and host != target and host in known_services and host not in deps:
                    deps.append(host)

        if deps:
            return TopologyResult(
                provider=self.name,
                status=ProviderStatus.RESOLVED,
                dependencies=deps,
                latency_ms=elapsed(),
                payload_present=True,
            )

        return TopologyResult(
            provider=self.name,
            status=ProviderStatus.EMPTY,
            latency_ms=elapsed(),
            payload_present=True,
            note="Deployment declares no resolvable service addresses",
        )
