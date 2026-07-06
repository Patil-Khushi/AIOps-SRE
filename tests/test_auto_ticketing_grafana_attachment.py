"""Tests for DEMO-8 / #60: Grafana panel render + attach in auto-ticketing.

Three behaviours pinned here:

1. Mapped alert + servicenow ticket -> render_panel + attachment.add are
   both called, and the attachment outcome lands in the audit metadata.
2. Unmapped alert -> render_panel is NOT called; audit records the skip.
3. Render failure (e.g. plugin not installed) -> attachment.add is NOT
   called; audit records the failure; ticket creation still reports
   success.

The tests stub the registry rather than reaching out to real Grafana /
ServiceNow — the rendering capability is exercised end-to-end in its
own provider tests; here we lock down the agent's orchestration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.alert_triage.models import AuditMetadata, TriageVerdict
from agents.auto_ticketing import agent as auto_ticketing_agent


def _verdict(severity: str = "Sev-1", service: str = "payment") -> TriageVerdict:
    return TriageVerdict(
        affected_service=service,
        severity=severity,  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary=f"{service} unhealthy",
        assigned_team="Payments Team",
        assigned_engineer=None,
        recommended_runbook=None,
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime.now(UTC),
            source_alerts=["ALT-1"],
            decision_trace=["received"],
        ),
    )


class _StubResult:
    """Minimal stand-in for ``aiops.tools.registry.ToolResult`` — just the
    attributes the auto-ticketing agent reads."""

    def __init__(
        self,
        ok: bool,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.ok = ok
        self.data = data
        self.error = error
        self.metadata = metadata or {}


class _StubRegistry:
    """Records every ``call`` so tests can assert which capabilities the
    agent invoked and in what order. Each capability maps to either a
    callable that returns a ``_StubResult`` or a fixed result."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, capability: str, **kwargs: Any) -> _StubResult:
        self.calls.append((capability, kwargs))
        resp = self._responses.get(capability)
        if resp is None:
            # Mimic a missing capability — the agent treats it as non-fatal.
            return _StubResult(ok=False, error=f"{capability} not registered")
        return resp(kwargs) if callable(resp) else resp


@pytest.fixture(autouse=True)
def _reset_panel_map_cache():
    """Each test gets a fresh lazy-load so monkeypatching the JSON path works."""
    auto_ticketing_agent._reload_panel_map_for_tests()
    yield
    auto_ticketing_agent._reload_panel_map_for_tests()


def _wire_registry(monkeypatch, registry: _StubRegistry) -> None:
    monkeypatch.setattr(auto_ticketing_agent, "get_registry", lambda: registry)


def test_mapped_alert_triggers_render_and_attach(monkeypatch):
    """Happy path: alert maps to a panel, render succeeds, attachment
    succeeds. Both capabilities should be called once."""
    create_resp = _StubResult(
        ok=True,
        data={"sys_id": "abc123", "number": "INC0010001"},
        metadata={"provider": "servicenow"},
    )
    render_resp = _StubResult(
        ok=True,
        data={
            "png_bytes": b"\x89PNG\r\n\x1a\nfake-pixels",
            "content_type": "image/png",
            "dashboard_uid": "otel-demo-services",
            "panel_id": 5,
        },
        metadata={"provider": "grafana"},
    )
    attach_resp = _StubResult(
        ok=True,
        data={
            "attachment_sys_id": "att-9",
            "file_name": "PaymentErrorRateHigh.png",
            "size_bytes": 15,
        },
    )
    registry = _StubRegistry(
        {
            "itsm.incident.create": create_resp,
            "observability.metrics.render_panel": render_resp,
            "itsm.incident.attachment.add": attach_resp,
            "notify.send": _StubResult(ok=True, data={"ok": True}),
        }
    )
    _wire_registry(monkeypatch, registry)

    record = auto_ticketing_agent.ticket(_verdict(), alert_name="PaymentErrorRateHigh")

    assert record.created
    assert record.ticket_id == "INC0010001"
    assert record.system == "servicenow"
    assert record.attachment_added is True

    capabilities_called = [c for c, _ in registry.calls]
    assert "observability.metrics.render_panel" in capabilities_called
    assert "itsm.incident.attachment.add" in capabilities_called

    render_kwargs = next(
        kw for cap, kw in registry.calls if cap == "observability.metrics.render_panel"
    )
    # Validated live 2026-07-06 (#60): demo alerts attach the OTel Collector
    # "Overview" as a full-dashboard kiosk render — no panel_id, sized 1000x860.
    assert render_kwargs["dashboard_uid"] == "otel-demo_otel-collector_dashboard"
    assert render_kwargs["panel_id"] is None
    assert render_kwargs["time_range"] == "30m"
    assert render_kwargs["width"] == 1000
    assert render_kwargs["height"] == 860
    assert render_kwargs["format"] == "png"

    attach_kwargs = next(kw for cap, kw in registry.calls if cap == "itsm.incident.attachment.add")
    assert attach_kwargs["sys_id"] == "abc123"
    assert attach_kwargs["file_name"] == "PaymentErrorRateHigh.png"
    assert attach_kwargs["content"] == b"\x89PNG\r\n\x1a\nfake-pixels"

    assert any("grafana panel attached" in line for line in record.audit_metadata)


def test_unmapped_alert_skips_render_and_attach(monkeypatch):
    """Most alerts won't have a panel mapped. The agent must skip both
    capabilities without raising, and record the skip in the audit."""
    create_resp = _StubResult(
        ok=True,
        data={"sys_id": "abc123", "number": "INC0010002"},
        metadata={"provider": "servicenow"},
    )
    registry = _StubRegistry(
        {
            "itsm.incident.create": create_resp,
            "notify.send": _StubResult(ok=True, data={"ok": True}),
        }
    )
    _wire_registry(monkeypatch, registry)

    record = auto_ticketing_agent.ticket(_verdict(), alert_name="SomeUnmappedAlert")

    assert record.created
    capabilities_called = [c for c, _ in registry.calls]
    assert "observability.metrics.render_panel" not in capabilities_called
    assert "itsm.incident.attachment.add" not in capabilities_called

    assert any(
        "no panel mapped for alert 'SomeUnmappedAlert'" in line for line in record.audit_metadata
    )
    assert record.attachment_added is False


def test_render_failure_does_not_break_ticket(monkeypatch):
    """If Grafana is unreachable or the image-renderer plugin is missing,
    render_panel returns ok=False. The agent must record the failure but
    still report ticket creation as a success — the attachment is a
    nice-to-have on top of an already-created ticket."""
    create_resp = _StubResult(
        ok=True,
        data={"sys_id": "abc123", "number": "INC0010003"},
        metadata={"provider": "servicenow"},
    )
    render_resp = _StubResult(
        ok=False,
        error="grafana-image-renderer plugin not installed",
    )
    registry = _StubRegistry(
        {
            "itsm.incident.create": create_resp,
            "observability.metrics.render_panel": render_resp,
            "notify.send": _StubResult(ok=True, data={"ok": True}),
        }
    )
    _wire_registry(monkeypatch, registry)

    record = auto_ticketing_agent.ticket(_verdict(), alert_name="PaymentErrorRateHigh")

    assert record.created
    assert record.ticket_id == "INC0010003"

    capabilities_called = [c for c, _ in registry.calls]
    assert "observability.metrics.render_panel" in capabilities_called
    # Attachment must NOT be attempted when render failed.
    assert "itsm.incident.attachment.add" not in capabilities_called

    assert any("render_panel failed" in line for line in record.audit_metadata)
    assert record.attachment_added is False


def test_alert_name_omitted_skips_attachment(monkeypatch):
    """Callers that don't supply alert_name (e.g. the eval harness)
    should still get a clean ticket; the agent records the skip."""
    create_resp = _StubResult(
        ok=True,
        data={"sys_id": "abc123", "number": "INC0010004"},
        metadata={"provider": "servicenow"},
    )
    registry = _StubRegistry(
        {
            "itsm.incident.create": create_resp,
            "notify.send": _StubResult(ok=True, data={"ok": True}),
        }
    )
    _wire_registry(monkeypatch, registry)

    record = auto_ticketing_agent.ticket(_verdict())  # no alert_name

    assert record.created
    capabilities_called = [c for c, _ in registry.calls]
    assert "observability.metrics.render_panel" not in capabilities_called
    assert any("alert_name not supplied" in line for line in record.audit_metadata)
    assert record.attachment_added is False


def test_attachment_filename_is_sanitized():
    """Path separators and other shell-hostile chars in an alert name must
    not survive into the attachment filename. The sanitizer rewrites them
    to underscores and forbids leading dots."""
    sanitize = auto_ticketing_agent._safe_attachment_filename
    assert sanitize("PaymentErrorRateHigh") == "PaymentErrorRateHigh.png"
    assert sanitize("foo/bar") == "foo_bar.png"
    # ``..`` becomes ``..``, ``/`` becomes ``_``, then leading dots strip.
    assert sanitize("../etc/passwd") == "_etc_passwd.png"
    assert sanitize(".hidden") == "hidden.png"
    assert sanitize("with space") == "with_space.png"
    # Pure-garbage names still produce a usable file.
    assert sanitize("/////") == "alert.png"


def test_mock_provider_ticket_skips_grafana_path(monkeypatch):
    """When the mock ITSM is active (provider=mock), the resulting ticket
    has no real ServiceNow sys_id to attach to. The agent should skip the
    Grafana path entirely rather than fail trying to attach to a fake id."""
    create_resp = _StubResult(
        ok=True,
        data={"id": "MOCK-001"},  # mock returns ``id``, no sys_id
        metadata={"provider": "mock"},
    )
    registry = _StubRegistry(
        {
            "itsm.incident.create": create_resp,
            "notify.send": _StubResult(ok=True, data={"ok": True}),
        }
    )
    _wire_registry(monkeypatch, registry)

    record = auto_ticketing_agent.ticket(_verdict(), alert_name="PaymentErrorRateHigh")

    assert record.created
    assert record.system == "mock"
    assert record.attachment_added is False
    capabilities_called = [c for c, _ in registry.calls]
    assert "observability.metrics.render_panel" not in capabilities_called
    assert "itsm.incident.attachment.add" not in capabilities_called
