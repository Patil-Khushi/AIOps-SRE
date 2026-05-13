"""Unit tests for the Prometheus → canonical Alert adapter."""

from __future__ import annotations

from aiops.tools.alerts.prometheus_adapter import to_canonical_alert


def test_happy_path_full_payload():
    prom_alert = {
        "labels": {
            "alertname": "PaymentErrorRateHigh",
            "service": "payment",
            "severity": "critical",
            "instance": "10.0.0.5:9090",
        },
        "annotations": {
            "summary": "Payment 5xx rate above threshold",
            "description": "rate=0.4/s",
        },
        "value": "0.42",
        "activeAt": "2026-05-13T09:15:00Z",
    }
    out = to_canonical_alert(prom_alert)
    assert out["alert_id"] == "PROM-PaymentErrorRateHigh-10.0.0.5:9090"
    assert out["service"] == "payment"
    assert out["metric"] == "PaymentErrorRateHigh"
    assert out["value"] == 0.42
    assert out["source"] == "Prometheus"
    assert out["severity_hint"] == "critical"
    assert out["timestamp"] == "2026-05-13T09:15:00Z"
    assert out["labels"]["alertname"] == "PaymentErrorRateHigh"
    assert out["annotations"]["description"] == "rate=0.4/s"


def test_service_fallback_chain():
    # labels.service missing → labels.service_name wins
    out1 = to_canonical_alert({"labels": {"service_name": "cart", "alertname": "X"}})
    assert out1["service"] == "cart"

    # labels.service and service_name missing → labels.job wins
    out2 = to_canonical_alert({"labels": {"job": "otel-demo/ad", "alertname": "X"}})
    assert out2["service"] == "otel-demo/ad"

    # All label paths missing → annotations.service wins
    out3 = to_canonical_alert({
        "labels": {"alertname": "X"},
        "annotations": {"service": "checkout"},
    })
    assert out3["service"] == "checkout"

    # Nothing matches → "unknown" sentinel
    out4 = to_canonical_alert({"labels": {"alertname": "X"}})
    assert out4["service"] == "unknown"


def test_metric_falls_back_when_alertname_missing():
    # No alertname → __name__ wins
    out1 = to_canonical_alert({"labels": {"__name__": "http_requests_total"}})
    assert out1["metric"] == "http_requests_total"

    # No alertname or __name__ → annotations.summary wins
    out2 = to_canonical_alert({
        "labels": {"service": "ad"},
        "annotations": {"summary": "Ad service latency high"},
    })
    assert out2["metric"] == "Ad service latency high"

    # Nothing → "alert" sentinel
    out3 = to_canonical_alert({"labels": {"service": "ad"}})
    assert out3["metric"] == "alert"


def test_value_coercion_handles_string_missing_and_invalid():
    # String → float
    assert to_canonical_alert({"labels": {}, "value": "5.5"})["value"] == 5.5

    # Missing → 0.0 (default from value_str = "0")
    assert to_canonical_alert({"labels": {}})["value"] == 0.0

    # Invalid string → 0.0 (defensive coercion)
    assert to_canonical_alert({"labels": {}, "value": "not-a-number"})["value"] == 0.0

    # Value sourced from labels when top-level absent
    assert to_canonical_alert({"labels": {"value": "12.3"}})["value"] == 12.3


def test_label_values_are_stringified():
    """Some Prometheus labels (e.g. http_status_code) come in as ints. The
    canonical Alert.labels is ``dict[str, str]``, so the adapter must coerce."""
    out = to_canonical_alert({
        "labels": {"alertname": "X", "http_status_code": 500, "instance": 1},
    })
    assert out["labels"]["http_status_code"] == "500"
    assert out["labels"]["instance"] == "1"
