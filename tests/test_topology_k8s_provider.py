"""Tests for the Kubernetes topology tier (Phase 1b).

This tier reads dependencies declared as container env vars on a Deployment::

    CART_ADDR=cart:8080   EMAIL_ADDR=http://email:8080   FLAGD_HOST=flagd

Its value is covering what the OTel tier cannot: HTTP callees (``email``,
``shipping``) are absent from ``rpc_client_*`` metrics entirely, and declared
edges need no live traffic to appear. The cost is that declarations can be stale,
which is why it ranks below ``otel`` in the chain.

The dominant risk is phantom dependencies. An env var can hold a Kafka broker, a
Kubernetes template like ``$(OTEL_COLLECTOR_NAME)``, or an external URL — and a
hostname invented from any of those becomes a graph node and then an RCA suspect.
So every candidate is whitelisted against real Services in the namespace, and
anything unresolvable is dropped rather than guessed. Most of these tests exist
to pin that behaviour.
"""

from __future__ import annotations

import pytest

from aiops.tools.topology.base import ProviderStatus
from aiops.tools.topology.providers.k8s import K8sTopologyProvider, extract_host

# ─── hostname extraction ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Shapes taken verbatim from the live cluster's checkout Deployment.
        ("cart:8080", "cart"),
        ("http://email:8080", "email"),
        ("flagd", "flagd"),
        ("product-catalog:8080", "product-catalog"),
        ("https://shipping:443/path", "shipping"),
        # Cluster-qualified names reduce to the service label.
        ("payment.otel-demo.svc.cluster.local:8080", "payment"),
        ("  cart:8080  ", "cart"),
        ("CART:8080", "cart"),
    ],
)
def test_extract_host(value, expected):
    assert extract_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        # Kubernetes env templates: the live checkout Deployment really contains
        # OTEL_EXPORTER_OTLP_ENDPOINT=http://$(OTEL_COLLECTOR_NAME):4317, which a
        # naive parser turns into a node named "$(otel_collector_name)".
        "http://$(OTEL_COLLECTOR_NAME):4317",
        "${SERVICE_HOST}",
        "://broken",
        "_leading_underscore",
    ],
)
def test_extract_host_refuses_to_guess(value):
    assert extract_host(value) is None


# ─── dependency resolution ───────────────────────────────────────────────────


class _Env:
    def __init__(self, name: str, value: str | None) -> None:
        self.name = name
        self.value = value


class _Container:
    def __init__(self, env: list[_Env]) -> None:
        self.env = env


class _Deployment:
    def __init__(self, env: list[_Env]) -> None:
        spec = type("S", (), {})()
        template = type("T", (), {})()
        template.spec = type("PS", (), {})()
        template.spec.containers = [_Container(env)]
        spec.template = template
        self.spec = spec


class _Svc:
    def __init__(self, name: str) -> None:
        self.metadata = type("M", (), {"name": name})()


def _provider(monkeypatch, deployment, services: list[str], *, read_raises=None):
    p = K8sTopologyProvider()

    class _Apps:
        def read_namespaced_deployment(self, name, ns):
            if read_raises is not None:
                raise read_raises
            return deployment

    class _Core:
        def list_namespaced_service(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Svc(s) for s in services]})()

    p._apps = _Apps()
    p._core = _Core()
    monkeypatch.setattr(p, "_ensure_client", lambda: None)
    return p


def test_declared_addresses_become_dependencies(monkeypatch):
    dep = _Deployment(
        [
            _Env("CART_ADDR", "cart:8080"),
            _Env("PAYMENT_ADDR", "payment:8080"),
            _Env("EMAIL_ADDR", "http://email:8080"),
            _Env("FLAGD_HOST", "flagd"),
        ]
    )
    p = _provider(monkeypatch, dep, ["cart", "payment", "email", "flagd", "checkout"])

    res = p.resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.RESOLVED
    assert res.dependencies == ["cart", "payment", "email", "flagd"]


def test_http_only_callees_are_captured(monkeypatch):
    """The whole reason this tier earns a place above the static table: these
    edges do not exist in rpc_client_* metrics at all."""
    dep = _Deployment(
        [_Env("EMAIL_ADDR", "http://email:8080"), _Env("SHIPPING_ADDR", "http://shipping:8080")]
    )
    p = _provider(monkeypatch, dep, ["email", "shipping", "checkout"])

    res = p.resolve("checkout", timeout_s=2.0)
    assert set(res.dependencies) == {"email", "shipping"}


def test_hosts_not_backed_by_a_service_are_dropped(monkeypatch):
    """Whitelist guard: KAFKA_ADDR points at a broker, not a demo Service. Emitting
    it would put a node in the graph that no service graph should contain."""
    dep = _Deployment(
        [_Env("CART_ADDR", "cart:8080"), _Env("KAFKA_ADDR", "kafka-broker-external:9092")]
    )
    p = _provider(monkeypatch, dep, ["cart", "checkout"])

    res = p.resolve("checkout", timeout_s=2.0)
    assert res.dependencies == ["cart"]


def test_env_templates_are_dropped(monkeypatch):
    dep = _Deployment(
        [
            _Env("CART_ADDR", "cart:8080"),
            _Env("OTEL_EXPORTER_OTLP_ENDPOINT", "http://$(OTEL_COLLECTOR_NAME):4317"),
        ]
    )
    p = _provider(monkeypatch, dep, ["cart", "checkout"])
    assert p.resolve("checkout", timeout_s=2.0).dependencies == ["cart"]


def test_self_reference_is_dropped(monkeypatch):
    """A service's own address in its env is configuration, not a dependency."""
    dep = _Deployment([_Env("CHECKOUT_ADDR", "checkout:8080"), _Env("CART_ADDR", "cart:8080")])
    p = _provider(monkeypatch, dep, ["checkout", "cart"])
    assert p.resolve("checkout", timeout_s=2.0).dependencies == ["cart"]


def test_non_address_env_vars_are_ignored(monkeypatch):
    dep = _Deployment(
        [_Env("LOG_LEVEL", "debug"), _Env("REPLICAS", "3"), _Env("CART_ADDR", "cart:8080")]
    )
    p = _provider(monkeypatch, dep, ["cart", "debug", "checkout"])
    assert p.resolve("checkout", timeout_s=2.0).dependencies == ["cart"]


def test_duplicates_are_collapsed(monkeypatch):
    dep = _Deployment([_Env("A_ADDR", "cart:8080"), _Env("B_ENDPOINT", "http://cart:8080")])
    p = _provider(monkeypatch, dep, ["cart", "checkout"])
    assert p.resolve("checkout", timeout_s=2.0).dependencies == ["cart"]


def test_env_entries_without_values_are_skipped(monkeypatch):
    dep = _Deployment([_Env("CART_ADDR", None), _Env("PAYMENT_ADDR", "payment:8080")])
    p = _provider(monkeypatch, dep, ["cart", "payment", "checkout"])
    assert p.resolve("checkout", timeout_s=2.0).dependencies == ["payment"]


# ─── status mapping ──────────────────────────────────────────────────────────


def test_deployment_without_addresses_is_empty_not_failed(monkeypatch):
    dep = _Deployment([_Env("LOG_LEVEL", "info")])
    p = _provider(monkeypatch, dep, ["checkout"])

    res = p.resolve("checkout", timeout_s=2.0)
    assert res.status is ProviderStatus.EMPTY
    assert res.payload_present is True


def test_missing_deployment_is_empty_not_failed(monkeypatch):
    """A 404 is a definite answer — no Deployment here — so it must not trip the
    resolver's circuit breaker."""

    class _NotFound(Exception):
        status = 404

    p = _provider(monkeypatch, None, ["checkout"], read_raises=_NotFound("not found"))

    res = p.resolve("nonexistent", timeout_s=2.0)
    assert res.status is ProviderStatus.EMPTY
    assert "no Deployment" in (res.note or "")


def test_api_error_is_failed_so_breaker_trips(monkeypatch):
    p = _provider(monkeypatch, None, ["checkout"], read_raises=RuntimeError("api exploded"))

    res = p.resolve("checkout", timeout_s=2.0)
    assert res.status is ProviderStatus.FAILED
    assert "RuntimeError" in (res.error or "")


def test_missing_kubeconfig_is_unavailable_not_failed(monkeypatch):
    """No cluster configured is a configuration state, not an outage — otherwise
    every CI run would breaker this tier."""
    p = K8sTopologyProvider()
    monkeypatch.setattr(p, "_ensure_client", lambda: "ConfigException: no kubeconfig")

    assert p.health().healthy is False
    res = p.resolve("checkout", timeout_s=2.0)
    assert res.status is ProviderStatus.UNAVAILABLE


def test_empty_service_name_is_empty_not_failed(monkeypatch):
    """EMPTY, not FAILED — see the same test in test_topology_otel_provider.py.

    FAILED trips the breaker, which would disable this tier for every service
    because one caller passed a blank name.
    """
    p = K8sTopologyProvider()
    monkeypatch.setattr(p, "_ensure_client", lambda: None)
    res = p.resolve("  ", timeout_s=2.0)

    assert res.status is ProviderStatus.EMPTY
    assert res.error is None, "no provider error occurred"
    assert "empty service name" in (res.note or "")


def test_import_does_not_require_a_kubeconfig():
    """Constructing the provider must not touch the cluster, or importing
    aiops.tools.topology would fail on any machine without a kubeconfig."""
    p = K8sTopologyProvider()
    assert p._apps is None


# ─── chain integration ───────────────────────────────────────────────────────


def test_k8s_is_registered_but_opt_in():
    from aiops.tools.topology import resolver as topo_resolver

    assert "k8s" in topo_resolver._PROVIDERS
    assert topo_resolver._chain() == (["cmdb", "mock"], []), "k8s must not join the default chain"


def test_full_five_tier_chain_can_be_configured(monkeypatch):
    from aiops.tools.topology import resolver as topo_resolver

    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "otel,snow,k8s,cmdb,mock")
    assert topo_resolver._chain() == (["otel", "snow", "k8s", "cmdb", "mock"], [])
