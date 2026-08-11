"""Tests for stage 1 of the context pipeline — the only stage that does I/O.

Two things here carry most of the weight.

**The emptiness predicates are pinned against the real provider payload shapes.**
``EMPTY`` and ``FAILED`` mean opposite things to a consumer: RCA renders
"NONE — this signal was checked and was absent" for an empty category and instructs
the model to treat it as positive evidence *against* any cause that would have
produced that signal. Mislabel an unreachable Prometheus as ``EMPTY`` and the model
is told a cause has been ruled out when nothing was ever checked. So every predicate
is tested against the shape its provider actually returns, taken from
``aiops/tools/observability/*.py``, rather than against a shape we assumed.

**The denylist is tested for refusing at construction time.** A collector that only
failed on first use could sit in a chain through an entire test suite and first
surface in production.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest

from aiops.context.collectors import (
    CapabilityCollector,
    Collector,
    DeploymentsCollector,
    IncidentHistoryCollector,
    TopologyCollector,
    available_collectors,
    collector_for,
    resolve_chain,
)
from aiops.context.collectors.base import not_requested, unavailable
from aiops.context.denylist import (
    ContextDenylistError,
    ensure_allowed,
    is_denied,
)
from aiops.context.models import SectionSpec, SectionStatus, Source
from aiops.context.pack import ContextSection
from aiops.tools.registry import ToolResult

CORR = "corr-test-1"


class _StubRegistry:
    """Stands in for the tool registry, recording what was asked of it.

    A stub rather than a mock of ``httpx``: the contract a collector depends on is
    ``registry.call()``'s — never raises, reports everything as a ``ToolResult`` —
    and that is what these tests should be written against.
    """

    def __init__(self, result: ToolResult | Exception) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, capability: str, **kwargs: Any) -> ToolResult:
        self.calls.append((capability, kwargs))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def stub_registry(monkeypatch):
    """Install a stub registry into the collector module and hand back the installer."""

    def _install(result: ToolResult | Exception) -> _StubRegistry:
        registry = _StubRegistry(result)
        monkeypatch.setattr(
            "aiops.context.collectors.base.get_registry", lambda: registry, raising=True
        )
        return registry

    return _install


def _spec(source: Source = "metrics", **params: Any) -> SectionSpec:
    return SectionSpec(source=source, query_id="q1", params=params or {"promql": "up"})


# --- denylist ------------------------------------------------------------


def test_denylist_refuses_the_rca_grounding_probe():
    """``automation.fault.clear`` must never be collectable.

    The RCA agent calls it purely to read ``metadata["available_faults"]`` and ground
    an LLM's proposed fix against the fault names that actually exist. Cache a
    failure of it and grounding silently stops working for the whole TTL — nothing
    fails, the system just gets less correct, which is the worst failure mode there
    is.
    """
    assert is_denied("automation.fault.clear")
    with pytest.raises(ContextDenylistError) as exc:
        ensure_allowed("automation.fault.clear")
    assert exc.value.capability == "automation.fault.clear"


@pytest.mark.parametrize(
    "capability",
    [
        "automation.runbook.execute",
        "itsm.incident.create",
        "itsm.ticket.close",
        "knowledge.publish",
        "notify.send",
        "chatops.war_room.create",
        "rca.fix_step.execute",
        "feature_flags.list_variants",
        "feature_flags.set_variant",
    ],
)
def test_denylist_refuses_mutations_and_the_deleted_flag_seam(capability: str):
    assert is_denied(capability)


@pytest.mark.parametrize(
    "capability",
    [
        "observability.metrics.query",
        "observability.logs.query",
        "observability.traces.search",
        "itsm.cmdb.lookup",
        "oncall.schedule.lookup",
        "scm.commit.history",
    ],
)
def test_denylist_allows_every_read_the_layer_needs(capability: str):
    assert not is_denied(capability)
    ensure_allowed(capability)


def test_a_denied_collector_cannot_even_be_constructed():
    """Fails at construction, not on first use — see the module docstring."""
    with pytest.raises(ContextDenylistError):
        CapabilityCollector(
            name="nope",
            source="metrics",
            capability="automation.fault.clear",
        )


# --- registry / chain ---------------------------------------------------


def test_every_source_has_a_collector():
    """Total over ``Source``, so no section can silently have no way to be filled."""
    assert set(typing.get_args(Source)) == set(available_collectors())


def test_collectors_satisfy_the_protocol_structurally():
    """Composition, not inheritance — nothing here subclasses ``Collector``."""
    for collector in available_collectors().values():
        assert isinstance(collector, Collector)
        assert not isinstance(collector, type)


def test_collector_for_unknown_source_is_none():
    assert collector_for("does-not-exist") is None


def test_chain_defaults_to_every_collector(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_COLLECTORS", raising=False)
    known, unknown = resolve_chain()
    assert set(known) == set(available_collectors())
    assert unknown == ()


def test_chain_returns_unknown_names_rather_than_dropping_them(monkeypatch):
    """False completeness is worse than a visible gap.

    ``change_context/collector.py`` established this convention after a typo'd
    provider name produced a result that looked complete because the bad name was
    logged and discarded. A caller must be able to tell "not requested" from
    "that name does not exist".
    """
    monkeypatch.setenv("AIOPS_CONTEXT_COLLECTORS", "metrics, logz ,traces")
    known, unknown = resolve_chain()
    assert known == ("metrics", "traces")
    assert unknown == ("logz",)


# --- status mapping -----------------------------------------------------


def test_missing_provider_is_unavailable_not_failed(stub_registry):
    """A capability that was never configured has not malfunctioned."""
    stub_registry(ToolResult(ok=False, error="no provider", metadata={"missing_provider": True}))
    section = collector_for("metrics").collect(_spec(), CORR)

    assert section.status is SectionStatus.UNAVAILABLE
    assert section.provenance.coverage_note == "capability not registered"
    assert section.raw is None


def test_hitl_block_is_reported_as_unavailable_not_as_a_backend_failure(stub_registry):
    """Should never happen for a read, but must not masquerade as a broken backend."""
    stub_registry(ToolResult(ok=False, metadata={"blocked_by": "hitl_gate", "level": "required"}))
    section = collector_for("metrics").collect(_spec(), CORR)

    assert section.status is SectionStatus.UNAVAILABLE
    assert "HITL" in (section.provenance.coverage_note or "")


def test_provider_error_is_failed_and_keeps_the_error_verbatim(stub_registry):
    """Agent adapters reproduce decision-trace lines that embed this text, so a
    reworded error would change an operator-facing audit string."""
    stub_registry(ToolResult(ok=False, error="HTTPError: connection refused"))
    section = collector_for("metrics").collect(_spec(), CORR)

    assert section.status is SectionStatus.FAILED
    assert section.provenance.error == "HTTPError: connection refused"


def test_a_raising_provider_still_yields_a_section(stub_registry):
    """``collect()`` must not raise: a backend bug costs evidence, not a verdict."""
    stub_registry(RuntimeError("kaboom"))
    section = collector_for("metrics").collect(_spec(), CORR)

    assert section.status is SectionStatus.FAILED
    assert not section.status.usable


# --- emptiness predicates, per real provider payload -------------------
#
# Shapes taken from aiops/tools/observability/{prometheus,loki,jaeger}.py and
# aiops/tools/mock_providers.py. If a provider changes its schema, these fail.

_EMPTY_CASES: list[tuple[Source, dict[str, Any], dict[str, Any]]] = [
    # source, empty payload, non-empty payload
    (
        "metrics",
        {"query": "up", "result_type": "vector", "results": []},
        {
            "query": "up",
            "result_type": "vector",
            "results": [{"metric": {"pod": "p"}, "value": [1.0, "1"]}],
        },
    ),
    (
        "logs",
        {"streams": []},
        {"streams": [{"stream": {"level": "error"}, "values": [[1, "boom"]]}]},
    ),
    (
        "traces",
        {"service": "s", "lookback": "15m", "trace_count": 0, "traces": []},
        {
            "service": "s",
            "lookback": "15m",
            "trace_count": 1,
            "traces": [{"trace_id": "t", "span_count": 3}],
        },
    ),
    (
        "dependencies",
        {"service": "s", "dependencies": []},
        {"service": "s", "dependencies": ["mysql"]},
    ),
    (
        "runbooks",
        {"service": "s", "category": "c", "resolvers": []},
        {"service": "s", "category": "c", "resolvers": [{"name": "ada"}]},
    ),
    (
        "k8s_events",
        {"events": [], "configmaps": []},
        {"events": [{"reason": "BackOff", "message": "m"}], "configmaps": []},
    ),
]


@pytest.mark.parametrize(
    ("source", "empty_payload", "full_payload"),
    _EMPTY_CASES,
    ids=[c[0] for c in _EMPTY_CASES],
)
def test_emptiness_predicate_matches_the_real_payload_shape(
    stub_registry, source, empty_payload, full_payload
):
    stub_registry(ToolResult(ok=True, data=empty_payload, metadata={"provider": "p"}))
    empty = collector_for(source).collect(_spec(source), CORR)
    assert empty.status is SectionStatus.EMPTY, f"{source}: empty payload misread"
    assert empty.status.usable, "EMPTY is a real answer and must stay usable"

    # A fresh correlation id, so this is a real second call rather than a cache hit.
    stub_registry(ToolResult(ok=True, data=full_payload, metadata={"provider": "p"}))
    full = collector_for(source).collect(_spec(source), f"{CORR}-2")
    assert full.status is SectionStatus.COLLECTED, f"{source}: payload misread as empty"
    assert full.raw == {"q1": full_payload}


def test_an_unexpected_payload_shape_is_collected_not_empty(stub_registry):
    """A schema surprise is not an answer about the world.

    Reporting ``EMPTY`` here would tell a consumer "this signal was checked and was
    absent" on the basis of a payload we did not understand — the more damaging of
    the two possible errors.
    """
    stub_registry(ToolResult(ok=True, data={"unexpected": "shape"}, metadata={}))
    section = collector_for("metrics").collect(_spec(), CORR)
    assert section.status is SectionStatus.COLLECTED


def test_cmdb_and_oncall_treat_any_payload_as_an_answer(stub_registry):
    """Neither has a meaningful empty shape — the mock CMDB deliberately falls back
    to a default team so an agent always has somewhere to route."""
    stub_registry(
        ToolResult(ok=True, data={"service": "pay", "team": "Platform On-Call"}, metadata={})
    )
    assert collector_for("cmdb").collect(_spec("cmdb", service="pay"), CORR).status is (
        SectionStatus.COLLECTED
    )


# --- caching ------------------------------------------------------------


def test_second_collection_of_the_same_spec_is_served_from_cache(stub_registry):
    """The deduplication the whole layer exists for, measured at its smallest unit."""
    registry = stub_registry(
        ToolResult(ok=True, data={"team": "Payments"}, metadata={"provider": "db"})
    )
    collector = collector_for("oncall")
    spec = _spec("oncall", team="Payments", service="payment")

    first = collector.collect(spec, CORR)
    second = collector.collect(spec, CORR)

    assert len(registry.calls) == 1, "the second collection should not have called out"
    assert not first.provenance.cached
    assert second.provenance.cached, "a cache hit must be visible in provenance"
    assert second.raw == first.raw


def test_cache_does_not_leak_across_incidents(stub_registry):
    """Without incident scoping, a 60s TTL on an on-call lookup would serve one
    incident's engineer to the next — across a shift boundary that pages the wrong
    human."""
    registry = stub_registry(ToolResult(ok=True, data={"team": "Payments"}, metadata={}))
    collector = collector_for("oncall")
    spec = _spec("oncall", team="Payments")

    collector.collect(spec, "incident-a")
    collector.collect(spec, "incident-b")

    assert len(registry.calls) == 2


def test_failures_are_not_cached(stub_registry):
    """Caching a failure replays one dropped packet for the whole TTL window."""
    registry = stub_registry(ToolResult(ok=False, error="HTTPError: boom"))
    collector = collector_for("metrics")
    spec = _spec()

    collector.collect(spec, CORR)
    collector.collect(spec, CORR)

    assert len(registry.calls) == 2


def test_two_query_ids_with_identical_params_share_one_round_trip(stub_registry):
    """Keyed on the query fingerprint, not the label.

    Two agents asking the identical question under different names is exactly the
    duplication this layer exists to remove; keying on the label would leave it in
    place while appearing to fix it.
    """
    registry = stub_registry(ToolResult(ok=True, data={"results": [{"m": 1}]}, metadata={}))
    collector = collector_for("metrics")

    collector.collect(
        SectionSpec(source="metrics", query_id="rca.errors", params={"promql": "x"}), CORR
    )
    collector.collect(
        SectionSpec(source="metrics", query_id="triage.errors", params={"promql": "x"}), CORR
    )

    assert len(registry.calls) == 1


def test_a_spec_can_select_a_sibling_capability_in_its_source_family(stub_registry):
    """``metrics`` is two capabilities, not one.

    The RCA agent needs both ``observability.metrics.query`` (PromQL) and
    ``observability.metrics.alerts`` (what is firing), and they belong in the same
    section because they are the same kind of evidence from the same provider. The
    alternative — a twelfth ``Source`` for alerts — would split one provider's
    evidence across two sections over an endpoint boundary.
    """
    registry = stub_registry(
        ToolResult(ok=True, data={"alerts": [{"state": "firing"}]}, metadata={})
    )
    collector = collector_for("metrics")

    collector.collect(
        SectionSpec(source="metrics", query_id="alerts", capability="observability.metrics.alerts"),
        CORR,
    )
    collector.collect(SectionSpec(source="metrics", query_id="q", params={"promql": "up"}), CORR)

    assert [capability for capability, _ in registry.calls] == [
        "observability.metrics.alerts",
        "observability.metrics.query",
    ]


def test_the_capability_override_cannot_bypass_the_denylist(stub_registry):
    """The override arrives at call time, so ``__init__``'s guard cannot see it.

    Without a second check this field would be a way to reach ``automation.fault.clear``
    — the RCA grounding probe whose cached failure silently disables grounding — through
    a collector that was constructed legitimately.
    """
    stub_registry(ToolResult(ok=True, data={}, metadata={}))
    with pytest.raises(ContextDenylistError):
        collector_for("metrics").collect(
            SectionSpec(source="metrics", query_id="x", capability="automation.fault.clear"),
            CORR,
        )


def test_two_capabilities_with_empty_params_do_not_share_a_cache_entry(stub_registry):
    """Both take no arguments, so only the capability distinguishes them.

    A fingerprint over params alone would serve the alerts answer for a PromQL query.
    """
    a = SectionSpec(source="metrics", query_id="alerts", capability="observability.metrics.alerts")
    b = SectionSpec(source="metrics", query_id="other", capability="observability.metrics.query")
    assert a.fingerprint() != b.fingerprint()


def test_collector_passes_the_callers_params_through_untouched(stub_registry):
    """The platform owns the round-trip; the agent owns the query.

    Five call sites use five different PromQL dialects measuring different things —
    rewriting one would change that agent's numbers with nothing in CI to catch it.
    """
    registry = stub_registry(ToolResult(ok=True, data={"results": []}, metadata={}))
    promql = 'sum(rate(orders_failed_total{service="pay"}[5m]))'
    collector_for("metrics").collect(
        SectionSpec(source="metrics", query_id="q", params={"promql": promql}), CORR
    )

    capability, kwargs = registry.calls[0]
    assert capability == "observability.metrics.query"
    assert kwargs == {"promql": promql}


# --- helpers ------------------------------------------------------------


def test_not_requested_and_unavailable_are_distinguishable():
    nr = not_requested("metrics")
    un = unavailable("metrics", "prometheus", "capability not registered")

    assert nr.status is SectionStatus.NOT_REQUESTED
    assert un.status is SectionStatus.UNAVAILABLE
    assert not nr.status.attempted and not un.status.attempted
    assert not nr.status.usable and not un.status.usable
    assert un.provenance.coverage_note == "capability not registered"


# --- seam collectors ----------------------------------------------------


def test_seam_collectors_do_not_double_wrap_resilience(monkeypatch):
    """Each of these wraps an already-guarded chain.

    A second ``guard`` around them would nest two timeout budgets: a 3s outer bound
    around a chain that is itself allowed 3s *per provider*, so the outer timeout
    would fire mid-chain and report FAILED for a chain working through its tiers
    normally.
    """
    called = False

    def _tripwire(*args, **kwargs):  # pragma: no cover - asserted not to run
        nonlocal called
        called = True
        raise AssertionError("seam collectors must not call resilience.guard")

    monkeypatch.setattr("aiops.tools.resilience.guard", _tripwire, raising=True)
    monkeypatch.setattr(
        "aiops.tools.topology.resolve",
        lambda service: (_ for _ in ()).throw(RuntimeError("no chain in this test")),
        raising=True,
    )
    section = TopologyCollector().collect(_spec("topology", service="pay"), CORR)

    assert not called
    assert section.status is SectionStatus.FAILED


def test_topology_collector_requires_a_service():
    section = TopologyCollector().collect(SectionSpec(source="topology", query_id="q"), CORR)
    assert section.status is SectionStatus.UNAVAILABLE
    assert section.provenance.coverage_note == "no service given"


def test_deployments_collector_requires_a_window():
    section = DeploymentsCollector().collect(
        SectionSpec(source="deployments", query_id="q", params={"service": "pay"}), CORR
    )
    assert section.status is SectionStatus.UNAVAILABLE
    assert "window" in (section.provenance.coverage_note or "")


def test_incident_history_separates_searched_and_found_nothing_from_could_not_search(
    monkeypatch,
):
    """The distinction that licenses a consumer to conclude "this incident is novel".

    ``RetrievalStatus.EMPTY`` is the seam's own word for "queried successfully, found
    nothing", and it is the only status that may become ``SectionStatus.EMPTY``.
    """
    from aiops.tools.incident_history import RetrievalResult, RetrievalStatus

    monkeypatch.setattr(
        "aiops.tools.incident_history.search_similar",
        lambda query: [RetrievalResult(provider="mock", status=RetrievalStatus.EMPTY)],
        raising=True,
    )
    searched = IncidentHistoryCollector().collect(_spec("incident_history", service="pay"), CORR)
    assert searched.status is SectionStatus.EMPTY
    assert "similarity floor" in (searched.provenance.coverage_note or "")

    monkeypatch.setattr(
        "aiops.tools.incident_history.search_similar",
        lambda query: [
            RetrievalResult(provider="mock", status=RetrievalStatus.UNAVAILABLE, error="off")
        ],
        raising=True,
    )
    blind = IncidentHistoryCollector().collect(
        _spec("incident_history", service="pay"), f"{CORR}-b"
    )
    assert blind.status is SectionStatus.UNAVAILABLE


def test_seam_collectors_never_raise(monkeypatch):
    """All three promise it, and the builder relies on it having no exceptions."""
    for target, collector, spec in (
        ("aiops.tools.topology.resolve", TopologyCollector(), _spec("topology", service="s")),
        (
            "aiops.tools.incident_history.search_similar",
            IncidentHistoryCollector(),
            _spec("incident_history", service="s"),
        ),
    ):
        monkeypatch.setattr(
            target,
            lambda *a, **k: (_ for _ in ()).throw(ValueError("seam exploded")),
            raising=True,
        )
        section = collector.collect(spec, CORR)
        assert isinstance(section, ContextSection)
        assert section.status is SectionStatus.FAILED


# --- in-flight request coalescing ----------------------------------------
#
# A cache alone only stops a REPEATED request from re-fetching; it does nothing
# for two requests that are CONCURRENT, because "check the cache, then fetch" is
# a race, not an atomic operation. The builder's own fan-out makes this the
# common case: alert_triage's three trace-search candidates are frequently the
# identical string, and Phase 8 unions that with notification's identical-params
# trace search into one build, so four specs with the same fingerprint can be
# in flight at once. These tests prove the collector coalesces them into one
# live call rather than letting the exact count become a timing accident.


def test_concurrent_identical_requests_make_exactly_one_live_call(monkeypatch):
    """The regression this fix exists for. Ten threads request the identical
    spec at once; without coalescing, several race past the cache-miss check
    before any of them populates the cache, and the live-call count becomes
    non-deterministic (observed as low as 2 of 10 coalescing, under real
    system load in the full suite — see the failure this test was written
    against)."""
    import threading
    import time

    call_count = 0
    call_lock = threading.Lock()

    class _SlowRegistry:
        def call(self, capability: str, **kwargs: Any) -> ToolResult:
            nonlocal call_count
            with call_lock:
                call_count += 1
            # Long enough that all ten threads are guaranteed to have started
            # and hit the cache-miss check before this one finishes — the
            # exact window the race lived in.
            time.sleep(0.05)
            return ToolResult(ok=True, data={"results": [{"value": [1, "1"]}]}, metadata={})

    monkeypatch.setattr(
        "aiops.context.collectors.base.get_registry", lambda: _SlowRegistry(), raising=True
    )
    collector = collector_for("metrics")
    spec = _spec("metrics", promql="up")

    results: list[ContextSection] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        section = collector.collect(spec, CORR)
        with results_lock:
            results.append(section)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert call_count == 1, f"expected exactly one live call, saw {call_count}"
    assert len(results) == 10
    assert all(r.status is SectionStatus.COLLECTED for r in results)
    # Nine of the ten were waiters — each must be able to tell it was served
    # from the leader's fetch, not that it independently called live.
    assert sum(1 for r in results if r.provenance.cached) == 9


def test_a_failed_leader_result_lets_a_waiter_retry_rather_than_hang(monkeypatch):
    """FAILED/UNAVAILABLE results are not cached (see cache.ttl_for_status), so
    a waiter that wakes to a cache miss must become a new leader and retry live
    — never serve nothing and never deadlock waiting on an Event nobody will
    set again."""
    import threading

    class _FlakyThenHealthyRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, capability: str, **kwargs: Any) -> ToolResult:
            self.calls += 1
            if self.calls == 1:
                return ToolResult(ok=False, error="connection refused", metadata={})
            return ToolResult(ok=True, data={"results": [{"value": [1, "1"]}]}, metadata={})

    registry = _FlakyThenHealthyRegistry()
    monkeypatch.setattr(
        "aiops.context.collectors.base.get_registry", lambda: registry, raising=True
    )
    collector = collector_for("metrics")
    spec = _spec("metrics", promql="up")

    results: list[ContextSection] = []

    def _worker() -> None:
        results.append(collector.collect(spec, CORR))

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 3
    assert any(r.status is SectionStatus.COLLECTED for r in results), (
        "at least one caller must retry live after the uncached FAILED result "
        "rather than every caller silently inheriting the failure"
    )


def test_reset_for_tests_clears_leaked_inflight_entries():
    """A leaked entry from a crashed test would otherwise leave every later
    request for that exact key waiting on an Event nothing will ever set."""
    import threading

    from aiops.context.collectors import base as collectors_base

    key = "leaked-key-for-test"
    collectors_base._inflight[key] = threading.Event()
    collectors_base.reset_for_tests()
    assert key not in collectors_base._inflight
