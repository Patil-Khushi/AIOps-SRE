"""Endpoint-level tests for ``POST /api/triage``.

After INFRA-2 (#74) the RA-001→RA-005 chain lives in
``aiops.runtime.orchestrator.run_reactive_flow``. The endpoint's remaining
responsibilities are narrow and are what these tests pin:

1. Construct an ``Alert`` from the request body and map a bad payload to 400.
2. Delegate to the orchestrator and return its ``to_api_dict()`` verbatim.

The pipeline behaviors that used to be asserted here (the #84 RoutingOutcome
unwrap, the deliveries sibling key, the Suppressed/empty-deliveries case) now
live in ``tests/test_orchestrator_reactive_flow.py``, which owns them at the
layer that implements them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from demo.ui import server as srv


def _alert_payload() -> dict[str, Any]:
    return {
        "alert_id": "ALT-OUTCOME-1",
        "service": "payment",
        "metric": "ErrorRateHigh",
        "value": 0.42,
        "threshold": 0.1,
        "timestamp": "2026-05-26T10:00:00Z",
        "source": "Prometheus",
        "labels": {},
        "annotations": {},
    }


class _FakeFlowResult:
    """Stand-in for ``ReactiveFlowResult``: the endpoint calls ``to_api_dict()``
    for the response and reads ``.war_room`` / ``.routing`` / ``.verdict`` to
    record the incident feed row. With ``war_room=None`` that recording is a
    clean no-op, so this test pins only the delegation contract."""

    SENTINEL: ClassVar[dict[str, Any]] = {
        "verdict": {"affected_service": "payment"},
        "persisted": {"verdict_id": 7},
    }

    # The combined Notification Assembler runs inside the flow now; the endpoint
    # only records what it returns. war_room=None → nothing to record.
    verdict: ClassVar = srv.TriageVerdict(
        affected_service="payment",
        severity="Sev-4",
        confidence_score=0.9,
        alert_summary="n/a",
        assigned_team="Payments Team",
        status="Active",
        audit_metadata=srv.AuditMetadata(
            created_at=datetime(2026, 1, 1, tzinfo=UTC), created_by="test"
        ),
    )
    war_room: ClassVar = None
    routing: ClassVar = None

    def to_api_dict(self) -> dict[str, Any]:
        return self.SENTINEL


def test_triage_alert_delegates_to_orchestrator(monkeypatch):
    """The handler builds an Alert and returns the orchestrator result's
    ``to_api_dict()`` unchanged."""
    captured: dict[str, Any] = {}

    def _fake_run(alert):
        captured["alert"] = alert
        return _FakeFlowResult()

    monkeypatch.setattr(srv, "run_reactive_flow", _fake_run)

    response = srv.triage_alert(srv.TriageRequest(alert=_alert_payload()))

    # Alert was constructed from the body and handed to the orchestrator.
    assert captured["alert"].alert_id == "ALT-OUTCOME-1"
    assert captured["alert"].service == "payment"
    # Response is the orchestrator result's dict, passed through untouched.
    assert response is _FakeFlowResult.SENTINEL


def test_triage_alert_maps_invalid_alert_to_400(monkeypatch):
    """A malformed alert payload becomes a 400 before the orchestrator runs."""
    called = False

    def _fake_run(_alert):
        nonlocal called
        called = True
        return _FakeFlowResult()

    monkeypatch.setattr(srv, "run_reactive_flow", _fake_run)

    # Missing required fields (service / metric / value / timestamp).
    with pytest.raises(srv.HTTPException) as exc_info:
        srv.triage_alert(srv.TriageRequest(alert={"alert_id": "X"}))

    assert exc_info.value.status_code == 400
    assert "invalid alert" in str(exc_info.value.detail)
    assert called is False  # short-circuited before delegation
