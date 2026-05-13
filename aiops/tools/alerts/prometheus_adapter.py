"""Adapter: Prometheus ``/api/v1/alerts`` entry → canonical Alert dict.

Pure function — no httpx, no registry hooks — so unit tests run without a
cluster. The fetching half lives in ``aiops/tools/observability/prometheus.py``
under capability ``observability.metrics.alerts``.

Kept dict-in / dict-out (rather than typed against
``agents.alert_triage.models.Alert``) so this adapter has no back-reference
from ``aiops/tools/`` into ``agents/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def to_canonical_alert(prom_alert: dict[str, Any]) -> dict[str, Any]:
    """Translate one Prometheus alert entry into the canonical Alert shape.

    Resolves ``service`` and ``metric`` via documented Prometheus label
    conventions, with ordered fallbacks so any well-formed alert (incl.
    those without an explicit ``service`` label) produces a usable Alert.
    """
    labels = prom_alert.get("labels", {}) or {}
    annotations = prom_alert.get("annotations", {}) or {}
    service = (
        labels.get("service")
        or labels.get("service_name")
        or labels.get("job")
        or annotations.get("service")
        or "unknown"
    )
    metric = (
        labels.get("alertname")
        or labels.get("__name__")
        or annotations.get("summary")
        or "alert"
    )
    value_str = prom_alert.get("value") or labels.get("value") or "0"
    try:
        value = float(value_str)
    except (TypeError, ValueError):
        value = 0.0
    return {
        "alert_id": f"PROM-{labels.get('alertname','UNKNOWN')}-{labels.get('instance', 'na')}",
        "service": service,
        "metric": metric,
        "value": value,
        "timestamp": prom_alert.get("activeAt") or datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "severity_hint": labels.get("severity"),
        "labels": {k: str(v) for k, v in labels.items()},
        "annotations": {k: str(v) for k, v in annotations.items()},
    }
