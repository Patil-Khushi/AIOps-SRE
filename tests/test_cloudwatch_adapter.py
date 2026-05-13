"""Unit tests for the CloudWatch → canonical Alert adapter."""

from __future__ import annotations

from agents.alert_triage.models import Alert
from aiops.tools.alerts.cloudwatch_adapter import to_canonical_alert

_SAMPLE = {
    "AlarmName": "PaymentErrorRateHigh",
    "AlarmDescription": "5xx errors above 5/min for 2 minutes",
    "NewStateValue": "ALARM",
    "NewStateReason": "Threshold Crossed: 1 datapoint [12.0] greater than 5.0",
    "StateChangeTime": "2026-05-13T10:00:00.000+0000",
    "Region": "us-east-1",
    "Trigger": {
        "MetricName": "5XXError",
        "Namespace": "AWS/ApplicationELB",
        "Dimensions": [{"name": "TargetGroup", "value": "payment-tg"}],
        "Threshold": 5.0,
        "ComparisonOperator": "GreaterThanThreshold",
    },
}


def test_happy_path_full_payload():
    out = to_canonical_alert(_SAMPLE)
    assert out["alert_id"] == "CW-PaymentErrorRateHigh"
    assert out["service"] == "payment-tg"
    assert out["metric"] == "5XXError"
    assert out["value"] == 12.0
    assert out["threshold"] == 5.0
    assert out["source"] == "CloudWatch"
    assert out["severity_hint"] == "critical"
    assert out["labels"]["namespace"] == "AWS/ApplicationELB"
    assert out["labels"]["TargetGroup"] == "payment-tg"


def test_service_uses_first_known_dimension_then_falls_back():
    # Unknown dimension name → uses the first non-empty value
    out_first = to_canonical_alert(
        {
            "AlarmName": "X",
            "Trigger": {"MetricName": "m", "Dimensions": [{"name": "Custom", "value": "abc"}]},
        }
    )
    assert out_first["service"] == "abc"

    # No dimensions at all → "unknown"
    out_missing = to_canonical_alert(
        {
            "AlarmName": "X",
            "Trigger": {"MetricName": "m", "Dimensions": []},
        }
    )
    assert out_missing["service"] == "unknown"


def test_datapoint_extraction_handles_missing_reason():
    out = to_canonical_alert(
        {
            "AlarmName": "X",
            "NewStateReason": "no bracketed number here",
            "Trigger": {"MetricName": "m", "Dimensions": []},
        }
    )
    assert out["value"] == 0.0


def test_output_constructs_canonical_alert():
    out = to_canonical_alert(_SAMPLE)
    alert = Alert(**out)
    assert alert.service == "payment-tg"
    assert alert.source == "CloudWatch"
    assert alert.threshold == 5.0
