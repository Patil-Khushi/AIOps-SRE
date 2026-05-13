"""Adapter: Datadog webhook event → canonical Alert dict.

Stub implementation — maps fields a typical Datadog monitor webhook sends.
Datadog delivers one alert per webhook event (unlike Alertmanager's batch
envelope), so this adapter takes the full webhook body.

Datadog tags are formatted ``"key:value"`` in a flat list; the ``service``
tag is conventional and we use it as the primary service signal.

Reference payload shape (excerpt)::

    {
      "id": "5234829...",
      "title": "[Triggered] High error rate on payment",
      "body": "Error rate 0.42 above threshold 0.20",
      "priority": "P1",
      "alert_type": "error",
      "date": 1715600000,
      "tags": ["service:payment", "env:prod", "team:payments"],
      "metric": "trace.http.request.errors",
      "value": 0.42
    }
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_PRIORITY_TO_SEVERITY = {"P1": "critical", "P2": "high", "P3": "warning", "P4": "info"}


def _parse_tags(tags: list[Any]) -> dict[str, str]:
    """Flatten Datadog ``"key:value"`` tags into a dict. Tags without a colon
    are kept as ``key=tag, value=""`` so they aren't silently dropped."""
    out: dict[str, str] = {}
    for t in tags or []:
        s = str(t)
        if ":" in s:
            k, _, v = s.partition(":")
            out[k] = v
        else:
            out[s] = ""
    return out


def to_canonical_alert(payload: dict[str, Any]) -> dict[str, Any]:
    tags = _parse_tags(payload.get("tags", []))
    service = tags.get("service") or tags.get("kube_service") or "unknown"
    metric = payload.get("metric") or payload.get("title") or "alert"
    value_raw = payload.get("value", 0)
    try:
        value = float(value_raw)
    except (TypeError, ValueError):
        value = 0.0
    epoch = payload.get("date")
    if isinstance(epoch, (int, float)):
        timestamp = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    else:
        timestamp = str(epoch) if epoch else datetime.now(UTC).isoformat()
    priority = payload.get("priority")
    severity_hint = _PRIORITY_TO_SEVERITY.get(priority) if isinstance(priority, str) else None
    return {
        "alert_id": f"DD-{payload.get('id', 'UNKNOWN')}",
        "service": service,
        "metric": metric,
        "value": value,
        "timestamp": timestamp,
        "source": "Datadog",
        "severity_hint": severity_hint,
        "labels": tags,
        "annotations": {
            "summary": str(payload.get("title", "")),
            "description": str(payload.get("body", "")),
        },
    }
