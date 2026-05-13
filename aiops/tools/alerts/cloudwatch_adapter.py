"""Adapter: CloudWatch alarm event (SNS-wrapped JSON) → canonical Alert dict.

Stub implementation — maps fields from a typical CloudWatch alarm-state-
change message. The adapter expects the *parsed* alarm message (caller has
already unwrapped the SNS envelope).

Service resolution uses the first matching dimension name from a small
allow-list (``TargetGroup``, ``ServiceName``, ``ClusterName``,
``FunctionName``); falls back to ``"unknown"``.

Reference payload shape (excerpt)::

    {
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
        "ComparisonOperator": "GreaterThanThreshold"
      }
    }
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_SERVICE_DIMENSION_NAMES = ("TargetGroup", "ServiceName", "ClusterName", "FunctionName")
_STATE_TO_SEVERITY = {"ALARM": "critical", "INSUFFICIENT_DATA": "warning", "OK": "info"}
# CloudWatch's "Threshold Crossed: ... datapoint [12.0] greater than ..." form
_DATAPOINT_PATTERN = re.compile(r"\[([\d.]+)\]")


def _service_from_dimensions(dimensions: list[dict[str, Any]]) -> str:
    by_name = {d.get("name"): d.get("value") for d in dimensions or [] if isinstance(d, dict)}
    for name in _SERVICE_DIMENSION_NAMES:
        if by_name.get(name):
            return str(by_name[name])
    for v in by_name.values():
        if v:
            return str(v)
    return "unknown"


def to_canonical_alert(payload: dict[str, Any]) -> dict[str, Any]:
    trigger = payload.get("Trigger") or {}
    dimensions = trigger.get("Dimensions") or []
    service = _service_from_dimensions(dimensions)
    metric = trigger.get("MetricName") or payload.get("AlarmName") or "alert"
    # CloudWatch alarm events don't carry the breaching datapoint as a typed
    # field; extract it from NewStateReason on a best-effort basis.
    reason = str(payload.get("NewStateReason", ""))
    m = _DATAPOINT_PATTERN.search(reason)
    try:
        value = float(m.group(1)) if m else 0.0
    except ValueError:
        value = 0.0
    threshold = trigger.get("Threshold")
    try:
        threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        threshold = None
    state = payload.get("NewStateValue")
    severity_hint = _STATE_TO_SEVERITY.get(state) if isinstance(state, str) else None
    timestamp = payload.get("StateChangeTime") or datetime.now(UTC).isoformat()
    labels = {
        "alarm_name": str(payload.get("AlarmName", "")),
        "namespace": str(trigger.get("Namespace", "")),
        "region": str(payload.get("Region", "")),
        **{str(d.get("name")): str(d.get("value")) for d in dimensions if isinstance(d, dict)},
    }
    return {
        "alert_id": f"CW-{payload.get('AlarmName', 'UNKNOWN')}",
        "service": service,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "timestamp": str(timestamp),
        "source": "CloudWatch",
        "severity_hint": severity_hint,
        "labels": labels,
        "annotations": {
            "summary": str(payload.get("AlarmName", "")),
            "description": str(payload.get("AlarmDescription") or reason),
        },
    }
