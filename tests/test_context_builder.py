"""Tests for the eight-stage builder — the seam that turns one request into one pack.

Stage 1 is the only impure stage, so the registry and the three chained seams are
stubbed and nothing here touches a socket. Everything else runs for real: these are
whole-pipeline tests, and that is deliberate — a status that survives collection but
gets flattened by normalisation, or a secret that a later stage copies past the
redactor, is invisible to a per-stage test.

Three properties carry most of the weight.

**"Nobody asked" must never read as "we asked and there was nothing".** ``EMPTY`` is a
claim about the world — RCA renders "NONE — this signal was checked and was absent" for
an empty category and instructs the model to treat it as positive evidence *against* a
cause. ``NOT_REQUESTED``, ``UNAVAILABLE`` and ``FAILED`` are not claims at all. Every
test that touches a non-collected section asserts the distinction rather than merely
asserting "no observations".

**The call count is the layer's thesis.** "Build once, stop duplicating retrieval" is
either measurable or assumed, so the tests that count registry calls
(``test_two_requested_sections_perform_exactly_two_registry_calls``,
``test_two_agents_asking_the_identical_question_share_one_round_trip``,
``test_offline_makes_no_guarded_call_at_all``) assert exact numbers, not "at most".

**Determinism is what the eval harness rests on.** The collectors fan out over a thread
pool, so the order sections and observations arrive in genuinely varies between two runs
over the same incident. ``_stable`` is how these tests compare a whole context
byte-for-byte while excluding the two fields that are measurements of the *run* rather
than of the incident.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
import typing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiops.context.builder import (
    ContextBuilder,
    ContextRequest,
    _max_workers,
    _merge,
    build,
)
from aiops.context.correlation import derive_correlation_id
from aiops.context.models import SectionSpec, SectionStatus, Source
from aiops.context.pack import ContextSection, IncidentContext, SourceProvenance
from aiops.tools import resilience
from aiops.tools.change_context import ChangeContext, ChangeRecord, ChangeType
from aiops.tools.incident_history import (
    IncidentMatch,
    ResolutionMetadata,
    RetrievalResult,
    RetrievalStatus,
)
from aiops.tools.registry import ToolResult
from aiops.tools.topology import ProviderStatus, TopologyResolution, TopologyResult

SERVICE = "payment-service"
WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(minutes=15)
NOW = WINDOW_END + timedelta(seconds=30)

EVERY_SOURCE: tuple[str, ...] = typing.get_args(Source)

# A GitHub classic PAT shape (``ghp_`` + 36) with a deliberately non-hex body: an
# all-hex body would be masked by the signature normaliser's ``\b[0-9a-f]{8,}\b`` rule,
# so a redaction test using one would pass even with the redactor removed.
GITHUB_TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
LEAKY_LINE = f"auth failed for nina@example.com using {GITHUB_TOKEN}"

# Env vars that change what the builder does. Cleared by the ``registry`` fixture so a
# developer's ``.env`` cannot put a laptop on a different code path than CI — the same
# bleed class ``conftest.py``'s ``_opt_in_enrichment_seams_off`` exists for.
_CONTEXT_ENV = (
    "AIOPS_CONTEXT_COLLECTORS",
    "AIOPS_CONTEXT_WORKERS",
    "AIOPS_CONTEXT_TIMEOUT",
    "AIOPS_CONTEXT_RETRIES",
    "AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS",
)


# --- provider payloads ---------------------------------------------------
#
# Shapes taken from aiops/context/collectors/__init__.py, which pins them against the
# real providers. If a backend changes its schema these payloads are what has to move.

_CAPABILITY_FOR: dict[str, str] = {
    "metrics": "observability.metrics.query",
    "logs": "observability.logs.query",
    "traces": "observability.traces.search",
    "k8s_events": "observability.events.query",
    "dependencies": "itsm.cmdb.dependencies",
    "cmdb": "itsm.cmdb.lookup",
    "oncall": "oncall.schedule.lookup",
    "runbooks": "incident.resolvers.lookup",
}
"""The eight sources served by a single registry capability. The other three
(``topology``, ``incident_history``, ``deployments``) are chained seams."""

_PROVIDER_FOR: dict[str, str] = {
    "observability.metrics.query": "prometheus",
    "observability.logs.query": "loki",
    "observability.traces.search": "jaeger",
    "observability.events.query": "kubernetes",
    "itsm.cmdb.dependencies": "snow",
    "itsm.cmdb.lookup": "snow",
    "oncall.schedule.lookup": "db",
    "incident.resolvers.lookup": "db",
}

_PAYLOADS: dict[str, dict[str, Any]] = {
    "observability.metrics.query": {
        "query": "sum(rate(orders_failed_total[5m]))",
        "result_type": "vector",
        "results": [
            {
                "metric": {"__name__": "orders_failed_total", "service_name": SERVICE},
                "value": [WINDOW_END.timestamp(), "0.42"],
            }
        ],
    },
    "observability.logs.query": {
        "streams": [
            {
                "stream": {"level": "error", "service_name": SERVICE},
                "values": [[str(int(WINDOW_END.timestamp() * 1e9)), LEAKY_LINE]],
            }
        ]
    },
    "observability.traces.search": {
        "service": SERVICE,
        "lookback": "15m",
        "trace_count": 1,
        "traces": [
            {
                "trace_id": "4bf92f3577b34da6",
                "span_count": 7,
                "root_operation": "POST /charge",
                "duration_us": 1_500_000,
                "start_time_us": int(WINDOW_END.timestamp() * 1e6),
            }
        ],
    },
    "observability.events.query": {
        "namespace": "otel-demo",
        "events": [
            {
                "involved_object": {"kind": "Pod", "name": "payment-7f9c4b"},
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "type": "Warning",
                "count": 4,
                "last_timestamp": WINDOW_END.isoformat(),
                "event_time": None,
                "first_timestamp": WINDOW_START.isoformat(),
            }
        ],
        "configmaps": [
            {
                "name": "payment-config",
                "managed_fields": [
                    {"manager": "kubectl", "operation": "Update", "time": WINDOW_START.isoformat()}
                ],
            }
        ],
    },
    "itsm.cmdb.dependencies": {"service": SERVICE, "dependencies": ["mysql", "kafka"]},
    "itsm.cmdb.lookup": {
        "service": SERVICE,
        "team": "Payments",
        "runbook": "https://runbooks.internal/payment",
    },
    "oncall.schedule.lookup": {"team": "Payments", "engineer_email": "dana@example.com"},
    "incident.resolvers.lookup": {
        "service": SERVICE,
        "category": "Payment Gateway",
        "resolvers": [
            {"resolver_name": "Ada Lovelace", "resolver_handle": "@ada", "incident_id": "INC0001"}
        ],
    },
}

_PARAMS: dict[str, dict[str, Any]] = {
    "metrics": {"promql": f'sum(rate(orders_failed_total{{service="{SERVICE}"}}[5m]))'},
    "logs": {"logql": f'{{service_name="{SERVICE}"}} |= "error"', "limit": 50},
    "traces": {"service": SERVICE, "lookback": "15m"},
    "k8s_events": {"namespace": "otel-demo"},
    "topology": {"service": SERVICE},
    "dependencies": {"service": SERVICE},
    "deployments": {"service": SERVICE, "window_start": WINDOW_START, "window_end": WINDOW_END},
    "incident_history": {"service": SERVICE, "signatures": ["db connection timeout"]},
    "oncall": {"team": "Payments", "service": SERVICE},
    "cmdb": {"service": SERVICE},
    "runbooks": {"service": SERVICE, "category": "Payment Gateway"},
}


# --- stubs ---------------------------------------------------------------


class _StubRegistry:
    """Stands in for the tool registry, recording exactly what was asked of it.

    A stub of ``registry.call()``'s contract — never raises, reports everything as a
    ``ToolResult`` — rather than a mock of ``httpx``, matching
    ``tests/test_context_collectors.py``. It owns a deep copy of ``_PAYLOADS`` so the
    module-level table cannot be mutated by one test and read by the next, while still
    handing the *same* payload object to every call for one capability — which is what
    lets ``test_no_stage_mutates_a_provider_payload_in_place`` mean anything.
    """

    def __init__(self, overrides: dict[str, ToolResult | Exception] | None = None) -> None:
        self.payloads = copy.deepcopy(_PAYLOADS)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._overrides = overrides or {}

    def call(self, capability: str, **kwargs: Any) -> ToolResult:
        self.calls.append((capability, dict(kwargs)))
        if capability in self._overrides:
            outcome = self._overrides[capability]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        payload = self.payloads.get(capability)
        if payload is None:
            return ToolResult(
                ok=False,
                error=f"no provider for {capability}",
                metadata={"missing_provider": True},
            )
        return ToolResult(ok=True, data=payload, metadata={"provider": _PROVIDER_FOR[capability]})

    @property
    def capabilities(self) -> list[str]:
        return [capability for capability, _kwargs in self.calls]


@pytest.fixture
def registry(monkeypatch):
    """Install a stub registry and hand back the installer.

    Also clears every ``AIOPS_CONTEXT_*`` knob, so each test states the configuration
    it depends on instead of inheriting one.
    """
    for name in _CONTEXT_ENV:
        monkeypatch.delenv(name, raising=False)

    def _install(overrides: dict[str, ToolResult | Exception] | None = None) -> _StubRegistry:
        stub = _StubRegistry(overrides)
        monkeypatch.setattr(
            "aiops.context.collectors.base.get_registry", lambda: stub, raising=True
        )
        return stub

    return _install


def _topology_resolution(deps: tuple[str, ...] = ("mysql", "kafka")) -> TopologyResolution:
    return TopologyResolution(
        dependencies=list(deps),
        winning_provider="cmdb",
        attempts=[
            TopologyResult(
                provider="cmdb",
                status=ProviderStatus.RESOLVED,
                dependencies=list(deps),
                latency_ms=1.5,
                payload_present=True,
            )
        ],
    )


def _history_results() -> list[RetrievalResult]:
    return [
        RetrievalResult(
            provider="mock",
            status=RetrievalStatus.MATCHED,
            corpus_size=12,
            latency_ms=2.5,
            matches=[
                IncidentMatch(
                    incident_id="INC0042",
                    similarity_score=0.72,
                    title="payment latency spike",
                    occurred_at=WINDOW_START - timedelta(days=9),
                    provider="mock",
                    resolution=ResolutionMetadata(
                        resolved=True,
                        recorded_cause="connection pool exhausted",
                        resolution_summary="raised pool size to 40",
                    ),
                )
            ],
        )
    ]


def _change_context() -> ChangeContext:
    return ChangeContext(
        records=[
            ChangeRecord(
                change_id="deploy-118",
                change_type=ChangeType.DEPLOYMENT,
                source="k8s",
                timestamp=WINDOW_START + timedelta(minutes=1),
                service=SERVICE,
                summary="deploy payment v2.3.1",
                commit_sha="9f2c1ab8de41",
                author_username="dana",
            )
        ],
        sources_collected=["k8s"],
        sources_unavailable=["github"],
        coverage_note="github provider unavailable",
    )


@pytest.fixture
def seams(monkeypatch):
    """Pin the three already-chained seams so a full eleven-section build does no I/O.

    Each seam collector imports its seam inside ``collect()``, so patching the module
    attribute is what reaches it — and patching the seam rather than ``resilience.guard``
    keeps the double-wrapping these collectors deliberately avoid out of the test too.
    """

    def _install(*, topology=None, history=None, changes=None) -> None:
        monkeypatch.setattr(
            "aiops.tools.topology.resolve",
            topology or (lambda service: _topology_resolution()),
            raising=True,
        )
        monkeypatch.setattr(
            "aiops.tools.incident_history.search_similar",
            history or (lambda query: _history_results()),
            raising=True,
        )
        monkeypatch.setattr(
            "aiops.tools.change_context.collect_change_context",
            changes or (lambda service, start, end: _change_context()),
            raising=True,
        )

    return _install


def _raiser(exc: Exception):
    def _raise(*_args: Any, **_kwargs: Any):
        raise exc

    return _raise


# --- request / assertion helpers ----------------------------------------


def _spec(source: str, query_id: str | None = None, **params: Any) -> SectionSpec:
    return SectionSpec(
        source=source,
        query_id=query_id or f"{source}.primary",
        params=params or _PARAMS[source],
    )


def _request(
    *sources: str, specs: list[SectionSpec] | None = None, **kwargs: Any
) -> ContextRequest:
    return ContextRequest(
        service=SERVICE,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        specs=specs if specs is not None else [_spec(source) for source in sources],
        **kwargs,
    )


def _section(
    status: SectionStatus,
    provider: str = "p",
    *,
    query_id: str | None = None,
    payload: Any = None,
    note: str | None = None,
    error: str | None = None,
    latency_ms: float = 0.0,
) -> ContextSection:
    return ContextSection(
        status=status,
        provenance=SourceProvenance(
            provider=provider,
            status=status,
            latency_ms=latency_ms,
            error=error,
            coverage_note=note,
        ),
        raw={query_id: payload} if query_id else None,
    )


def _stable(pack: IncidentContext) -> str:
    """The pack's JSON form with this run's wall-clock measurements zeroed.

    ``latency_ms`` and ``cached`` are facts about *this build* — how long a socket took,
    whether the intra-incident cache had already answered — not about the incident, so
    they legitimately differ between two runs over identical evidence. Everything else
    must be byte-identical, and excluding exactly these two is what lets the
    determinism tests compare whole contexts instead of hand-picked fields.
    """
    data = pack.model_dump(mode="json")
    for name in EVERY_SOURCE:
        data[name]["provenance"]["latency_ms"] = 0.0
        data[name]["provenance"]["cached"] = False
    return json.dumps(data)


def _observations_json(pack: IncidentContext) -> str:
    return json.dumps([obs.model_dump(mode="json") for obs in pack.observations])


# --- every section is present -------------------------------------------


def test_every_one_of_the_eleven_sections_is_present_however_little_was_requested(registry):
    """``IncidentContext`` requires all eleven, and a consumer must be able to tell
    "not requested" from "requested and unavailable" without checking for a key."""
    registry()
    pack = build(_request("metrics"), now=NOW)

    assert set(pack.sections) == set(EVERY_SOURCE)
    for name in EVERY_SOURCE:
        assert isinstance(pack.section(name), ContextSection), name


def test_a_section_nobody_asked_for_is_not_requested_and_never_empty(registry):
    """The distinction the RCA prompt spends a whole line on.

    ``EMPTY`` renders as "NONE — this signal was checked and was absent" and the model
    is told to treat it as positive evidence *against* a cause. Reporting a section
    nobody paid for as ``EMPTY`` would rule out causes on the strength of a query that
    never ran.
    """
    registry()
    pack = build(_request("metrics"), now=NOW)

    assert pack.metrics.status is SectionStatus.COLLECTED
    for name in EVERY_SOURCE:
        if name == "metrics":
            continue
        section = pack.section(name)
        assert section.status is SectionStatus.NOT_REQUESTED, name
        assert not section.status.attempted, name
        assert not section.status.usable, name
        assert section.observations == (), name
        assert section.raw is None, name


def test_a_full_build_fills_every_section_with_a_real_answer(registry, seams):
    """The whole-pipeline smoke case the rest of the file leans on: eleven sources, one
    request, and no section left in a non-attempted state."""
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW)

    for name in EVERY_SOURCE:
        section = pack.section(name)
        assert section.status.usable, f"{name}: {section.status} / {section.provenance}"
        assert section.raw, name
    assert pack.observations
    assert len(pack.evidence_ranking) == len(pack.observations)
    assert not pack.is_empty


# --- the call count: the layer's stated purpose --------------------------


def test_two_requested_sections_perform_exactly_two_registry_calls(registry):
    """The measurement that turns "build once, stop duplicating retrieval" from a claim
    into a fact. An exact count, not an upper bound: a build that quietly re-queried a
    section it had already collected would still satisfy "at most one call each"
    nowhere, but would satisfy a lax assertion the moment somebody added a second pass.
    """
    stub = registry()
    pack = build(_request("metrics", "logs"), now=NOW)

    assert len(stub.calls) == 2, stub.calls
    assert sorted(stub.capabilities) == [
        "observability.logs.query",
        "observability.metrics.query",
    ]
    assert pack.metrics.status is SectionStatus.COLLECTED
    assert pack.logs.status is SectionStatus.COLLECTED


def test_the_callers_own_query_reaches_the_provider_untouched(registry):
    """The platform owns the round-trip; the agent owns the query. Five call sites use
    five PromQL dialects that measure different things, so a rewrite here would change
    one agent's numbers with nothing in CI to catch it."""
    stub = registry()
    build(_request("metrics", "logs"), now=NOW)

    sent = dict(stub.calls)
    assert sent["observability.metrics.query"] == _PARAMS["metrics"]
    assert sent["observability.logs.query"] == _PARAMS["logs"]


def test_two_agents_asking_the_identical_question_share_one_round_trip(registry, monkeypatch):
    """Two query ids, one PromQL, one call.

    Keyed on the query fingerprint rather than the label, because two agents asking the
    identical question under different names is exactly the duplication this layer
    exists to remove; keying on the label would leave it in place while appearing to fix
    it. Pinned to a single worker so the two specs are ordered rather than racing each
    other past the cache — the thing being measured is the cache, not the scheduler.

    KNOWN GAP, deliberately not asserted here: the deduplicated result comes back keyed
    under the *first* spec's ``query_id``, so ``raw`` holds only ``rca.errors`` and the
    second caller cannot find its own answer by name — see
    ``aiops/context/collectors/base.py::CapabilityCollector.collect``, which re-stamps
    provenance on a cache hit but never re-keys ``raw``.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_WORKERS", "1")
    stub = registry()
    pack = build(
        _request(
            specs=[
                _spec("metrics", "rca.errors", **_PARAMS["metrics"]),
                _spec("metrics", "triage.errors", **_PARAMS["metrics"]),
            ]
        ),
        now=NOW,
    )

    assert len(stub.calls) == 1, stub.calls
    assert pack.metrics.status is SectionStatus.COLLECTED
    assert pack.metrics.raw, "the shared payload must still reach the section"
    assert pack.metrics.observations


def test_rebuilding_the_same_incident_calls_nothing_a_second_time(registry):
    """A standalone agent invocation lands on the orchestrated run's cache entries
    without any coordination — the reason the correlation id is derived rather than
    random."""
    stub = registry()
    first = build(_request("metrics", "logs"), now=NOW)
    calls_after_first = len(stub.calls)
    second = build(_request("metrics", "logs"), now=NOW)

    assert calls_after_first == 2
    assert len(stub.calls) == 2, "the second build should have been served from cache"
    assert _stable(first) == _stable(second)
    assert second.logs.provenance.cached is True


# --- offline: the zero-I/O path -----------------------------------------


def test_offline_makes_no_guarded_call_at_all(registry, seams):
    """The path the eval harness depends on for reproducible goldens.

    ``resilience.stats()`` is the proof: every guarded call increments a ``calls``
    counter before it does anything else, so an empty stats dict is evidence that no
    provider was even *considered* — much stronger than an empty registry call log,
    which a collector could bypass.
    """
    stub = registry()
    seams()
    pack = build(_request(*EVERY_SOURCE, offline=True), now=NOW)

    assert stub.calls == []
    assert resilience.stats() == {}
    for name in EVERY_SOURCE:
        section = pack.section(name)
        assert section.status is SectionStatus.NOT_REQUESTED, name
        assert section.provenance.coverage_note == "offline build requested", name


def test_an_offline_context_is_still_a_well_formed_context(registry, seams):
    """A golden run needs the identity, the security record and the ranking to exist —
    an offline build that returned a differently-shaped object could not be diffed
    against a live one."""
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE, offline=True, severity="Sev-2"), now=NOW)

    assert pack.built_at == NOW
    assert pack.incident.service == SERVICE
    assert pack.incident.severity == "Sev-2"
    assert pack.evidence_ranking == ()
    assert pack.observations == ()
    assert pack.is_empty
    assert pack.security.redaction_applied is False
    assert IncidentContext.model_validate(pack.model_dump(mode="json")) == pack


# --- build never raises -------------------------------------------------


_FAILURE_MODES: list[tuple[str, ToolResult | Exception, SectionStatus]] = [
    (
        "provider_error",
        ToolResult(ok=False, error="HTTPError: connection refused"),
        SectionStatus.FAILED,
    ),
    (
        "missing_provider",
        ToolResult(ok=False, error="no provider", metadata={"missing_provider": True}),
        SectionStatus.UNAVAILABLE,
    ),
    ("registry_raises", RuntimeError("kaboom"), SectionStatus.FAILED),
    (
        "payload_is_a_string",
        ToolResult(ok=True, data="not a payload at all"),
        SectionStatus.COLLECTED,
    ),
    ("payload_is_none", ToolResult(ok=True, data=None), SectionStatus.EMPTY),
    ("payload_missing_every_key", ToolResult(ok=True, data={}), SectionStatus.COLLECTED),
    (
        "list_where_a_list_belongs_is_a_string",
        ToolResult(ok=True, data={"query": "up", "results": "boom"}),
        SectionStatus.COLLECTED,
    ),
    (
        "row_is_none",
        ToolResult(ok=True, data={"query": "up", "results": [None, {"metric": None}]}),
        SectionStatus.COLLECTED,
    ),
    (
        "prometheus_nan_over_no_samples",
        ToolResult(
            ok=True,
            data={
                "query": "up",
                "result_type": "vector",
                "results": [{"metric": {"__name__": "error_rate"}, "value": [1.0, "NaN"]}],
            },
        ),
        SectionStatus.COLLECTED,
    ),
]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [(outcome, expected) for _id, outcome, expected in _FAILURE_MODES],
    ids=[case[0] for case in _FAILURE_MODES],
)
def test_build_degrades_one_section_and_keeps_the_rest(
    registry, outcome: ToolResult | Exception, expected: SectionStatus
):
    """A failure on the incident path must cost evidence, never a verdict.

    Each mode is applied to ``metrics`` only, and ``logs`` is asserted intact in the
    same context — a builder that aborted the fan-out on the first bad section would
    return a context that *looks* thin rather than one that says which part is missing.
    """
    registry({"observability.metrics.query": outcome})
    pack = build(_request("metrics", "logs"), now=NOW)

    assert pack.metrics.status is expected
    assert pack.logs.status is SectionStatus.COLLECTED
    assert pack.logs.observations, "a sibling section must not be collateral damage"
    if not expected.usable:
        assert pack.metrics.observations == ()
        assert pack.metrics.raw is None


def test_a_nan_sample_never_reaches_an_observation(registry):
    """Prometheus renders an aggregation over zero samples as the literal ``"NaN"``.

    ``float("NaN")`` is truthy and formats as ``nan``, so the obvious code hands a model
    ``error_rate = nan`` as though it were a reading. The section is still ``COLLECTED``
    — a row came back, so this is not a claim that nothing was seen — but the row must
    not become evidence.
    """
    registry(
        {
            "observability.metrics.query": ToolResult(
                ok=True,
                data={
                    "query": "up",
                    "result_type": "vector",
                    "results": [{"metric": {"__name__": "error_rate"}, "value": [1.0, "NaN"]}],
                },
            )
        }
    )
    pack = build(_request("metrics"), now=NOW)

    assert pack.metrics.status is SectionStatus.COLLECTED
    assert pack.metrics.observations == ()
    assert "nan" not in _observations_json(pack).lower()
    assert pack.evidence_ranking == ()


def test_a_registry_that_cannot_even_be_reached_still_yields_every_section(registry, monkeypatch):
    """``get_registry()`` is called outside the collector's own guard, so this exercises
    the builder's own ``except`` rather than the collector's — the one that exists purely
    because ``build`` must not raise either."""
    registry()
    monkeypatch.setattr(
        "aiops.context.collectors.base.get_registry",
        _raiser(RuntimeError("registry exploded")),
        raising=True,
    )
    pack = build(_request("metrics", "logs"), now=NOW)

    for name in ("metrics", "logs"):
        section = pack.section(name)
        assert section.status is SectionStatus.FAILED, name
        assert "registry exploded" in (section.provenance.error or ""), name
    assert pack.is_empty


@pytest.mark.parametrize("source", ["topology", "incident_history", "deployments"])
def test_a_seam_that_raises_costs_its_own_section_only(registry, seams, source: str):
    """All three seams document that they never raise. This is what happens when one
    does anyway, and it must be one FAILED section rather than a lost context."""
    registry()
    seams(
        **{
            {"topology": "topology", "incident_history": "history", "deployments": "changes"}[
                source
            ]: _raiser(ValueError("seam exploded"))
        }
    )
    pack = build(_request("topology", "incident_history", "deployments", "metrics"), now=NOW)

    assert pack.section(source).status is SectionStatus.FAILED
    assert "seam exploded" in (pack.section(source).provenance.error or "")
    assert pack.metrics.status is SectionStatus.COLLECTED


def test_build_survives_every_source_failing_at_once(registry, seams):
    """The worst case: nothing answered. Still eleven sections, still a valid context,
    and ``is_empty`` True so an adapter can fall through to its legacy retrieval rather
    than presenting a context-shaped void as evidence."""
    registry(
        {
            capability: ToolResult(ok=False, error="HTTPError: boom")
            for capability in _CAPABILITY_FOR.values()
        }
    )
    seams(
        topology=_raiser(OSError("down")),
        history=_raiser(OSError("down")),
        changes=_raiser(OSError("down")),
    )
    pack = build(_request(*EVERY_SOURCE), now=NOW)

    assert set(pack.sections) == set(EVERY_SOURCE)
    assert pack.usable_sources == ()
    assert pack.evidence_ranking == ()
    assert pack.is_empty
    for name in EVERY_SOURCE:
        assert pack.section(name).status is SectionStatus.FAILED, name


# --- _merge -------------------------------------------------------------


_PRECEDENCE: tuple[SectionStatus, ...] = (
    SectionStatus.COLLECTED,
    SectionStatus.EMPTY,
    SectionStatus.FAILED,
    SectionStatus.UNAVAILABLE,
    SectionStatus.NOT_REQUESTED,
)


def test_merging_into_nothing_returns_the_incoming_section_itself():
    incoming = _section(SectionStatus.COLLECTED, query_id="q1", payload={"rows": [1]})
    assert _merge(None, incoming) is incoming


@pytest.mark.parametrize(("left", "right"), list(itertools.product(_PRECEDENCE, _PRECEDENCE)))
def test_merge_keeps_the_strongest_status_regardless_of_arrival_order(
    left: SectionStatus, right: SectionStatus
):
    """``COLLECTED > EMPTY > FAILED > UNAVAILABLE > NOT_REQUESTED``, symmetrically.

    Order-independence is the half that matters: the collectors fan out, so which of two
    queries on one source lands first is a scheduling accident, and a section whose
    status depended on it would report ``FAILED`` for evidence it is holding on one run
    and ``COLLECTED`` on the next.
    """
    expected = min(left, right, key=_PRECEDENCE.index)
    assert _merge(_section(left, "a"), _section(right, "b")).status is expected
    assert _merge(_section(right, "b"), _section(left, "a")).status is expected


def test_merge_keeps_both_query_payloads_under_their_own_ids():
    """One section, several queries — RCA alone issues about ten distinct PromQL queries
    against ``metrics``, and each consumer has to be able to find its own answer."""
    first = _section(SectionStatus.COLLECTED, query_id="rca.errors", payload={"rows": [1]})
    second = _section(SectionStatus.COLLECTED, query_id="triage.errors", payload={"rows": [2]})

    merged = _merge(first, second)
    assert merged.raw == {"rca.errors": {"rows": [1]}, "triage.errors": {"rows": [2]}}


def test_one_query_with_rows_makes_the_section_collected_even_when_a_sibling_failed():
    """The honest reading: the section *does* carry evidence. Downgrading it to FAILED
    would hide real rows; upgrading the failure away would hide the gap."""
    collected = _section(
        SectionStatus.COLLECTED, "prometheus", query_id="ok", payload={"rows": [1]}
    )
    failed = _section(SectionStatus.FAILED, "prometheus", error="HTTPError: boom")

    merged = _merge(collected, failed)
    assert merged.status is SectionStatus.COLLECTED
    assert merged.raw == {"ok": {"rows": [1]}}


def test_a_failed_siblings_reason_survives_the_merge():
    """A partial section that says nothing about being partial is worse than a failed
    one: the consumer stops checking.

    Both provenance channels are inspected because ``_status_for`` leaves
    ``coverage_note`` empty for a plain ``ok=False`` result and puts the reason in
    ``error`` — a consumer discovering partialness must find it in one of the two.
    """
    collected = _section(
        SectionStatus.COLLECTED, "prometheus", query_id="ok", payload={"rows": [1]}
    )
    failed = _section(SectionStatus.FAILED, "prometheus", error="HTTPError: boom")

    merged = _merge(collected, failed)
    trace = f"{merged.provenance.coverage_note or ''} {merged.provenance.error or ''}"
    assert "HTTPError: boom" in trace
    assert merged.provenance.error == "HTTPError: boom"


def test_a_failed_siblings_coverage_note_lands_in_the_merged_note():
    """The case the docstring describes literally: an ``UNAVAILABLE`` sibling carries
    "capability not registered", and that note is how a consumer learns which half of
    the section is missing."""
    collected = _section(
        SectionStatus.COLLECTED, "prometheus", query_id="ok", payload={"rows": [1]}
    )
    skipped = _section(SectionStatus.UNAVAILABLE, "prometheus", note="capability not registered")

    merged = _merge(collected, skipped)
    assert merged.status is SectionStatus.COLLECTED
    assert merged.provenance.coverage_note == "capability not registered"


def test_merge_deduplicates_identical_notes_and_sums_the_latency():
    """Two queries that both hit the same partial-coverage condition should say so once;
    latency is additive because the section really did cost both round-trips."""
    first = _section(
        SectionStatus.COLLECTED,
        query_id="a",
        payload={},
        note="only 5 of 12 streams",
        latency_ms=10.0,
    )
    second = _section(
        SectionStatus.COLLECTED,
        query_id="b",
        payload={},
        note="only 5 of 12 streams",
        latency_ms=7.5,
    )

    merged = _merge(first, second)
    assert merged.provenance.coverage_note == "only 5 of 12 streams"
    assert merged.provenance.latency_ms == 17.5


def test_merge_does_not_mutate_either_input(registry):
    """``ContextSection`` is frozen but ``raw`` is a plain dict, so an in-place update
    here would let one query's result edit another's — and the inputs are cache entries
    that later builds still read."""
    first = _section(SectionStatus.COLLECTED, query_id="a", payload={"rows": [1]})
    second = _section(SectionStatus.EMPTY, query_id="b", payload={"rows": []})
    before_first = copy.deepcopy(first.raw)
    before_second = copy.deepcopy(second.raw)

    merged = _merge(first, second)
    assert first.raw == before_first
    assert second.raw == before_second
    assert merged.raw is not first.raw
    assert merged.raw is not second.raw


def test_the_builder_folds_two_specs_on_one_source_into_one_section(registry):
    """End to end: two different PromQL queries, one ``metrics`` section, both payloads
    addressable, and both observations normalised."""
    stub = registry()
    pack = build(
        _request(
            specs=[
                _spec("metrics", "rca.errors", promql="sum(rate(orders_failed_total[5m]))"),
                _spec("metrics", "rca.saturation", promql="max(container_cpu_usage)"),
            ]
        ),
        now=NOW,
    )

    assert len(stub.calls) == 2, "two distinct queries are two round-trips"
    assert set(pack.metrics.raw or {}) == {"rca.errors", "rca.saturation"}
    assert pack.metrics.status is SectionStatus.COLLECTED
    query_ids = {obs.metadata.get("query_id") for obs in pack.metrics.observations}
    assert query_ids == {"rca.errors", "rca.saturation"}


def test_a_failing_sibling_query_does_not_hide_the_collected_one(registry, monkeypatch):
    """One agent's PromQL erroring must not cost another agent its rows in the same
    section — the failure mode a shared section introduces and has to defend against."""
    monkeypatch.setenv("AIOPS_CONTEXT_RETRIES", "0")
    calls: list[dict[str, Any]] = []

    def responder(_capability: str, kwargs: dict[str, Any]) -> ToolResult:
        calls.append(kwargs)
        if kwargs.get("promql") == "boom":
            return ToolResult(ok=False, error="HTTPError: bad query")
        return ToolResult(ok=True, data=_PAYLOADS["observability.metrics.query"], metadata={})

    stub = registry()
    monkeypatch.setattr(
        stub, "call", lambda capability, **kwargs: responder(capability, kwargs), raising=True
    )
    pack = build(
        _request(
            specs=[
                _spec("metrics", "good", promql="up"),
                _spec("metrics", "bad", promql="boom"),
            ]
        ),
        now=NOW,
    )

    assert pack.metrics.status is SectionStatus.COLLECTED
    assert set(pack.metrics.raw or {}) == {"good"}
    assert pack.metrics.observations
    trace = f"{pack.metrics.provenance.coverage_note or ''} {pack.metrics.provenance.error or ''}"
    assert "bad query" in trace, "the sibling failure must remain discoverable"


# --- worker pool starvation ---------------------------------------------


def test_a_full_build_starves_nothing_in_the_shared_pool(registry, seams):
    """The subtle one, and the reason this builder owns an executor at all.

    ``resilience.guard`` submits into one process-wide pool of eight shared with the
    topology, incident-history and change-context seams. ``starved`` deliberately does
    *not* trip a breaker and does *not* count as an attempt, so a starved section comes
    back looking like it was never requested — a context silently missing evidence,
    which is precisely what this layer exists to prevent. If the builder ever fans out
    eleven collectors against those eight slots, this is the test that notices.
    """
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW)

    counters = resilience.stats()
    starved = {name: c for name, c in counters.items() if c.get("starved")}
    assert starved == {}, f"sections lost to pool starvation: {starved}"
    # Eight capability collectors go through ``guard``; the three seam collectors
    # deliberately do not, so a fourth entry here would mean somebody double-wrapped one.
    assert len(counters) == 8, counters
    assert all(name.startswith("context.") for name in counters), counters
    assert all(counter.get("calls") == 1 for counter in counters.values()), counters
    for name in EVERY_SOURCE:
        assert pack.section(name).status is not SectionStatus.NOT_REQUESTED, name


def test_the_builder_ceiling_stays_below_the_shared_pool():
    """The invariant the module docstring states and nothing else enforces: raising this
    builder's ceiling without raising ``AIOPS_RESILIENCE_WORKERS`` trades a little
    latency for silently missing sections."""
    assert _max_workers(len(EVERY_SOURCE)) * 2 <= resilience._MAX_WORKERS


def test_the_builder_never_runs_more_collectors_at_once_than_its_cap(registry, monkeypatch):
    """The cap has to bound live concurrency, not just the executor's constructor
    argument — a build that submitted every spec before checking would still report a
    small ``max_workers`` while occupying every slot in the shared pool."""
    monkeypatch.setenv("AIOPS_CONTEXT_WORKERS", "2")
    stub = registry()
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def counting_call(capability: str, **kwargs: Any) -> ToolResult:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.01)
        with lock:
            inflight -= 1
        return _StubRegistry.call(stub, capability, **kwargs)

    monkeypatch.setattr(stub, "call", counting_call, raising=True)
    build(_request(*_CAPABILITY_FOR), now=NOW)

    assert 1 <= peak <= 2, f"peak concurrency {peak} exceeded the configured cap of 2"


@pytest.mark.parametrize(
    ("raw", "requested", "expected"),
    [
        (None, 11, 4),  # unset: the ceiling chosen against the shared pool of 8
        (None, 2, 2),  # never more threads than there is work
        (None, 0, 1),  # ThreadPoolExecutor(max_workers=0) raises; clamp to 1
        ("2", 11, 2),
        ("1", 11, 1),
        ("0", 11, 4),  # a nonsense ceiling falls back rather than deadlocking on 0
        ("-3", 11, 4),
        ("four", 11, 4),  # non-numeric: fall back, never raise on the incident path
        ("", 11, 4),
        ("  3  ", 11, 3),  # int() tolerates surrounding whitespace, so this is honoured
    ],
)
def test_max_workers_honours_the_env_and_always_returns_at_least_one(
    monkeypatch, raw: str | None, requested: int, expected: int
):
    if raw is None:
        monkeypatch.delenv("AIOPS_CONTEXT_WORKERS", raising=False)
    else:
        monkeypatch.setenv("AIOPS_CONTEXT_WORKERS", raw)
    assert _max_workers(requested) == expected


# --- correlation id -----------------------------------------------------


def test_a_derived_correlation_id_is_stable_for_the_same_service_and_window():
    """What lets a standalone agent invocation share an orchestrated run's cache with no
    coordination between them — each of the 19 agents is individually sellable, so the
    standalone path is first-class rather than an edge case."""
    one = _request("metrics")
    two = _request("logs")

    assert one.correlation_id == two.correlation_id
    assert one.correlation_id == derive_correlation_id(SERVICE, WINDOW_START, WINDOW_END)


def test_a_supplied_correlation_id_is_used_verbatim(registry):
    """An orchestrator that already has an id must be able to impose it; deriving over
    the top would put the agent on a different cache namespace than its caller."""
    registry()
    request = _request("metrics", correlation_id="run-from-the-orchestrator")
    pack = build(request, now=NOW)

    assert request.correlation_id == "run-from-the-orchestrator"
    assert pack.incident.correlation_id == "run-from-the-orchestrator"
    assert all(obs.correlation_id == "run-from-the-orchestrator" for obs in pack.observations)


def test_windows_a_few_seconds_apart_derive_one_id(monkeypatch):
    """Two callers reasoning about one incident rarely compute byte-identical windows —
    the orchestrator's and an agent's own can differ by the seconds it took to get there.
    Without bucketing they would look like two incidents and double every backend call.
    """
    monkeypatch.delenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", raising=False)
    drifted = ContextRequest(
        service=SERVICE,
        window_start=WINDOW_START + timedelta(seconds=3),
        window_end=WINDOW_END + timedelta(seconds=4),
        specs=[_spec("metrics")],
    )
    assert drifted.correlation_id == _request("metrics").correlation_id


def test_a_different_service_derives_a_different_id():
    """Otherwise one incident's cached on-call engineer would be served for another's —
    across a shift boundary that pages the wrong human."""
    other = ContextRequest(
        service="checkout-service",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        specs=[_spec("metrics")],
    )
    assert other.correlation_id != _request("metrics").correlation_id


def test_the_request_reports_which_sources_it_asked_about():
    request = _request("metrics", "logs", "metrics")
    assert request.requested_sources == frozenset({"metrics", "logs"})


# --- injected clock and determinism -------------------------------------


def test_now_is_injected_not_read_from_the_clock(registry):
    """Stage 4 decays by age and stage 5 measures "how long before this did the deploy
    ship", so a test or an eval that could not pin "now" could not assert a score."""
    registry()
    pack = build(_request("metrics", "logs"), now=NOW)

    assert pack.built_at == NOW
    assert pack.built_at != datetime.now(UTC)


def test_two_builds_with_the_same_now_are_byte_identical(registry, seams):
    """Same inputs, same output — the property the eval harness needs to compare a
    re-run against its predecessor instead of merely replacing it."""
    registry()
    seams()
    request = _request(*EVERY_SOURCE)
    first = build(request, now=NOW)
    second = build(request, now=NOW)

    assert first.built_at == second.built_at
    assert first.evidence_ranking == second.evidence_ranking
    assert _stable(first) == _stable(second)


# A fixed permutation, not a random one: ``random`` is banned in this package, and a
# shuffle that varied per run would turn a real ordering bug into an intermittent one.
_SHUFFLED: tuple[str, ...] = (
    "oncall",
    "traces",
    "deployments",
    "metrics",
    "runbooks",
    "topology",
    "logs",
    "cmdb",
    "incident_history",
    "k8s_events",
    "dependencies",
)


def test_the_context_is_byte_identical_across_shuffled_spec_order(registry, seams):
    """Section order genuinely varies: the builder keys its working dict by spec order
    and hands the ranker observations in that order, so an unbroken tie or an unsorted
    stage would leak the schedule into the output and an eval's top-5 evidence set would
    change with no code change behind it.
    """
    assert sorted(_SHUFFLED) == sorted(EVERY_SOURCE), "the fixed shuffle must cover every source"
    registry()
    seams()
    in_order = build(_request(*EVERY_SOURCE), now=NOW)
    shuffled = build(_request(*_SHUFFLED), now=NOW)

    assert shuffled.evidence_ranking == in_order.evidence_ranking
    assert shuffled.observations == in_order.observations
    assert _stable(shuffled) == _stable(in_order)


def test_the_module_level_entry_point_agrees_with_the_class(registry, seams):
    """``build()`` mirrors ``resolve``/``search_similar``/``collect_change_context`` so a
    caller reaches the seam through a function; it must not become a second code path."""
    registry()
    seams()
    request = _request("metrics", "logs", "topology")
    assert _stable(build(request, now=NOW)) == _stable(ContextBuilder().build(request, now=NOW))


# --- no mutation --------------------------------------------------------


def test_no_stage_mutates_a_provider_payload_in_place(registry, seams):
    """``ContextSection.raw`` holds ``ToolResult.data`` by reference and
    ``Observation.metadata`` is a plain dict, so an in-place edit anywhere in stages 2–8
    would let one pipeline stage corrupt another's view — and, because the payload is
    also the cache entry, corrupt the *next* build of the same incident too."""
    stub = registry()
    seams()
    before = copy.deepcopy(stub.payloads)
    pack = build(_request(*EVERY_SOURCE), now=NOW)

    assert stub.payloads == before
    # And the section really is holding the provider's own object, so the check above is
    # not passing merely because everything was copied on the way in.
    assert pack.metrics.raw is not None
    assert pack.metrics.raw["metrics.primary"] is stub.payloads["observability.metrics.query"]


def test_a_second_build_cannot_see_the_first_ones_edits(registry, seams):
    """The consequence of the above, stated as the failure it prevents: two builds of one
    incident share cache entries, so a mutation would make the second build disagree with
    the first about evidence neither of them changed."""
    registry()
    seams()
    request = _request(*EVERY_SOURCE)
    first = build(request, now=NOW)
    snapshot = _stable(first)
    build(request, now=NOW)

    assert _stable(first) == snapshot


def test_the_request_is_not_modified_by_building_from_it(registry):
    registry()
    request = _request("metrics", "logs")
    specs_before = request.specs
    correlation_before = request.correlation_id

    build(request, now=NOW)
    assert request.specs == specs_before
    assert request.correlation_id == correlation_before


# --- budgeting is opt-in ------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"profile": "rca"}, {"max_tokens": 5_000}],
    ids=["neither", "profile-only", "max-tokens-only"],
)
def test_budgeting_is_opt_in_and_reports_nothing_when_not_asked_for(registry, kwargs):
    """``token_budget=None`` is the honest statement that nobody has trimmed this — as
    opposed to having been trimmed with a limit that happened not to bite. A half-given
    request is not a budget, and inventing a default limit would silently truncate."""
    registry()
    pack = build(_request("metrics", "logs"), now=NOW, **kwargs)
    assert pack.token_budget is None


def test_both_arguments_produce_a_populated_budget(registry, seams):
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW, profile="rca", max_tokens=100_000)

    assert pack.token_budget is not None
    assert pack.token_budget.profile == "rca"
    assert pack.token_budget.max_tokens == 100_000
    assert pack.token_budget.estimated_tokens > 0
    assert pack.token_budget.truncated is False


def test_a_tight_budget_trims_and_says_so(registry, seams):
    """Silent truncation is the failure mode that makes an LLM confidently wrong: a model
    handed a trimmed evidence set with no indication it was trimmed reasons as though it
    saw everything."""
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW, profile="summary", max_tokens=1)

    assert pack.token_budget is not None
    assert pack.token_budget.truncated is True
    assert pack.token_budget.evicted_observation_ids
    assert pack.observations == ()
    # A fully evicted section keeps its status: "we found things and dropped them" is not
    # "we could not look".
    assert pack.logs.status is SectionStatus.COLLECTED


# --- redaction ----------------------------------------------------------


def test_a_secret_in_a_log_line_never_reaches_an_observation(registry):
    """The one choke point where a leaked credential stops.

    Today an RCA prompt, a Slack war-room body and ``demo/audit/chatops.jsonl`` all
    quote log text verbatim, and the audit log persists it indefinitely. Both a token
    and an email are checked because they are caught by different pattern families
    (``scm._secrets.scrub`` and this stage's own prose rules).
    """
    registry()
    pack = build(_request("logs"), now=NOW)

    assert pack.logs.observations, "nothing to redact means nothing was tested"
    dumped = _observations_json(pack)
    assert GITHUB_TOKEN not in dumped
    assert "nina@example.com" not in dumped
    assert pack.security.redaction_applied is True
    assert pack.security.redaction_counts.get("github_token", 0) >= 1
    assert pack.security.redaction_counts.get("email", 0) >= 1


def test_raw_is_deliberately_not_redacted(registry):
    """The intentional asymmetry, asserted so nobody "fixes" it later.

    RCA rebuilds prompt strings like ``f"pod {pod}: cpu={cores:.2f} cores"`` from raw
    Prometheus rows and RA-007's log truncation is stream-order dependent, so scrubbing
    ``raw`` would silently change what those agents emit — the one thing a migration of
    this size cannot afford. The rule that comes with it: a consumer must never log,
    notify or prompt from ``raw`` directly.
    """
    registry()
    pack = build(_request("logs"), now=NOW)

    assert pack.logs.raw is not None
    assert GITHUB_TOKEN in json.dumps(pack.logs.raw)
    assert "nina@example.com" in json.dumps(pack.logs.raw)


def test_redacting_the_prose_does_not_break_paging(registry):
    """The asymmetry earning its keep: the on-call engineer's address is scrubbed out of
    the evidence text that reaches a prompt, while the routing target the notification
    assembler actually reads survives in ``metadata``."""
    registry()
    pack = build(_request("oncall"), now=NOW)

    (oncall,) = pack.oncall.observations
    assert "dana@example.com" not in oncall.evidence
    assert "[REDACTED_EMAIL]" in oncall.evidence
    assert oncall.metadata["engineer_email"] == "dana@example.com"


def test_a_clean_incident_reports_no_redaction(registry):
    """``redaction_applied`` means "something was actually redacted", not "this stage
    ran" — which is what makes a non-zero count meaningful to a reviewer."""
    registry()
    pack = build(_request("metrics"), now=NOW)

    assert pack.metrics.observations
    assert pack.security.redaction_applied is False
    assert pack.security.redaction_counts == {}


# --- collector chain configuration --------------------------------------


def test_an_excluded_collector_is_not_called_and_makes_no_claim(registry, monkeypatch):
    """``AIOPS_CONTEXT_COLLECTORS`` exists to *disable* a source — an unreachable Loki in
    a demo. The section it disables must not be readable as ``EMPTY``: nothing was
    checked, so nothing has been ruled out, and the note has to name the lever so an
    operator can tell this apart from a caller that simply did not ask.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_COLLECTORS", "metrics,traces")
    stub = registry()
    pack = build(_request("metrics", "logs"), now=NOW)

    assert stub.capabilities == ["observability.metrics.query"]
    assert pack.metrics.status is SectionStatus.COLLECTED
    assert pack.logs.status is not SectionStatus.EMPTY
    assert not pack.logs.status.usable
    assert pack.logs.observations == ()
    assert "AIOPS_CONTEXT_COLLECTORS" in (pack.logs.provenance.coverage_note or "")


def test_a_chain_of_nothing_but_typos_asks_no_one(registry, monkeypatch):
    """False completeness is worse than a visible gap.

    Every section still exists — ``IncidentContext`` requires that — but not one of them
    may claim to have been answered, and ``is_empty`` has to be True so an adapter falls
    through to its legacy retrieval instead of reasoning from a void.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_COLLECTORS", "metricz, logz")
    stub = registry()
    pack = build(_request("metrics", "logs"), now=NOW)

    assert stub.calls == []
    assert pack.usable_sources == ()
    assert pack.is_empty
    for name in ("metrics", "logs"):
        section = pack.section(name)
        assert section.status is not SectionStatus.EMPTY, name
        assert "AIOPS_CONTEXT_COLLECTORS" in (section.provenance.coverage_note or ""), name


def test_an_unknown_name_alongside_a_good_one_does_not_disable_the_good_one(registry, monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_COLLECTORS", "logz,metrics")
    stub = registry()
    pack = build(_request("metrics"), now=NOW)

    assert stub.capabilities == ["observability.metrics.query"]
    assert pack.metrics.status is SectionStatus.COLLECTED
    assert pack.metrics.observations


# --- dependencies feed the correlator -----------------------------------


def test_topology_is_preferred_over_dependencies_for_the_correlator():
    """Falling back rather than merging keeps provenance honest: the correlator's
    relations come from one named source, not a union of sources that may disagree."""
    sections = {
        "topology": _section(
            SectionStatus.COLLECTED, query_id="q", payload={"dependencies": ["mysql"]}
        ),
        "dependencies": _section(
            SectionStatus.COLLECTED, query_id="q", payload={"dependencies": ["kafka"]}
        ),
    }
    assert ContextBuilder._dependencies(sections) == ("mysql",)


def test_dependencies_answers_when_topology_could_not():
    sections = {
        "topology": _section(SectionStatus.FAILED, note="chain exhausted"),
        "dependencies": _section(
            SectionStatus.COLLECTED, query_id="q", payload={"dependencies": ["kafka", "redis"]}
        ),
    }
    assert ContextBuilder._dependencies(sections) == ("kafka", "redis")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"dependencies": None},
        {"dependencies": "mysql"},
        {"dependencies": []},
        "not a dict at all",
        {"dependencies": [None, 1]},
    ],
    ids=[
        "no-payload",
        "no-key",
        "null-list",
        "string-where-list-belongs",
        "empty-list",
        "payload-is-a-string",
        "list-of-non-strings",
    ],
)
def test_dependency_extraction_never_raises_on_a_malformed_payload(payload: Any):
    """These payloads come from real backends across versions, and this feeds stage 3 —
    a raise here would cost the whole context rather than one relation."""
    sections = {"topology": _section(SectionStatus.COLLECTED, query_id="q", payload=payload)}
    result = ContextBuilder._dependencies(sections)
    assert isinstance(result, tuple)
    assert all(isinstance(item, str) for item in result)


def test_an_unusable_topology_section_contributes_no_dependencies():
    """A ``FAILED`` payload is not a dependency list. Reading it would let a
    half-delivered response decide which observations are "unrelated"."""
    sections = {
        "topology": _section(
            SectionStatus.FAILED, query_id="q", payload={"dependencies": ["mysql"]}
        )
    }
    assert ContextBuilder._dependencies(sections) == ()


def test_the_collected_topology_reaches_the_correlator(registry, seams):
    """The wiring, end to end: a dependency named by the topology seam has to change how
    another section's observations are labelled, or stage 3 is running on nothing."""
    registry(
        {
            "observability.logs.query": ToolResult(
                ok=True,
                data={
                    "streams": [
                        {
                            "stream": {"level": "error", "service_name": "mysql"},
                            "values": [[str(int(WINDOW_END.timestamp() * 1e9)), "too many conns"]],
                        }
                    ]
                },
                metadata={"provider": "loki"},
            )
        }
    )
    seams(topology=lambda service: _topology_resolution(("mysql",)))
    pack = build(_request("logs", "topology"), now=NOW)

    (log_observation,) = pack.logs.observations
    assert log_observation.metadata["topology_relation"] == "dependency"


# --- identity, serialisation --------------------------------------------


def test_the_incident_identity_is_carried_through_verbatim(registry):
    registry()
    request = _request(
        "metrics",
        severity="Sev-1",
        alert_id="alert-9",
        alert_name="PaymentErrorRateHigh",
    )
    pack = build(request, now=NOW)

    assert pack.incident.service == SERVICE
    assert pack.incident.severity == "Sev-1"
    assert pack.incident.window_start == WINDOW_START
    assert pack.incident.window_end == WINDOW_END
    assert pack.incident.alert_id == "alert-9"
    assert pack.incident.alert_name == "PaymentErrorRateHigh"


def test_severity_defaults_to_unknown_rather_than_a_guessed_rung(registry):
    """Picking a rung on a ladder the caller never used would assert a grading nobody
    made."""
    registry()
    assert build(_request("metrics"), now=NOW).incident.severity == "unknown"


def test_the_context_round_trips_through_json(registry, seams):
    """It gets cached, persisted, logged and carried to an agent invoked over HTTP or
    MCP, so the boundary model has to survive the boundary.

    Compared as serialised forms rather than as models, because stage 3 writes
    ``sources_agreeing`` as a tuple into a plain-dict ``metadata`` field and JSON has no
    tuple — the correlator's docstring says so and tells consumers to compare contents,
    not types. Asserting model equality would therefore fail for a context that
    round-tripped perfectly, which is the wrong thing to pin.
    """
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW, profile="rca", max_tokens=100_000)
    dumped = pack.model_dump(mode="json")

    for revalidated in (
        IncidentContext.model_validate(dumped),
        IncidentContext.model_validate_json(pack.model_dump_json()),
    ):
        assert revalidated.model_dump(mode="json") == dumped
        assert revalidated.schema_version == pack.schema_version
        assert revalidated.built_at == pack.built_at
        assert revalidated.evidence_ranking == pack.evidence_ranking
        assert {name: s.status for name, s in revalidated.sections.items()} == {
            name: s.status for name, s in pack.sections.items()
        }
        assert [o.observation_id for o in revalidated.observations] == [
            o.observation_id for o in pack.observations
        ]


def test_every_observation_is_ranked_exactly_once(registry, seams):
    """Ranks are 1-based and contiguous so a consumer can treat ``rank <= n`` as "the
    top n" without checking for gaps."""
    registry()
    seams()
    pack = build(_request(*EVERY_SOURCE), now=NOW)

    ranks = [entry.rank for entry in pack.evidence_ranking]
    assert ranks == list(range(1, len(pack.observations) + 1))
    assert all(entry.rationale for entry in pack.evidence_ranking)
