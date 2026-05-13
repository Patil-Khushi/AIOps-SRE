"""Unit tests for the Datadog → canonical Alert adapter."""

from __future__ import annotations

from agents.alert_triage.models import Alert
from aiops.tools.alerts.datadog_adapter import to_canonical_alert

_SAMPLE = {
    "id": "5234829",
    "title": "[Triggered] High error rate on payment",
    "body": "Error rate 0.42 above threshold 0.20",
    "priority": "P1",
    "alert_type": "error",
    "date": 1715600000,
    "tags": ["service:payment", "env:prod", "team:payments", "no-colon-tag"],
    "metric": "trace.http.request.errors",
    "value": 0.42,
}


def test_happy_path_full_payload():
    out = to_canonical_alert(_SAMPLE)
    assert out["alert_id"] == "DD-5234829"
    assert out["service"] == "payment"
    assert out["metric"] == "trace.http.request.errors"
    assert out["value"] == 0.42
    assert out["source"] == "Datadog"
    assert out["severity_hint"] == "critical"
    assert out["labels"]["env"] == "prod"
    assert out["labels"]["team"] == "payments"
    assert out["labels"]["no-colon-tag"] == ""
    assert out["annotations"]["summary"].startswith("[Triggered]")
    # epoch → ISO conversion (1715600000 is 2024-05-13T13:13:20+00:00)
    assert out["timestamp"].endswith("+00:00")


def test_service_falls_back_to_unknown():
    out = to_canonical_alert({"id": "x", "tags": ["env:prod"], "metric": "m", "value": 0})
    assert out["service"] == "unknown"


def test_output_constructs_canonical_alert():
    """Adapter output must satisfy the Alert Pydantic contract."""
    out = to_canonical_alert(_SAMPLE)
    alert = Alert(**out)
    assert alert.service == "payment"
    assert alert.source == "Datadog"
