"""Parity for the smallest seam in the Context Engineering Layer migration:
``_context_item``'s two live-telemetry rows in the war-room context pack.

Byte-identity here means ``str(result.data)`` — the same Python ``repr`` a
fresh registry call would have produced — because that string is posted
verbatim into the Slack war-room body and the JSONL audit log.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agents.alert_triage import AuditMetadata, TriageVerdict
from agents.notification_assembler.agent import _build_context_pack
from agents.notification_assembler.context_adapter import (
    build_context_request_specs,
    context_pack_items_from_context,
)
from aiops.context.builder import ContextBuilder, ContextRequest
from aiops.tools.registry import ToolResult

SERVICE = "checkout-service"
NOW = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)


class _FakeRegistry:
    def __init__(self, *, metrics_ok: bool = True, traces_ok: bool = True) -> None:
        self.calls: list[str] = []
        self._metrics_ok = metrics_ok
        self._traces_ok = traces_ok

    def call(self, capability: str, **kwargs) -> ToolResult:
        self.calls.append(capability)
        if capability == "observability.metrics.query":
            if not self._metrics_ok:
                return ToolResult(ok=False, error="down", metadata={})
            return ToolResult(
                ok=True,
                data={
                    "query": kwargs["promql"],
                    "results": [{"metric": {}, "value": [1.0, "3.5"]}],
                },
                metadata={"provider": "prometheus"},
            )
        if capability == "observability.traces.search":
            if not self._traces_ok:
                return ToolResult(ok=False, error="down", metadata={})
            return ToolResult(
                ok=True,
                data={"service": SERVICE, "trace_count": 2, "traces": [{"trace_id": "t-1"}]},
                metadata={"provider": "jaeger"},
            )
        return ToolResult(ok=False, error="nope", metadata={"missing_provider": True})


def _verdict() -> TriageVerdict:
    return TriageVerdict(
        affected_service=SERVICE,
        severity="Sev-2",
        confidence_score=0.9,
        alert_summary="high error rate",
        assigned_team="checkout-oncall",
        assigned_engineer="oncall@checkout.example.com",
        status="Active",
        audit_metadata=AuditMetadata(created_at=NOW),
    )


def _build_context(registry) -> dict:
    request = ContextRequest(
        service=SERVICE,
        window_start=NOW,
        window_end=NOW,
        specs=build_context_request_specs(SERVICE),
    )
    from unittest.mock import patch

    with patch("aiops.context.collectors.base.get_registry", lambda: registry):
        ctx = ContextBuilder().build(request, now=NOW)
    return ctx.model_dump(mode="json")


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr("agents.notification_assembler.agent.get_registry", lambda: registry)
    return registry


def test_context_pack_is_byte_identical_to_legacy(fake_registry):
    verdict = _verdict()
    legacy = _build_context_pack(verdict)
    context = _build_context(fake_registry)
    from_context = _build_context_pack(verdict, context)

    assert [item.model_dump() for item in from_context] == [item.model_dump() for item in legacy]


def test_a_failed_lookup_renders_as_the_same_unavailable_string(monkeypatch):
    """No per-category live fallback here — see the adapter's module docstring.
    An unavailable section renders exactly the single string legacy always used."""
    verdict = _verdict()
    legacy_registry = _FakeRegistry(metrics_ok=False, traces_ok=False)
    monkeypatch.setattr("agents.notification_assembler.agent.get_registry", lambda: legacy_registry)
    legacy = _build_context_pack(verdict)

    context = _build_context(_FakeRegistry(metrics_ok=False, traces_ok=False))
    from_context = _build_context_pack(verdict, context)

    assert [item.model_dump() for item in from_context] == [item.model_dump() for item in legacy]
    assert all(item.value == "unavailable" for item in from_context[-2:])


def test_no_context_reproduces_the_two_live_calls(fake_registry):
    """The default path today: context is None, nothing changes."""
    verdict = _verdict()
    _build_context_pack(verdict, None)
    assert fake_registry.calls == ["observability.metrics.query", "observability.traces.search"]


def test_context_present_but_flag_off_still_makes_the_two_live_calls(fake_registry, monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    verdict = _verdict()
    context = _build_context(_FakeRegistry())

    fake_registry.calls.clear()
    _build_context_pack(verdict, context)
    assert fake_registry.calls == ["observability.metrics.query", "observability.traces.search"]


def test_context_present_and_flag_on_makes_zero_live_calls(fake_registry, monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    verdict = _verdict()
    context = _build_context(_FakeRegistry())

    fake_registry.calls.clear()
    _build_context_pack(verdict, context)
    assert fake_registry.calls == [], (
        "the whole point: the shared context must save the round-trips"
    )


def test_context_pack_item_names_and_field_names_are_unchanged():
    """Pins the dashboard/Slack-visible contract this migration must not touch."""
    from agents.notification_assembler.models import ContextPackItem

    assert set(ContextPackItem.model_fields) == {"label", "value", "source"}


def test_neither_section_requested_falls_back_to_none_not_two_unavailables():
    """An incident-commander-orchestrated build that never asked for these two
    sections must look like "no context available" to the caller, not like
    "both live calls already failed" — the two are different fallback triggers."""
    from aiops.context.models import SectionStatus
    from aiops.context.pack import IncidentContext

    built = IncidentContext.model_validate(_build_context(_FakeRegistry()))
    # SectionStatus enum member, not the bare string: model_copy(update=...) does
    # not re-validate, so a plain "not_requested" string would silently bypass
    # the StrEnum coercion pydantic applies at construction and blow up on the
    # first .usable access instead of exercising the case under test.
    ctx = built.model_copy(
        update={
            "metrics": built.metrics.model_copy(
                update={"status": SectionStatus.NOT_REQUESTED, "raw": None}
            ),
            "traces": built.traces.model_copy(
                update={"status": SectionStatus.NOT_REQUESTED, "raw": None}
            ),
        }
    )
    assert context_pack_items_from_context(ctx, SERVICE) is None
