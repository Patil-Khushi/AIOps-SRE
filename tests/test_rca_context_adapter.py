"""Parity: RCA's context-derived evidence must be byte-identical to its legacy
evidence, given the same underlying data.

This is the test that makes "migrating RCA to the Context Engineering Layer does
not change RCA's reasoning" a CI-enforced claim rather than a review promise. It
works because ``agents/rca_agent/evidence.py`` exposes a dependency-injection seam
(``Backend``) that both paths run through — the live registry on one side, an
``IncidentContext`` on the other — so everything downstream (floors, the NaN guard,
key insertion order, every format string in ``render()``) is the *same code*,
not a second copy that could drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.rca_agent import evidence as _evidence
from agents.rca_agent.context_adapter import (
    ALWAYS_KEYS,
    ContextBackend,
    build_context_request_specs,
    evidence_from_context,
)
from aiops.context.builder import ContextBuilder, ContextRequest
from aiops.context.denylist import ContextDenylistError
from aiops.tools.registry import ToolResult

WINDOW_END = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
WINDOW_START = WINDOW_END - timedelta(minutes=15)
SERVICE = "payment-service"


class _FakeRegistry:
    """One fixed answer set, shared by both the live path and the context build.

    This is what makes the comparison meaningful: both sides are asked the exact
    same questions of the exact same backend, so a divergence in the result can
    only come from the adapter, never from the fixture drifting between runs.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        promqls = _evidence.required_promql_queries()
        # A handful of the ~12 required queries get real rows so both the
        # "found something" and "checked and empty" paths are exercised; the rest
        # answer with an empty result set.
        self._rows: dict[str, list[dict]] = {
            promqls[0]: [{"metric": {}, "value": [1.0, "0"]}],  # dependency gauge: DOWN
            "sum by (reason) (rate(orders_failed_total[5m]))": [
                {"metric": {"reason": "db_error"}, "value": [1.0, "0.42"]}
            ],
            'kube_pod_container_status_restarts_total{namespace="ecommerce"}': [
                {"metric": {"pod": "payment-service-abc12"}, "value": [1.0, "3"]}
            ],
        }

    def call(self, capability: str, **kwargs) -> ToolResult:
        self.calls.append(capability)
        if capability == "observability.metrics.query":
            return ToolResult(
                ok=True,
                data={"results": self._rows.get(kwargs["promql"], [])},
                metadata={"provider": "prometheus"},
            )
        if capability == "observability.metrics.alerts":
            return ToolResult(
                ok=True,
                data={
                    "alerts": [
                        {
                            "labels": {"alertname": "EcommerceServiceDown", "severity": "critical"},
                            "state": "firing",
                        }
                    ]
                },
                metadata={"provider": "prometheus"},
            )
        if capability == "observability.logs.query":
            return ToolResult(
                ok=True,
                data={
                    "streams": [
                        {
                            "stream": {"level": "error", "service_name": SERVICE},
                            "values": [[1754827800000000000, "mysql connection timed out"]],
                        }
                    ]
                },
                metadata={"provider": "loki"},
            )
        return ToolResult(ok=False, error="nope", metadata={"missing_provider": True})


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    # Both the legacy evidence.py path and the context collectors call
    # get_registry() from their own module namespace — patch both so the two
    # sides genuinely share one backend.
    monkeypatch.setattr("agents.rca_agent.evidence.get_registry", lambda: registry)
    monkeypatch.setattr("aiops.context.collectors.base.get_registry", lambda: registry)
    return registry


def _build_context(fake_registry):
    request = ContextRequest(
        service=SERVICE,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        specs=build_context_request_specs(
            SERVICE, window_start=WINDOW_START, window_end=WINDOW_END
        ),
    )
    return ContextBuilder().build(request, now=WINDOW_END)


def test_context_derived_evidence_is_byte_identical_to_legacy(fake_registry):
    legacy = _evidence.gather(SERVICE)
    ctx = _build_context(fake_registry)
    from_context = evidence_from_context(ctx, SERVICE)

    assert from_context == legacy, f"\nlegacy:  {legacy}\ncontext: {from_context}"
    # Insertion order matters too — agent.py joins observed.items() verbatim into
    # a dashboard-visible decision-trace line.
    assert list(from_context) == list(legacy)


def test_render_output_is_identical_either_way(fake_registry):
    legacy = _evidence.gather(SERVICE)
    ctx = _build_context(fake_registry)
    from_context = evidence_from_context(ctx, SERVICE)

    assert _evidence.render(legacy) == _evidence.render(from_context)


def test_a_category_queried_and_genuinely_empty_stays_a_none_line(fake_registry):
    """Distinguishes 'checked, nothing there' from 'never checked' — the whole
    reason evidence_from_context does a per-category fallback at all."""
    ctx = _build_context(fake_registry)
    from_context = evidence_from_context(ctx, SERVICE)
    rendered = _evidence.render(from_context)

    # latency is not in _FakeRegistry's populated rows, so every latency query
    # returned []; the category is genuinely absent, not unavailable.
    assert "latency" not in from_context
    assert "no order failures" not in rendered  # error_breakdown WAS populated


def test_an_unavailable_metrics_section_falls_back_per_category_not_wholesale(monkeypatch):
    """The mitigation this adapter exists for: a whole-backend outage must not
    silently render "NONE" for the four always-keys as though they were checked.
    """

    class _DeadRegistry:
        def call(self, capability, **kwargs):
            return ToolResult(ok=False, error="connection refused", metadata={})

    dead = _DeadRegistry()
    monkeypatch.setattr("aiops.context.collectors.base.get_registry", lambda: dead)
    ctx = _build_context(dead)
    assert not ctx.metrics.status.usable

    # Now point the LIVE fallback at data, proving the adapter actually re-queries
    # rather than accepting the empty context evidence as final.
    live = _FakeRegistry()
    monkeypatch.setattr("agents.rca_agent.evidence.get_registry", lambda: live)

    from_context = evidence_from_context(ctx, SERVICE)

    assert ALWAYS_KEYS & from_context.keys(), "always-keys must be re-queried, not rendered NONE"
    assert "observability.metrics.query" in live.calls, "the per-category fallback never fired"


def test_recent_changes_always_goes_live_never_through_context(fake_registry):
    """The one deliberately-not-migrated category. ContextBackend.commits must
    reach the registry directly regardless of what the context otherwise has."""
    ctx = _build_context(fake_registry)
    backend = ContextBackend(ctx)

    fake_registry.calls.clear()
    backend.commits(path="demo/ecommerce/payment-service", limit=5)
    assert "scm.commit.history" in fake_registry.calls


def test_context_backend_never_touches_the_denylist():
    """A defensive check that this adapter cannot become a path to a mutation:
    every spec it builds names a read-only source, so none can ever resolve to a
    denylisted capability no matter what AIOPS_CONTEXT_COLLECTORS is set to."""
    from aiops.context.denylist import ensure_allowed

    for spec in build_context_request_specs(
        SERVICE, window_start=WINDOW_START, window_end=WINDOW_END
    ):
        try:
            ensure_allowed(spec.capability or "observability.metrics.query")
        except ContextDenylistError:
            pytest.fail(f"RCA's own context request touched a denied capability: {spec}")


def test_no_context_and_flag_off_reproduces_gather_exactly(fake_registry):
    """The default path today: no context is ever built, nothing changes.

    ``_observe`` now returns an ``_Observation`` — the evidence plus the backend the
    investigation stages should read their facts from, so the prompt and the evidence
    matrix describe the same readings. The evidence itself is still byte-identical to a
    plain ``gather``, which is what this test exists to pin.
    """
    from agents.rca_agent.agent import _observe

    trace: list[str] = []
    observation = _observe(SERVICE, None, trace)
    assert observation.observed == _evidence.gather(SERVICE)
    # No Context Pack, so availability is unknown and must be inferred rather than
    # asserted — see ``investigation/facts.py``.
    assert observation.metrics_available is None
    assert observation.logs_available is None
