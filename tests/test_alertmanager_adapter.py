"""Unit tests for the Alertmanager → canonical Alert adapter."""

from __future__ import annotations

from agents.alert_triage.models import Alert
from aiops.tools.alerts.alertmanager_adapter import to_canonical_alert


_SAMPLE = {
    "status": "firing",
    "labels": {
        "alertname": "PaymentErrorRateHigh",
        "service": "payment",
        "severity": "critical",
        "instance": "10.0.0.5:9090",
    },
    "annotations": {
        "summary": "Payment 5xx high",
        "description": "rate=0.4/s",
    },
    "startsAt": "2026-05-13T10:00:00.000Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "generatorURL": "http://prometheus:9090/graph?...",
}


def test_happy_path_full_payload():
    out = to_canonical_alert(_SAMPLE)
    assert out["alert_id"] == "AM-PaymentErrorRateHigh-10.0.0.5:9090"
    assert out["service"] == "payment"
    assert out["metric"] == "PaymentErrorRateHigh"
    assert out["source"] == "Alertmanager"
    assert out["severity_hint"] == "critical"
    assert out["timestamp"] == "2026-05-13T10:00:00.000Z"
    assert out["labels"]["alertname"] == "PaymentErrorRateHigh"


def test_service_fallback_to_job_label():
    out = to_canonical_alert({
        "labels": {"alertname": "X", "job": "otel-demo/cart"},
    })
    assert out["service"] == "otel-demo/cart"


def test_output_constructs_canonical_alert():
    out = to_canonical_alert(_SAMPLE)
    alert = Alert(**out)
    assert alert.service == "payment"
    assert alert.source == "Alertmanager"
    assert alert.severity_hint == "critical"
