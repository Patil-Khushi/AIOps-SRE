"""Tests for the ServiceNow CI-relationship topology tier and its backing tool.

Two layers:

- ``cmdb_relationships`` in ``aiops.tools.itsm.servicenow`` — the Table API
  traversal (CI lookup then ``cmdb_rel_ci``).
- ``SnowTopologyProvider`` — mapping that tool's ToolResult onto the chain's
  four-way ``ProviderStatus``.

The mapping is what these tests mostly protect, because getting it wrong is
silent and expensive: a probe of the configured PDI found **zero**
``cmdb_ci_service`` rows for the demo services, so "queried fine, nothing found"
is the *normal* result here. If that were classified ``FAILED``, this tier's
circuit breaker would sit open permanently while nothing was actually broken —
and the decision trace would blame ServiceNow for an outage that isn't one.

The other hazard under test is phantom dependencies. ``cmdb_lookup`` resolves CIs
with ``name=X^ORnameLIKEX``; inheriting that LIKE here would let "ad" match
"admin" and hang that CI's entire relationship set onto the wrong service,
feeding a wrong suspect into the RCA agent. CI resolution must be exact.
"""

from __future__ import annotations

import pytest

from aiops.tools.itsm import servicenow as sn
from aiops.tools.registry import ToolResult
from aiops.tools.topology.base import ProviderStatus
from aiops.tools.topology.providers import snow as snow_mod
from aiops.tools.topology.providers.snow import SnowTopologyProvider


class _FakeRegistry:
    def __init__(self, result: ToolResult | None, *, registered: bool = True) -> None:
        self._result = result
        self._registered = registered
        self.calls: list[dict] = []

    def by_capability(self, capability: str):
        if not self._registered:
            raise KeyError(capability)
        return object()

    def call(self, capability: str, **kwargs):
        if not self._registered:
            raise KeyError(capability)
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _use(monkeypatch, result, *, registered: bool = True) -> _FakeRegistry:
    reg = _FakeRegistry(result, registered=registered)
    monkeypatch.setattr(snow_mod, "get_registry", lambda: reg)
    return reg


# ─── status mapping (the part that governs breaker behaviour) ────────────────


def test_real_relationships_resolve(monkeypatch):
    _use(
        monkeypatch,
        ToolResult(
            ok=True,
            data={"service": "checkout", "dependencies": ["payment", "cart"]},
            metadata={"reason": "resolved"},
        ),
    )
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.RESOLVED
    assert res.dependencies == ["payment", "cart"]
    assert res.resolved is True


def test_service_absent_from_cmdb_is_empty_not_failed(monkeypatch):
    """The demo's normal case: query succeeded, service simply has no CI record."""
    _use(
        monkeypatch,
        ToolResult(
            ok=True,
            data={"service": "checkout", "dependencies": []},
            metadata={"reason": "no_ci_record", "matched": False},
        ),
    )
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.EMPTY, "must not be FAILED — nothing malfunctioned"
    assert res.payload_present is False, "no CI record means no payload to speak of"
    assert res.note == "no_ci_record"


def test_ci_without_relationships_is_empty_with_payload(monkeypatch):
    """Distinct from 'no CI record': the CI exists but has no relationships.

    Different CMDB-hygiene problem, so the two stay distinguishable in the trace.
    """
    _use(
        monkeypatch,
        ToolResult(
            ok=True,
            data={"service": "checkout", "dependencies": []},
            metadata={"reason": "ci_without_relationships", "matched": True},
        ),
    )
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.EMPTY
    assert res.payload_present is True


def test_query_failure_is_failed_so_the_breaker_trips(monkeypatch):
    _use(monkeypatch, ToolResult(ok=False, error="HTTPError: 503"))
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.FAILED
    assert "503" in (res.error or "")


def test_unconfigured_servicenow_is_unavailable_not_failed(monkeypatch):
    """Missing credentials is a configuration state, not an outage — classifying
    it FAILED would breaker the tier on every fresh checkout of the repo."""
    _use(
        monkeypatch,
        ToolResult(
            ok=False, error="ServiceNow not configured: ...", metadata={"configured": False}
        ),
    )
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.UNAVAILABLE
    assert "not configured" in (res.note or "")


def test_capability_not_registered_is_unavailable(monkeypatch):
    _use(monkeypatch, None, registered=False)
    provider = SnowTopologyProvider()

    assert provider.health().healthy is False
    res = provider.resolve("checkout", timeout_s=2.0)
    assert res.status is ProviderStatus.UNAVAILABLE


def test_unexpected_exception_is_contained_as_failed(monkeypatch):
    _use(monkeypatch, RuntimeError("kaboom"))
    res = SnowTopologyProvider().resolve("checkout", timeout_s=2.0)

    assert res.status is ProviderStatus.FAILED
    assert "RuntimeError" in (res.error or "")


def test_health_is_true_when_capability_registered(monkeypatch):
    _use(monkeypatch, ToolResult(ok=True, data={"dependencies": []}))
    assert SnowTopologyProvider().health().healthy is True


# ─── the tool itself: exact CI matching + relationship traversal ─────────────


@pytest.fixture(autouse=True)
def _snow_env(monkeypatch):
    monkeypatch.setenv("AIOPS_SERVICENOW_INSTANCE_URL", "https://example.service-now.com")
    monkeypatch.setenv("AIOPS_SERVICENOW_USER", "u")
    monkeypatch.setenv("AIOPS_SERVICENOW_PASSWORD", "p")
    for var in (
        "AIOPS_SERVICENOW_OAUTH_CLIENT_ID",
        "AIOPS_SERVICENOW_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    sn._reset_oauth_cache_for_tests()


def test_ci_lookup_uses_exact_match_never_like(monkeypatch):
    """Regression guard for the phantom-dependency hazard.

    ``cmdb_lookup`` uses ``name=X^ORnameLIKEX``. If that leaked into relationship
    traversal, a short name like "ad" would match "admin" and every relationship
    on the wrong CI would surface as a dependency of the wrong service.
    """
    queries: list[str] = []

    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        queries.append((params or {}).get("sysparm_query", ""))
        return ToolResult(ok=True, data={"result": []})

    monkeypatch.setattr(sn, "_request", _fake_request)
    sn.cmdb_relationships("ad")

    assert queries, "a CI lookup should have been attempted"
    assert queries[0] == "name=ad"
    assert "LIKE" not in queries[0], "relationship traversal must not use a LIKE match"


def test_no_ci_row_returns_ok_with_empty_dependencies(monkeypatch):
    monkeypatch.setattr(sn, "_request", lambda *a, **kw: ToolResult(ok=True, data={"result": []}))
    res = sn.cmdb_relationships("checkout")

    assert res.ok is True, "a successful query that found nothing is not an error"
    assert res.data["dependencies"] == []
    assert res.metadata["reason"] == "no_ci_record"


def test_relationships_are_extracted_and_deduplicated(monkeypatch):
    calls = {"n": 0}

    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        calls["n"] += 1
        if "cmdb_ci_service" in path:
            return ToolResult(ok=True, data={"result": [{"sys_id": "abc123", "name": "checkout"}]})
        return ToolResult(
            ok=True,
            data={
                "result": [
                    {"parent": {"display_value": "payment"}},
                    {"parent": {"display_value": "cart"}},
                    {"parent": {"display_value": "payment"}},  # duplicate
                    {"parent": None},  # unresolvable -> dropped
                ]
            },
        )

    monkeypatch.setattr(sn, "_request", _fake_request)
    res = sn.cmdb_relationships("checkout")

    assert res.ok is True
    assert res.data["dependencies"] == ["payment", "cart"], "dedup, order-preserving"
    assert res.metadata["reason"] == "resolved"


def test_unresolvable_relationship_rows_are_dropped_not_placeholdered(monkeypatch):
    """A dependency we cannot name is not usable evidence; an empty string would
    otherwise enter the graph as a real node."""

    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        if "cmdb_ci_service" in path:
            return ToolResult(ok=True, data={"result": [{"sys_id": "abc", "name": "x"}]})
        return ToolResult(ok=True, data={"result": [{"parent": None}, {"parent": ""}]})

    monkeypatch.setattr(sn, "_request", _fake_request)
    res = sn.cmdb_relationships("x")

    assert res.data["dependencies"] == []
    assert res.metadata["reason"] == "ci_without_relationships"


def test_ci_lookup_failure_propagates_as_not_ok(monkeypatch):
    monkeypatch.setattr(
        sn, "_request", lambda *a, **kw: ToolResult(ok=False, error="HTTPError: timeout")
    )
    res = sn.cmdb_relationships("checkout")

    assert res.ok is False
    assert res.metadata["stage"] == "ci_lookup"


def test_relationship_query_failure_propagates_as_not_ok(monkeypatch):
    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        if "cmdb_ci_service" in path:
            return ToolResult(ok=True, data={"result": [{"sys_id": "abc", "name": "x"}]})
        return ToolResult(ok=False, error="HTTPError: 500")

    monkeypatch.setattr(sn, "_request", _fake_request)
    res = sn.cmdb_relationships("x")

    assert res.ok is False
    assert res.metadata["stage"] == "rel_query"


def test_blank_service_is_rejected():
    res = sn.cmdb_relationships("   ")
    assert res.ok is False


def test_topology_queries_use_the_short_timeout_not_the_15s_default(monkeypatch):
    """Topology must fail fast; incident creation must not.

    Measured live: a CI lookup for a service with no record took ~1.9s, and a
    hibernating PDI can take far longer. Topology resolves *before* RA-007's
    logs/traces/metrics fan-out, and the resolver's total budget only gates
    between tiers — it cannot interrupt an in-flight call. So both CMDB queries
    must carry the tighter deadline while the ticketing paths keep the generous
    one.
    """
    seen: list[float | None] = []

    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        seen.append(timeout_override)
        if "cmdb_ci_service" in path:
            return ToolResult(ok=True, data={"result": [{"sys_id": "abc", "name": "checkout"}]})
        return ToolResult(ok=True, data={"result": []})

    monkeypatch.setattr(sn, "_request", _fake_request)
    sn.cmdb_relationships("checkout")

    assert len(seen) == 2, "both the CI lookup and the relationship query should run"
    assert all(t == sn._TOPOLOGY_TIMEOUT for t in seen), f"expected short timeout, got {seen}"
    assert sn._TOPOLOGY_TIMEOUT < 15, "must be tighter than the ticketing default"


def test_incident_paths_keep_the_generous_default_timeout(monkeypatch):
    """The tighter deadline must not leak onto ticket creation, where waiting for
    a waking PDI is the correct behaviour."""
    seen: list[float | None] = []

    def _fake_request(method, path, *, timeout_override=None, params=None, json=None):
        seen.append(timeout_override)
        return ToolResult(ok=True, data={"result": []})

    monkeypatch.setattr(sn, "_request", _fake_request)
    sn.cmdb_lookup("checkout")

    assert seen == [None], "cmdb_lookup must not pass a topology override"


# ─── chain integration ───────────────────────────────────────────────────────


def test_snow_is_registered_but_not_in_the_default_chain():
    """Available != in the chain. Registering the tier must not add a ServiceNow
    round-trip to the default path, or every correlate() would hit the PDI."""
    from aiops.tools.topology import resolver as topo_resolver

    assert "snow" in topo_resolver._PROVIDERS
    assert topo_resolver._chain() == (["cmdb", "mock"], []), "snow must be opt-in"


def test_snow_can_be_enabled_via_env(monkeypatch):
    from aiops.tools.topology import resolver as topo_resolver

    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "snow,cmdb,mock")
    assert topo_resolver._chain() == (["snow", "cmdb", "mock"], [])
