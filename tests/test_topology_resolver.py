"""Unit tests for the pluggable topology resolution chain.

The resolver owns every cross-cutting concern for topology discovery — chain
order, caching, per-provider health, circuit breaking, and a total latency
budget — so these tests are the only thing standing between a provider bug and
a wrong ``suspected_dependencies`` in an RA-007 evidence pack (which feeds the
RCA agent). The golden evals cannot cover this: ``log_correlation.run()`` forces
``force_synthetic=True``, so the eval path never resolves topology at all.

The most important behavioural distinction under test is ``ProviderStatus``:
``EMPTY`` ("I asked, the answer is genuinely nothing") must fall through to the
next tier **without** tripping a breaker, while ``FAILED`` ("I could not ask")
must trip it. On a stock ServiceNow PDI the demo services have no CI records, so
``EMPTY`` is the steady state for that tier — conflating the two would open
breakers during healthy operation.
"""

from __future__ import annotations

import pytest

from aiops.tools import topology
from aiops.tools.topology import cache as topo_cache
from aiops.tools.topology import resolver as topo_resolver
from aiops.tools.topology.base import HealthStatus, ProviderStatus, TopologyResult


class _FakeProvider:
    """Scriptable provider that records how often it was actually consulted.

    ``calls`` is the assertion hook for caching and breaking: those features are
    only meaningful if they *prevent* a lookup, which a returned value alone
    cannot demonstrate.
    """

    def __init__(
        self,
        name: str,
        *,
        result: TopologyResult | None = None,
        healthy: bool = True,
        raises: Exception | None = None,
        health_raises: Exception | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.name = name
        self._result = result
        self._healthy = healthy
        self._raises = raises
        self._health_raises = health_raises
        self._sleep_s = sleep_s
        self.calls = 0
        self.health_calls = 0
        self.last_timeout: float | None = None

    def health(self) -> HealthStatus:
        self.health_calls += 1
        if self._health_raises is not None:
            raise self._health_raises
        return HealthStatus(healthy=self._healthy, detail=f"{self.name} healthy={self._healthy}")

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        self.calls += 1
        self.last_timeout = timeout_s
        if self._raises is not None:
            raise self._raises
        if self._sleep_s:
            # Advance the faked clock rather than really sleeping, so budget
            # tests stay instant and deterministic.
            topo_resolver.time.monotonic()  # touch, for symmetry with real code
        return self._result or TopologyResult(provider=self.name, status=ProviderStatus.EMPTY)


def _resolved(name: str, deps: list[str]) -> TopologyResult:
    return TopologyResult(provider=name, status=ProviderStatus.RESOLVED, dependencies=deps)


def _empty(name: str, *, payload_present: bool = False) -> TopologyResult:
    return TopologyResult(
        provider=name, status=ProviderStatus.EMPTY, payload_present=payload_present
    )


def _failed(name: str, error: str = "boom") -> TopologyResult:
    return TopologyResult(provider=name, status=ProviderStatus.FAILED, error=error)


@pytest.fixture
def providers(monkeypatch):
    """Give each test an isolated provider table.

    ``_PROVIDERS`` is module-global; without snapshot/restore a fake registered
    by one test would stay visible to every later test in the session (the same
    process-global leak class the conftest breaker fixtures exist to fix).
    """
    original = dict(topo_resolver._PROVIDERS)
    topo_resolver._PROVIDERS.clear()
    try:
        yield topo_resolver._PROVIDERS
    finally:
        topo_resolver._PROVIDERS.clear()
        topo_resolver._PROVIDERS.update(original)


@pytest.fixture
def fake_clock(monkeypatch):
    """Freeze monotonic time in both the resolver and the cache.

    Both modules call ``time.monotonic`` independently, so patching only one
    would let TTL expiry and breaker windows disagree about "now".
    """
    state = {"now": 1000.0}
    monkeypatch.setattr(topo_resolver.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(topo_cache.time, "monotonic", lambda: state["now"])
    return state


# ─── chain order and fall-through ────────────────────────────────────────────


def test_default_chain_is_cmdb_then_mock():
    """Unset env must yield the behaviour-preserving default, not an empty chain."""
    assert topo_resolver._chain() == (["cmdb", "mock"], [])


def test_first_resolving_provider_wins_and_stops_the_chain(providers, monkeypatch):
    first = _FakeProvider("first", result=_resolved("first", ["payment"]))
    second = _FakeProvider("second", result=_resolved("second", ["should-not-be-used"]))
    providers["first"] = first
    providers["second"] = second
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "first,second")

    res = topology.resolve("checkout")

    assert res.dependencies == ["payment"]
    assert res.winning_provider == "first"
    assert second.calls == 0, "a resolved higher tier must short-circuit the rest of the chain"


def test_empty_provider_falls_through_to_next_tier(providers, monkeypatch):
    empty = _FakeProvider("empty", result=_empty("empty"))
    backup = _FakeProvider("backup", result=_resolved("backup", ["cart"]))
    providers["empty"] = empty
    providers["backup"] = backup
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "empty,backup")

    res = topology.resolve("checkout")

    assert res.dependencies == ["cart"]
    assert res.winning_provider == "backup"
    assert empty.calls == 1, "the empty tier must still have been consulted"


def test_all_tiers_empty_yields_empty_resolution(providers, monkeypatch):
    providers["a"] = _FakeProvider("a", result=_empty("a"))
    providers["b"] = _FakeProvider("b", result=_empty("b"))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "a,b")

    res = topology.resolve("checkout")

    assert res.dependencies == []
    assert res.winning_provider is None
    assert res.resolved is False
    assert [a.status for a in res.attempts] == [ProviderStatus.EMPTY, ProviderStatus.EMPTY]


def test_resolved_but_empty_dependency_list_is_not_treated_as_resolved(providers, monkeypatch):
    """A provider claiming RESOLVED with no dependencies must not stop the chain.

    Guards the ``resolved`` property: status alone is not enough, because a
    buggy provider that returns RESOLVED with an empty list would otherwise
    silently suppress every lower tier.
    """
    liar = _FakeProvider("liar", result=TopologyResult("liar", ProviderStatus.RESOLVED, []))
    backup = _FakeProvider("backup", result=_resolved("backup", ["payment"]))
    providers["liar"] = liar
    providers["backup"] = backup
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "liar,backup")

    res = topology.resolve("checkout")
    assert res.dependencies == ["payment"]


def test_unknown_provider_name_is_skipped_not_fatal(providers, monkeypatch):
    """A typo in AIOPS_TOPOLOGY_PROVIDERS must not disable topology entirely."""
    providers["mock"] = _FakeProvider("mock", result=_resolved("mock", ["cart"]))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "bogus,mock")

    res = topology.resolve("checkout")
    assert res.dependencies == ["cart"]


def test_chain_of_only_unknown_names_records_them_as_unavailable(providers, monkeypatch):
    """An all-typo chain resolves nothing, and says so rather than saying nothing.

    This used to assert ``attempts == []``, which is the silent-drop behaviour: a
    caller could not distinguish "no provider was configured" from "providers were
    configured and found nothing". RA-007 renders attempts into an operator-facing
    trace, so the name has to survive.
    """
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "only-bogus-names")
    res = topology.resolve("checkout")

    assert res.dependencies == []
    assert [a.provider for a in res.attempts] == ["only-bogus-names"]
    assert res.attempts[0].status is ProviderStatus.UNAVAILABLE
    assert "unknown provider" in (res.attempts[0].note or "")


# ─── circuit breaker: FAILED trips, EMPTY/UNAVAILABLE do not ─────────────────


def test_failed_provider_trips_breaker_and_is_skipped_next_call(providers, monkeypatch):
    flaky = _FakeProvider("flaky", result=_failed("flaky"))
    providers["flaky"] = flaky
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "flaky")

    topology.resolve("checkout")
    assert flaky.calls == 1

    # Different service so the result cache cannot be what suppresses the call.
    topology.resolve("cart")
    assert flaky.calls == 1, "breaker must short-circuit the second lookup"


def test_empty_status_does_not_trip_breaker(providers, monkeypatch):
    """Decision-3 guard: 'queried OK, zero relationships' is a legitimate answer.

    The ServiceNow tier returns this for every demo service, so treating it as a
    failure would keep that tier permanently breakered during normal operation.
    """
    quiet = _FakeProvider("quiet", result=_empty("quiet", payload_present=True))
    providers["quiet"] = quiet
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "quiet")

    topology.resolve("checkout")
    topology.resolve("cart")

    assert quiet.calls == 2, "EMPTY must NOT open the breaker"
    assert "quiet" not in topo_resolver._circuit_open_until


def test_unavailable_status_does_not_trip_breaker(providers, monkeypatch):
    """A provider that was never configured has not malfunctioned."""
    absent = _FakeProvider(
        "absent", result=TopologyResult("absent", ProviderStatus.UNAVAILABLE, note="no creds")
    )
    providers["absent"] = absent
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "absent")

    topology.resolve("checkout")
    topology.resolve("cart")

    assert absent.calls == 2
    assert "absent" not in topo_resolver._circuit_open_until


def test_breaker_reopens_after_window_elapses(providers, monkeypatch, fake_clock):
    flaky = _FakeProvider("flaky", result=_failed("flaky"))
    providers["flaky"] = flaky
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "flaky")

    topology.resolve("svc-a")
    assert flaky.calls == 1

    fake_clock["now"] += topo_resolver._CIRCUIT_OPEN_SECONDS - 1
    topology.resolve("svc-b")
    assert flaky.calls == 1, "still inside the breaker window"

    fake_clock["now"] += 2
    topology.resolve("svc-c")
    assert flaky.calls == 2, "must retry once the window elapses"


def test_breaker_is_per_provider(providers, monkeypatch):
    """One bad tier must not disable its siblings."""
    bad = _FakeProvider("bad", result=_failed("bad"))
    good = _FakeProvider("good", result=_resolved("good", ["cart"]))
    providers["bad"] = bad
    providers["good"] = good
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "bad,good")

    topology.resolve("svc-a")
    res = topology.resolve("svc-b")

    assert bad.calls == 1, "bad tier breakered"
    assert res.dependencies == ["cart"], "good tier still serving"


def test_provider_raising_is_contained_as_failed(providers, monkeypatch):
    """The Protocol says resolve() must not raise; if one does, the resolver must
    degrade that tier rather than propagate into correlate()."""
    rude = _FakeProvider("rude", raises=RuntimeError("provider exploded"))
    backup = _FakeProvider("backup", result=_resolved("backup", ["cart"]))
    providers["rude"] = rude
    providers["backup"] = backup
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "rude,backup")

    res = topology.resolve("checkout")

    assert res.dependencies == ["cart"]
    rude_attempt = next(a for a in res.attempts if a.provider == "rude")
    assert rude_attempt.status is ProviderStatus.FAILED
    assert "RuntimeError" in (rude_attempt.error or "")


# ─── health checks ───────────────────────────────────────────────────────────


def test_unhealthy_provider_is_skipped_without_being_queried(providers, monkeypatch):
    sick = _FakeProvider("sick", healthy=False, result=_resolved("sick", ["nope"]))
    backup = _FakeProvider("backup", result=_resolved("backup", ["cart"]))
    providers["sick"] = sick
    providers["backup"] = backup
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "sick,backup")

    res = topology.resolve("checkout")

    assert sick.calls == 0, "an unhealthy provider must not be queried"
    assert res.dependencies == ["cart"]


def test_health_check_raising_is_treated_as_unhealthy(providers, monkeypatch):
    sick = _FakeProvider("sick", health_raises=RuntimeError("probe failed"))
    providers["sick"] = sick
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "sick")

    res = topology.resolve("checkout")

    assert sick.calls == 0
    assert res.attempts[0].status is ProviderStatus.UNAVAILABLE


def test_health_is_cached_across_calls(providers, monkeypatch):
    p = _FakeProvider("p", result=_empty("p"))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")

    topology.resolve("svc-a")
    topology.resolve("svc-b")

    assert p.health_calls == 1, "health must be TTL-cached, not re-probed per lookup"


# ─── result caching ──────────────────────────────────────────────────────────


def test_result_is_cached_and_provider_not_requeried(providers, monkeypatch):
    p = _FakeProvider("p", result=_resolved("p", ["payment"]))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")

    first = topology.resolve("checkout")
    second = topology.resolve("checkout")

    assert p.calls == 1, "second identical lookup must be served from cache"
    assert second.dependencies == first.dependencies
    assert second.attempts[0].cached is True
    assert first.attempts[0].cached is False


def test_cache_is_keyed_per_service(providers, monkeypatch):
    p = _FakeProvider("p", result=_resolved("p", ["payment"]))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")

    topology.resolve("checkout")
    topology.resolve("cart")

    assert p.calls == 2, "different services must not share a cache entry"


def test_cache_key_is_case_and_whitespace_insensitive(providers, monkeypatch):
    p = _FakeProvider("p", result=_resolved("p", ["payment"]))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")

    topology.resolve("checkout")
    topology.resolve("  CheckOut ")

    assert p.calls == 1, "normalised keys must collapse onto one entry"


def test_cache_expires_after_ttl(providers, monkeypatch, fake_clock):
    p = _FakeProvider("p", result=_resolved("p", ["payment"]))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")

    topology.resolve("checkout")
    assert p.calls == 1

    fake_clock["now"] += topo_cache._RESOLVED_TTL + 1
    topology.resolve("checkout")
    assert p.calls == 2, "expired entry must trigger a fresh lookup"


def test_empty_result_uses_shorter_ttl_than_resolved():
    """EMPTY is re-checked more eagerly than a positive answer: a half-populated
    CMDB looks identical to a genuinely empty one, so we don't cache it as long."""
    assert topo_cache.ttl_for_status("empty") < topo_cache.ttl_for_status("resolved")
    assert topo_cache.ttl_for_status("failed") <= topo_cache.ttl_for_status("empty")


def test_zero_ttl_disables_caching(monkeypatch):
    topo_cache.put("k", "v", 0)
    assert topo_cache.get("k") is None


# ─── budget ──────────────────────────────────────────────────────────────────


def test_total_budget_stops_the_chain(providers, monkeypatch, fake_clock):
    """Per-provider timeouts bound each hop but not their sum; the budget caps
    the whole walk so topology can't stall an incident."""

    class _SlowProvider(_FakeProvider):
        def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
            fake_clock["now"] += 5.0  # burn more than the budget
            return super().resolve(service, timeout_s=timeout_s)

    slow = _SlowProvider("slow", result=_empty("slow"))
    never = _FakeProvider("never", result=_resolved("never", ["cart"]))
    providers["slow"] = slow
    providers["never"] = never
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "slow,never")

    res = topology.resolve("checkout")

    assert slow.calls == 1
    assert never.calls == 0, "budget must stop the walk before the next tier"
    assert res.budget_exhausted is True


def test_budget_exhaustion_still_consults_free_in_process_tiers(providers, monkeypatch, fake_clock):
    """A slow network tier must not starve the in-process fallbacks.

    Found live: with ``otel`` enabled and Prometheus refusing connections, the
    failing tier burned the whole 3s budget and the walk stopped — leaving
    ``dependencies=[]`` when the static table two tiers down had the answer all
    along. That is strictly worse than the single-CMDB-lookup behaviour this
    chain replaced, so free tiers stay eligible past the cut-off.
    """

    class _SlowProvider(_FakeProvider):
        def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
            fake_clock["now"] += 5.0  # blow the entire budget
            return super().resolve(service, timeout_s=timeout_s)

    slow = _SlowProvider("otel", result=_failed("otel"))
    providers["otel"] = slow
    providers["mock"] = _FakeProvider("mock", result=_resolved("mock", ["cart", "payment"]))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "otel,mock")

    res = topology.resolve("checkout")

    assert res.budget_exhausted is True, "the budget should still be reported as blown"
    assert res.dependencies == ["cart", "payment"], "the free tier must still have been consulted"
    assert res.winning_provider == "mock"


def test_budget_exhaustion_skips_free_tier_whose_breaker_is_open(
    providers, monkeypatch, fake_clock
):
    """The post-budget fallback still respects breakers — it is a latency
    exemption, not a correctness exemption.

    Getting this test to actually reach the fallback took three attempts, so the
    mechanics are worth spelling out. ``budget_exhausted`` is assigned on the only
    statement guarding the fallback loop, so it doubles as proof the loop ran — and
    both earlier versions of this test had it ``False``, meaning they asserted
    breaker behaviour from the *ordinary* walk and would have passed with the
    fallback's breaker handling deleted entirely.

    The trap: once ``otel``'s breaker is open, ``_run_provider`` short-circuits
    before the slow provider can advance the fake clock, so the budget is never
    spent and the walk never enters the fallback. So this trips only ``mock``'s
    breaker and leaves ``otel`` free to burn the budget on every call.
    """

    class _SlowProvider(_FakeProvider):
        def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
            fake_clock["now"] += 5.0
            return super().resolve(service, timeout_s=timeout_s)

    otel = _SlowProvider("otel", result=_empty("otel"))
    mock = _FakeProvider("mock", result=_failed("mock"))
    providers["otel"] = otel
    providers["mock"] = mock
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "otel,mock")

    # First walk: otel burns the budget (EMPTY, so its own breaker stays closed),
    # the fallback consults mock, mock FAILS and its breaker trips.
    first = topology.resolve("svc-a")
    assert first.budget_exhausted is True, "precondition: the fallback ran"
    assert topo_resolver._breaker_open("mock") is True, "precondition: mock is broken open"

    # Drop the cached FAILED result so the breaker — not the cache — is what gates
    # mock on the next walk.
    topo_cache.clear()
    fake_clock["now"] += 1
    calls_before = mock.calls
    res = topology.resolve("svc-b")

    assert res.budget_exhausted is True, "the fallback loop must have been entered"
    assert res.dependencies == []
    assert res.attempts[-1].provider == "mock"
    assert res.attempts[-1].status is ProviderStatus.UNAVAILABLE
    assert res.attempts[-1].note == "mock circuit open"
    assert mock.calls == calls_before, "the fallback must not call a broken-open tier"


def test_unknown_name_does_not_displace_a_higher_priority_tier(providers, monkeypatch):
    """A typo in slot 2 must not occupy slot 0 of ``attempts``.

    The companion test puts its typo first, which makes the assertion hold for the
    degenerate case only — an all-unknown pre-pass passes it just as happily. This is
    the case that actually pins interleaving: RA-007 reports ``attempts[0]``, so
    hoisting unknown names to the front made the trace describe a typo while the
    primary tier that genuinely answered was masked.
    """
    providers["cmdb"] = _FakeProvider("cmdb", result=_empty("cmdb", payload_present=True))
    providers["mock"] = _FakeProvider("mock", result=_empty("mock"))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "cmdb,bogus,mock")

    res = topology.resolve("svc")

    assert [a.provider for a in res.attempts] == ["cmdb", "bogus", "mock"]
    assert res.attempts[0].provider == "cmdb", "the configured primary owns slot 0"
    assert res.attempts[1].status is ProviderStatus.UNAVAILABLE
    assert "unknown provider" in (res.attempts[1].note or "")


def test_provider_timeout_is_capped_by_remaining_budget(providers, monkeypatch, fake_clock):
    p = _FakeProvider("p", result=_empty("p"))
    providers["p"] = p
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "p")
    monkeypatch.setattr(topo_resolver, "_PER_PROVIDER_TIMEOUT", 99.0)
    monkeypatch.setattr(topo_resolver, "_TOTAL_BUDGET", 1.5)

    topology.resolve("checkout")

    assert p.last_timeout is not None
    assert p.last_timeout <= 1.5, "a provider must not be handed a deadline beyond the budget"


# ─── test seam ───────────────────────────────────────────────────────────────


def test_reset_for_tests_clears_cache_and_breakers(providers, monkeypatch):
    flaky = _FakeProvider("flaky", result=_failed("flaky"))
    providers["flaky"] = flaky
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "flaky")

    topology.resolve("checkout")
    assert topo_resolver._circuit_open_until, "breaker should be armed"

    topology.reset_for_tests()

    assert topo_resolver._circuit_open_until == {}
    assert topo_cache.get("deps:flaky:checkout") is None


def test_register_provider_replaces_by_name(providers):
    first = _FakeProvider("dup", result=_resolved("dup", ["a"]))
    second = _FakeProvider("dup", result=_resolved("dup", ["b"]))
    topology.register_provider(first)
    topology.register_provider(second)

    assert topo_resolver._PROVIDERS["dup"] is second, "re-registration must replace, not raise"


# ─── the shipped providers ───────────────────────────────────────────────────


def test_mock_provider_resolves_known_service():
    from aiops.tools.topology.providers.mock import MockTopologyProvider

    res = MockTopologyProvider().resolve("checkout", timeout_s=1.0)
    assert res.status is ProviderStatus.RESOLVED
    assert "payment" in res.dependencies


def test_mock_provider_reports_empty_for_unknown_service():
    from aiops.tools.topology.providers.mock import MockTopologyProvider

    res = MockTopologyProvider().resolve("no-such-service-xyz", timeout_s=1.0)
    assert res.status is ProviderStatus.EMPTY
    assert res.dependencies == []


def test_cmdb_provider_marks_payload_present_for_zero_dep_service():
    """``payload_present`` is what lets RA-007 keep saying "0 downstream dep(s)
    from cmdb" for a known-but-standalone service, instead of the different
    "cmdb returned no dependencies" line that means "no record at all"."""
    from aiops.tools.topology.providers.cmdb import CmdbTopologyProvider

    res = CmdbTopologyProvider().resolve("ad", timeout_s=1.0)
    assert res.status is ProviderStatus.EMPTY
    assert res.payload_present is True


def test_cmdb_provider_unavailable_when_capability_missing(monkeypatch):
    from aiops.tools.topology.providers import cmdb as cmdb_mod

    class _EmptyRegistry:
        def by_capability(self, capability):
            raise KeyError(capability)

        def call(self, capability, **kwargs):
            raise KeyError(capability)

    monkeypatch.setattr(cmdb_mod, "get_registry", lambda: _EmptyRegistry())

    provider = cmdb_mod.CmdbTopologyProvider()
    assert provider.health().healthy is False
    res = provider.resolve("checkout", timeout_s=1.0)
    assert res.status is ProviderStatus.UNAVAILABLE


def test_provider_status_is_a_plain_string():
    """Serializes straight into a decision trace / ToolResult.metadata."""
    assert ProviderStatus.EMPTY == "empty"
    assert f"{ProviderStatus.RESOLVED}" == "resolved"


# ─── breaker vs cache ordering (PR #235 review, blocking #2) ──────────────────


def test_tripped_breaker_still_serves_a_fresh_cache_entry(providers, monkeypatch):
    """A tripped breaker means "stop calling this provider", not "forget it".

    The chain walk used to check the breaker *before* _run_provider's cache
    lookup, so one failure for service B short-circuited every later lookup on
    that tier — including service A, whose fresh cached answer was sitting right
    there — forcing a fallthrough to a lower-confidence tier for the full
    _CIRCUIT_OPEN_SECONDS window.

    Both halves matter, so both are asserted: the cache must still answer, and the
    breaker must still prevent a live call.
    """

    class _PerService:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def health(self) -> HealthStatus:
            return HealthStatus(healthy=True, detail="ok")

        def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
            self.calls += 1
            if service == "b":
                return _failed(self.name, "backend down")
            return _resolved(self.name, ["dep-of-a"])

    provider = _PerService()
    providers["flaky"] = provider
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "flaky")

    assert topology.resolve("a").dependencies == ["dep-of-a"]

    # Trip the breaker on an unrelated service.
    topology.resolve("b")
    assert topo_resolver._breaker_open("flaky") is True

    calls_before = provider.calls
    again = topology.resolve("a")

    assert again.dependencies == ["dep-of-a"], "cached answer must survive a tripped breaker"
    assert again.attempts[0].cached is True
    assert provider.calls == calls_before, "breaker must still prevent a live call"


def test_tripped_breaker_without_cache_reports_circuit_open(providers, monkeypatch):
    """The breaker still short-circuits when there is nothing cached to serve —
    moving the check after the cache must not disable it.

    The note is asserted in full because RA-007 renders it verbatim into an
    operator-facing decision_trace line; a bare "circuit open" there named neither
    the tier nor the cause.
    """
    provider = _FakeProvider("flaky", result=_failed("flaky"))
    providers["flaky"] = provider
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "flaky")

    topology.resolve("b")
    assert topo_resolver._breaker_open("flaky") is True

    topo_cache.clear()
    calls_before = provider.calls
    res = topology.resolve("c")

    assert provider.calls == calls_before, "breaker must prevent the live call"
    assert res.attempts[-1].status is ProviderStatus.UNAVAILABLE
    assert res.attempts[-1].note == "flaky circuit open"


def test_attempts_are_recorded_in_chain_priority_order(providers, monkeypatch):
    """``attempts[0]`` must be the highest-priority configured tier.

    RA-007's decision_trace attribution depends on this cross-module invariant, and
    nothing pinned it — every trace test stubs ``topology_resolve``, so the real
    resolver was never exercised for ordering. A locally reasonable resolver change
    (appending the budget fallback first, skipping an unhealthy tier's attempt
    entirely) would silently make the agent attribute an outcome to the wrong tier.

    Both the unhealthy and breaker-open paths are covered, since those are the two
    that previously produced no attempt at all.
    """
    providers["top"] = _FakeProvider("top", healthy=False)
    providers["mid"] = _FakeProvider("mid", result=_failed("mid"))
    providers["bottom"] = _FakeProvider("bottom", result=_resolved("bottom", ["dep"]))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "top,mid,bottom")

    res = topology.resolve("svc")

    assert [a.provider for a in res.attempts] == ["top", "mid", "bottom"]
    assert res.attempts[0].status is ProviderStatus.UNAVAILABLE
    assert res.winning_provider == "bottom"


def test_unknown_provider_name_occupies_its_priority_slot(providers, monkeypatch):
    """A typo must not silently promote a lower tier into the attempts[0] slot.

    Otherwise RA-007 attributes the outcome to a tier the operator never configured
    as primary, with nothing recording that the intended one was never tried.
    """
    providers["mock"] = _FakeProvider("mock", result=_resolved("mock", ["dep"]))
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "cmdbb,mock")

    res = topology.resolve("svc")

    assert [a.provider for a in res.attempts] == ["cmdbb", "mock"]
    assert res.attempts[0].status is ProviderStatus.UNAVAILABLE
    assert "unknown provider" in (res.attempts[0].note or "")
    assert res.dependencies == ["dep"], "the rest of the chain still answers"


def test_budget_exhaustion_still_serves_a_cached_non_free_tier(providers, monkeypatch):
    """The budget fallback must consult any tier that can answer from cache.

    Gating solely on _FREE_PROVIDERS discarded a fresh cached answer from a remote
    tier and fell through to a lower-confidence one — the same bug class as checking
    the breaker before the cache, one level up. Serving a cache entry costs nothing
    and cannot blow the budget.
    """
    remote = _FakeProvider("remote", result=_resolved("remote", ["cached-dep"]))
    providers["remote"] = remote
    monkeypatch.setenv("AIOPS_TOPOLOGY_PROVIDERS", "remote")
    assert "remote" not in topo_resolver._FREE_PROVIDERS

    # Warm the cache, then force the budget to be spent before the walk starts.
    assert topology.resolve("svc").dependencies == ["cached-dep"]
    calls_before = remote.calls
    monkeypatch.setattr(topo_resolver, "_TOTAL_BUDGET", 0.0)

    res = topology.resolve("svc")

    assert res.budget_exhausted is True
    assert res.dependencies == ["cached-dep"], "a cached remote tier must still answer"
    assert remote.calls == calls_before, "no live call — the answer came from cache"
