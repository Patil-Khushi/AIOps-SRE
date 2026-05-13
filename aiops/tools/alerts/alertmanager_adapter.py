"""Adapter: Prometheus Alertmanager webhook (one inner alert) → canonical Alert dict.

Stub implementation. Alertmanager's webhook envelope contains a batch under
``alerts``; callers iterate the batch and invoke this adapter on each inner
alert. The inner alert shape is structurally similar to a Prometheus
``/api/v1/alerts`` entry (handled by ``prometheus_adapter``), but kept
separate so future divergences (e.g. ``startsAt`` vs ``activeAt``,
``generatorURL`` enrichment) land in the right module.

Reference inner-alert shape (excerpt)::

    {
      "status": "firing",
      "labels": {"alertname": "PaymentErrorRateHigh", "service": "payment",
                 "severity": "critical", "instance": "10.0.0.5:9090"},
      "annotations": {"summary": "Payment 5xx high", "description": "..."},
      "startsAt": "2026-05-13T10:00:00.000Z",
      "endsAt":   "0001-01-01T00:00:00Z",
      "generatorURL": "http://prometheus:9090/graph?..."
    }
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def to_canonical_alert(inner_alert: dict[str, Any]) -> dict[str, Any]:
    labels = inner_alert.get("labels", {}) or {}
    annotations = inner_alert.get("annotations", {}) or {}
    service = (
        labels.get("service")
        or labels.get("service_name")
        or labels.get("job")
        or annotations.get("service")
        or "unknown"
    )
    metric = labels.get("alertname") or annotations.get("summary") or "alert"
    value_raw = labels.get("value") or annotations.get("value") or "0"
    try:
        value = float(value_raw)
    except (TypeError, ValueError):
        value = 0.0
    timestamp = inner_alert.get("startsAt") or datetime.now(UTC).isoformat()
    return {
        "alert_id": f"AM-{labels.get('alertname', 'UNKNOWN')}-{labels.get('instance', 'na')}",
        "service": service,
        "metric": metric,
        "value": value,
        "timestamp": timestamp,
        "source": "Alertmanager",
        "severity_hint": labels.get("severity"),
        "labels": {k: str(v) for k, v in labels.items()},
        "annotations": {k: str(v) for k, v in annotations.items()},
    }
