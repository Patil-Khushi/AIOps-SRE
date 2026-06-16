"""DEMO-1 / #53 — ServiceNow ``cmdb_lookup`` falls back to the demo CMDB table
when the real PDI returns no match, so the OpenTelemetry Astronomy Shop demo
flow keeps routing to the right team + runbook even on a stock PDI.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiops.tools.itsm import _demo_cmdb, servicenow


class _StubResponse:
    """Minimal httpx-Response-like object for ``servicenow._request``."""

    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _force_real_servicenow(monkeypatch):
    """Configure ``servicenow.cmdb_lookup`` to act as if PDI creds are set so
    ``_request`` reaches the (mocked) HTTP layer instead of returning the
    'not configured' short-circuit."""
    monkeypatch.setenv("AIOPS_SERVICENOW_INSTANCE_URL", "https://example.service-now.com")
    monkeypatch.setenv("AIOPS_SERVICENOW_USER", "admin")
    monkeypatch.setenv("AIOPS_SERVICENOW_PASSWORD", "pw")
    yield


def _stub_httpx(monkeypatch, *, results: list[dict[str, Any]]):
    """Patch ``httpx.request`` so it returns a 200 with the given ``results``
    list under the ServiceNow ``result`` key."""

    def fake_request(*_args: Any, **_kwargs: Any) -> _StubResponse:
        return _StubResponse(200, {"result": results})

    monkeypatch.setattr(servicenow.httpx, "request", fake_request)


def test_real_cmdb_hit_returns_servicenow_data(monkeypatch):
    """When the PDI has a row for the service, we use it verbatim — no fallback."""
    _stub_httpx(
        monkeypatch,
        results=[
            {
                "name": "payment-api",
                "support_group": {"display_value": "Real Payments Squad"},
                "sys_class_name": "cmdb_ci_service",
                "u_runbook_url": "https://real-pdi.example.com/payment",
            }
        ],
    )
    result = servicenow.cmdb_lookup("payment")
    assert result.ok
    assert result.data is not None
    assert result.data["team"] == "Real Payments Squad"
    assert result.data["runbook"] == "https://real-pdi.example.com/payment"
    assert result.metadata["matched"] is True
    assert "fallback" not in result.metadata, "fallback marker leaked on a real hit"


def test_real_cmdb_miss_falls_back_to_demo_table(monkeypatch):
    """The headline #53 case: PDI returns nothing for ``payment`` (no CI row),
    so we expose the demo table's mapping with a clear ``fallback`` marker."""
    _stub_httpx(monkeypatch, results=[])
    result = servicenow.cmdb_lookup("payment")
    assert result.ok
    assert result.data is not None
    assert result.data["team"] == "Payments Team"
    assert "runbooks.example.com/payment" in (result.data["runbook"] or "")
    assert result.metadata["matched"] is True
    assert result.metadata["fallback"] == "demo_cmdb"


def test_real_cmdb_miss_and_demo_miss_returns_none(monkeypatch):
    """Service unknown in both real CMDB and demo table — keep the agent's
    existing 'Platform On-Call' default path working (``data=None``)."""
    _stub_httpx(monkeypatch, results=[])
    result = servicenow.cmdb_lookup("weather-forecast-service")
    assert result.ok
    assert result.data is None
    assert result.metadata["matched"] is False
    assert result.metadata["fallback"] == "demo_cmdb"


def test_cmdb_row_with_empty_team_falls_back_to_demo(monkeypatch):
    """A short service name like ``ad`` can LIKE-match an unrelated CI that
    has no support_group. Such a row is useless (it would collapse the agent
    to its Platform On-Call default), so we defer to the demo table — ``ad``
    must route to Ads Team, not the wildcard escalation."""
    _stub_httpx(
        monkeypatch,
        results=[
            {"name": "admin-portal", "support_group": "", "sys_class_name": "cmdb_ci_service"}
        ],
    )
    result = servicenow.cmdb_lookup("ad")
    assert result.ok
    assert result.data is not None
    assert result.data["team"] == "Ads Team"
    assert result.metadata["fallback"] == "demo_cmdb"


def test_cmdb_error_falls_back_to_demo(monkeypatch):
    """When ServiceNow is unreachable / not configured (``res`` not ok), the
    lookup still resolves via the demo table instead of returning an error
    that collapses the agent to its 'Platform On-Call' default (which routes
    every service to the global wildcard escalation engineer)."""
    monkeypatch.delenv("AIOPS_SERVICENOW_INSTANCE_URL", raising=False)
    monkeypatch.delenv("AIOPS_SERVICENOW_USER", raising=False)
    monkeypatch.delenv("AIOPS_SERVICENOW_PASSWORD", raising=False)
    result = servicenow.cmdb_lookup("ad")
    assert result.ok
    assert result.data is not None
    assert result.data["team"] == "Ads Team"
    assert result.metadata["fallback"] == "demo_cmdb"


def test_demo_cmdb_lookup_returns_none_for_unknown_service():
    """Direct unit test of the demo-table helper: unknown service -> None."""
    assert _demo_cmdb.lookup("weather-forecast-service") is None
    assert _demo_cmdb.lookup("") is None
    assert _demo_cmdb.lookup("   ") is None


def test_demo_cmdb_lookup_normalizes_case_and_whitespace():
    """Direct unit test: helper lowercases + strips so callers don't have to."""
    info = _demo_cmdb.lookup("  PAYMENT  ")
    assert info is not None
    assert info["team"] == "Payments Team"
