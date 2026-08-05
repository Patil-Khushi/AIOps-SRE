"""One-shot enrichment: add eval blocks to the ecommerce truth files.

Adds ``expected_alert_payload`` + ``exercises`` to every truth file that lacks
them, so ``evals/harness.py`` can actually score them. Without both blocks the
harness returns an empty run — it does NOT error — so the suite silently
reports success while measuring nothing.

Idempotent: output is a pure function of the input, so re-running is a no-op
unless the mapping below changed. Kept in the
repo (rather than deleted after use) so the mapping between a Prometheus alert
rule and its truth file is reviewable in one place, and so adding a 13th
scenario is a one-line change plus a re-run.

    uv run python demo/ecommerce/truth_files/_add_eval_blocks.py

Key detail — `severity_hint` must be TOP-LEVEL, not just inside `labels`.
agents/alert_triage/agent.py reads ``alert.severity_hint``; the Alertmanager /
Prometheus adapters populate it from ``labels.severity``, but the harness feeds
the payload straight to ``run()`` with no adapter in the path. Put it only in
`labels` and the rule-based classifier falls through to the value/threshold
ratio — which scores a MySQL outage (value=0, threshold=1) as Sev-4, because
that heuristic assumes higher-is-worse.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# scenario id -> (alertname, metric, value, threshold)
# Alert names must match the `ecommerce` rule group in
# infra/observability/prometheus-values.yaml.
# value/threshold mirror what the firing rule would actually report.
ALERTS: dict[str, tuple[str, str, float, float]] = {
    "user_service_mysql_down": ("EcommerceMySQLDown", "mysql_connection_status", 0.0, 1.0),
    "order_service_postgres_down": (
        "EcommercePostgresDown",
        "postgres_connection_status",
        0.0,
        1.0,
    ),
    "payment_service_redis_down": ("EcommerceRedisDown", "redis_connection_status", 0.0, 1.0),
    "user_service_crashloop": ("EcommerceServiceDown", "up", 0.0, 1.0),
    "order_service_memory_leak": ("EcommerceServiceDown", "up", 0.0, 1.0),
    "order_service_payment_timeout": (
        "EcommercePaymentTimeouts",
        "payment_timeout_total",
        1.8,
        0.0,
    ),
    "payment_service_gateway_timeout": (
        "EcommercePaymentTimeouts",
        "payment_timeout_total",
        1.5,
        0.0,
    ),
    "order_service_http_500": ("EcommerceOrderErrorRateHigh", "orders_failed_total", 1.84, 0.0),
    "payment_service_http_500": ("EcommerceOrderErrorRateHigh", "orders_failed_total", 1.6, 0.0),
    "user_service_high_latency": ("EcommerceOrderLatencyHigh", "order_latency_seconds", 10.4, 2.0),
    "user_service_high_cpu": ("EcommerceOrderLatencyHigh", "order_latency_seconds", 4.2, 2.0),
    "payment_service_high_cpu": ("EcommerceOrderLatencyHigh", "order_latency_seconds", 3.8, 2.0),
}

# The truth files' own `severity` field drives the expected verdict. This is the
# rule-based path in _classify_severity_rule_based: a severity_hint containing
# "critical" returns Sev-1 at 0.95; "high" returns Sev-2 at 0.90.
SEVERITY_TO_VERDICT = {"critical": "Sev-1", "high": "Sev-2"}

# Expected on-call team per service.
#
# alert_triage resolves ownership from the CMDB, falling back to
# "Platform On-Call". These values must match the ecommerce rows in
# aiops/tools/itsm/_demo_cmdb.py — added in migration Phase 5, which is why
# user-service and order-service moved off the Platform On-Call default.
TEAM_BY_SERVICE = {
    "user-service": "Identity Team",
    "order-service": "Order Experience",
    "payment-service": "Payments Team",
}
DEFAULT_TEAM = "Platform On-Call"

# Fixed timestamp: the payload is a fixture, and a moving clock would make eval
# runs non-reproducible.
TIMESTAMP = "2026-08-03T10:00:00Z"


def build(data: dict) -> tuple[dict, dict]:
    scenario_id = data["id"]
    service = data["service"]
    severity = data["severity"]
    alertname, metric, value, threshold = ALERTS[scenario_id]

    payload = {
        "alert_id": f"ALT-{scenario_id.replace('_', '-')}",
        "service": service,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        # Top-level — see the module docstring.
        "severity_hint": severity,
        "timestamp": TIMESTAMP,
        "source": "Prometheus",
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "service": service,
            "namespace": "ecommerce",
        },
    }

    exercises = {
        "alert_triage": {
            "affected_service": service,
            "severity_in": [SEVERITY_TO_VERDICT[severity]],
            # status_in, not status: alert_triage dedups against prior verdicts
            # for the same (service, alertname), so a second harness run
            # legitimately yields "Suppressed" instead of "Active".
            "status_in": ["Active", "Suppressed"],
            "min_confidence_score": 0.6,
            "assigned_team": TEAM_BY_SERVICE.get(service, DEFAULT_TEAM),
        }
    }
    return payload, exercises


def main() -> int:
    changed = 0
    for path in sorted(HERE.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") not in ALERTS:
            print(f"  SKIP (no alert mapping): {path.name}")
            continue
        payload, exercises = build(data)
        data["expected_alert_payload"] = payload
        data["exercises"] = exercises
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  written: {path.name}")
        changed += 1
    print(f"\n{changed} file(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
