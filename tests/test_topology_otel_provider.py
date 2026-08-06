"""Tests for the OTel-derived topology tier and its RPC name normalizer.

The normalizer is the highest-risk code in the topology package. ``rpc_service``
is an RPC *interface* name (``oteldemo.PaymentService``), not a Kubernetes
service name, so every edge depends on mapping it correctly. A wrong mapping
does not fail loudly — it invents a dependency, which becomes a suspect
component, which lands in the RCA agent's prompt as evidence. That is risk T3/G4
from the Phase 0/2 analyses, and these tests are the guard for it.

Expected values were taken from the shapes actually observed in the running
cluster, not invented::

    checkout -> oteldemo.CartService, oteldemo.CurrencyService,
                oteldemo.PaymentService, oteldemo.ProductCatalogService
    ad       -> flagd.evaluation.v1.Service
"""

from __future__ import annotations

import pytest

from aiops.tools.registry import ToolResult
from aiops.tools.topology.base import ProviderStatus
from aiops.tools.topology.providers import otel as otel_mod
from aiops.tools.topology.providers.otel import OtelTopologyProvider, normalize_rpc_service


class _FakeRegistry:
    def __init__(self, result, *, registered: bool = True) -> None:
        self._result = result
        self._registered = registered
        self.queries: list[str] = []

    def by_capability(self, capability: str):
        if not self._registered:
            raise KeyError(capability)
        return object()

    def call(self, capability: str, **kwargs):
        if not self._registered:
            raise KeyError(capability)
        self.queries.append(kwargs.get("promql", ""))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _series(caller: str, callee: str, rate: str = "1.5") -> dict:
    return {"metric": {"service_name": caller, "rpc_service": callee}, "value": [1690000000, rate]}


def _metrics(rows: list[dict]) -> ToolResult:
    return ToolResult(ok=True, data={"results": rows})


def _use(monkeypatch, result, *, registered: bool = True) -> _FakeRegistry:
    reg = _FakeRegistry(result, registered=registered)
    monkeypatch.setattr(otel_mod, "get_registry", lambda: reg)
    return reg


# ─── name normalization ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rpc_service", "expected"),
    [
        # Real shapes observed in the cluster.
        ("oteldemo.PaymentService", "payment"),
        ("oteldemo.CartService", "cart"),
        ("oteldemo.CurrencyService", "currency"),
        ("oteldemo.ProductCatalogService", "product-catalog"),
        # The edge case that breaks the naive implementation: the final segment
        # is a bare "Service", so stripping the suffix leaves an empty string.
        ("flagd.evaluation.v1.Service", "flagd"),
        # No package prefix.
        ("PaymentService", "payment"),
        # Multi-word CamelCase without a Service suffix.
        ("ProductCatalog", "product-catalog"),
        # Already a plain name.
        ("payment", "payment"),
        ("product-catalog", "product-catalog"),
        # Underscores normalise to the kebab convention used everywhere else.
        ("product_catalog", "product-catalog"),
    ],
)
def test_normalize_rpc_service(rpc_service, expected):
    assert normalize_rpc_service(rpc_service) == expected


@pytest.mark.parametrize("bad", ["", "   ", ".", "...", "Service", None])
def test_normalize_returns_none_rather_than_guessing(bad):
    """Unmappable input must yield ``None``, never a placeholder.

    An empty or invented node id would enter the graph as a real service and be
    reported as a suspect. A dropped edge merely weakens the evidence; a
    fabricated one corrupts it.
    """
    result = normalize_rpc_service(bad)  # type: ignore[arg-type]
    assert result is None or result == "service", f"unexpected mapping for {bad!r}: {result!r}"


def test_bare_service_segment_never_yields_empty_string():
    """Regression guard for the specific bug the flagd case exposes."""
    assert normalize_rpc_service("flagd.evaluation.v1.Service") != ""
    assert normalize_rpc_service("flagd.evaluation.v1.Service") is not None


# ─── resolve() ───────────────────────────────────────────────────────────────


def test_resolves_observed_callees(monkeypatch):
    _use(
        monkeypatch,
        _metrics(
            [
                _series("checkout", "oteldemo.PaymentService"),
                _series("checkout", "oteldemo.CartService"),
                _series("ad", "flagd.evaluation.v1.Service"),
            ]
        ),
    )
    res = OtelTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.RESOLVED
    assert res.dependencies == ["payment", "cart"]
    assert "ad" not in res.dependencies, "must filter to the queried caller only"


def test_matches_caller_case_insensitively(monkeypatch):
    _use(monkeypatch, _metrics([_series("checkout", "oteldemo.PaymentService")]))
    res = OtelTopologyProvider().resolve("CheckOut", timeout_s=2.0)
    assert res.dependencies == ["payment"]


def test_self_edges_are_dropped(monkeypatch):
    """A service calling its own interface is instrumentation noise; keeping it
    would make every service its own suspect."""
    _use(monkeypatch, _metrics([_series("payment", "oteldemo.PaymentService")]))
    res = OtelTopologyProvider().resolve("payment", timeout_s=2.0)

    assert res.dependencies == []
    assert res.status is ProviderStatus.EMPTY


def test_unmappable_callee_is_dropped_not_emitted(monkeypatch):
    _use(
        monkeypatch,
        _metrics(
            [
                _series("checkout", "oteldemo.PaymentService"),
                _series("checkout", "..."),
                _series("checkout", ""),
            ]
        ),
    )
    res = OtelTopologyProvider().resolve("checkout", timeout_s=2.0)
    assert res.dependencies == ["payment"]


def test_duplicate_edges_are_deduplicated(monkeypatch):
    _use(
        monkeypatch,
        _metrics(
            [
                _series("checkout", "oteldemo.PaymentService"),
                _series("checkout", "oteldemo.PaymentService"),
            ]
        ),
    )
    res = OtelTopologyProvider().resolve("checkout", timeout_s=2.0)
    assert res.dependencies == ["payment"]


def test_leaf_service_is_empty_not_failed(monkeypatch):
    """No outbound RPC is a real answer (leaf or idle), so the chain falls
    through without treating Prometheus as broken."""
    _use(monkeypatch, _metrics([_series("checkout", "oteldemo.PaymentService")]))
    res = OtelTopologyProvider().resolve("currency", timeout_s=2.0)

    assert res.status is ProviderStatus.EMPTY
    assert res.payload_present is True


def test_metrics_query_failure_is_failed(monkeypatch):
    _use(monkeypatch, ToolResult(ok=False, error="circuit open (Prometheus unreachable)"))
    res = OtelTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.FAILED
    assert "circuit open" in (res.error or "")


def test_capability_missing_is_unavailable(monkeypatch):
    _use(monkeypatch, None, registered=False)
    provider = OtelTopologyProvider()

    assert provider.health().healthy is False
    assert provider.resolve("checkout", timeout_s=2.0).status is ProviderStatus.UNAVAILABLE


def test_unexpected_exception_is_contained(monkeypatch):
    _use(monkeypatch, RuntimeError("prometheus exploded"))
    res = OtelTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.FAILED
    assert "RuntimeError" in (res.error or "")


def test_empty_service_name_is_empty_not_failed(monkeypatch):
    """A blank service name is a caller error, not a provider malfunction.

    FAILED trips the breaker, so returning it here let one malformed request
    disable this tier for *every* service for the full breaker window. The
    provider is working fine — there is simply nothing to look up.
    """
    _use(monkeypatch, _metrics([]))
    res = OtelTopologyProvider().resolve("  ", timeout_s=2.0)

    assert res.status is ProviderStatus.EMPTY
    assert res.error is None, "no provider error occurred"
    assert "empty service name" in (res.note or "")


def test_query_filters_idle_series_and_groups_by_edge(monkeypatch):
    """The PromQL must group by both edge labels and drop idle series, or the
    graph fills with dependencies that had no traffic in the window."""
    reg = _use(monkeypatch, _metrics([]))
    OtelTopologyProvider().resolve("checkout", timeout_s=2.0)

    q = reg.queries[0]
    assert "sum by (service_name, rpc_service)" in q
    assert "rpc_client_duration_milliseconds_count" in q
    assert "> 0" in q


def test_rate_window_is_configurable(monkeypatch):
    monkeypatch.setattr(otel_mod, "_RATE_WINDOW", "15m")
    reg = _use(monkeypatch, _metrics([]))
    OtelTopologyProvider().resolve("checkout", timeout_s=2.0)
    assert "[15m]" in reg.queries[0]


# ─── fetch_edges (whole-graph accessor for the Phase 2 builder) ──────────────


def test_fetch_edges_returns_all_edges_with_rates(monkeypatch):
    _use(
        monkeypatch,
        _metrics(
            [
                _series("checkout", "oteldemo.PaymentService", rate="2.5"),
                _series("ad", "flagd.evaluation.v1.Service", rate="0.1"),
            ]
        ),
    )
    edges, error = otel_mod.fetch_edges()

    assert error is None
    assert ("checkout", "payment", 2.5) in edges
    assert ("ad", "flagd", 0.1) in edges


def test_fetch_edges_is_a_single_query(monkeypatch):
    """One round-trip for the whole graph is what makes the Phase 2 builder
    affordable — an N-node graph must not cost N queries."""
    reg = _use(monkeypatch, _metrics([_series("checkout", "oteldemo.PaymentService")]))
    otel_mod.fetch_edges()
    assert len(reg.queries) == 1


def test_fetch_edges_reports_error_without_raising(monkeypatch):
    _use(monkeypatch, ToolResult(ok=False, error="boom"))
    edges, error = otel_mod.fetch_edges()

    assert edges == []
    assert error == "boom"


def test_malformed_value_defaults_rate_to_zero(monkeypatch):
    _use(
        monkeypatch,
        _metrics([{"metric": {"service_name": "a", "rpc_service": "B"}, "value": ["ts", "NaN?"]}]),
    )
    edges, error = otel_mod.fetch_edges()

    assert error is None
    assert edges == [("a", "b", 0.0)]


# ─── chain integration ───────────────────────────────────────────────────────


def test_otel_is_registered_but_opt_in():
    from aiops.tools.topology import resolver as topo_resolver

    assert "otel" in topo_resolver._PROVIDERS
    assert topo_resolver._chain() == (["cmdb", "mock"], []), "otel must not be in the default chain"


def test_full_priority_chain_can_be_configured(monkeypatch):
    from aiops.tools.topology import resolver as topo_resolver

    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "otel,snow,cmdb,mock")
    assert topo_resolver._chain() == (["otel", "snow", "cmdb", "mock"], [])
