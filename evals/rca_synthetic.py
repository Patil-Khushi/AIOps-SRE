"""Simulated telemetry for offline RCA evaluation.

The problem this solves
----------------------
RCA can only reason from what it observes, so scoring it needs telemetry. A live
cluster gives real telemetry but is unavailable in CI, slow, and not reproducible —
two runs an hour apart see different numbers. Without an alternative, an offline
eval hands the agent an empty observation block, and every scenario correctly
returns "insufficient evidence". That measures the abstention path and nothing else.

So this module projects each truth file's **declared observable symptoms** into a
synthetic ``IncidentContext``: Prometheus-shaped rows under the exact PromQL keys
``evidence.required_promql_queries()`` asks for, Loki-shaped streams, and a firing
alert. ``agents/rca_agent/context_adapter.ContextBackend`` then reads it exactly as
it reads a real context build, so the whole evidence path — the reporting floors,
the NaN guard, ``render()``'s NONE lines, the key insertion order — runs unmodified.

What this is and is not
-----------------------
**Simulated, not production-validated.** Two limitations, stated here so no number
derived from this module is over-read:

1. **No distractor noise.** ``expected_signals`` is a curated list of the signals
   that discriminate. Real telemetry buries those in unrelated series, so this
   measures "can RCA reason from clean discriminating evidence", which is an upper
   bound on live performance rather than an estimate of it.
2. **Only what RCA queries can express.** RCA issues a fixed set of PromQL queries.
   A truth-file signal outside that set cannot be represented at all — and rather
   than dropping it silently, every such signal is recorded in
   :attr:`SyntheticEvidence.unrepresentable`. That list is a finding in its own
   right: it names the evidence the agent structurally cannot see, which is a real
   ceiling on its accuracy and not an artifact of the simulation.

Blindness
---------
Only observable fields are read: ``expected_alert_payload`` (the alert that fires)
and ``expected_signals`` (the symptoms). Never ``root_cause``, ``failure_key``,
``fault_category``, ``remediation`` or ``grading``. The signal *names* are metric
names the monitoring stack publishes; the ``expect`` prose is used only to derive a
numeric value and never reaches the agent as text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.rca_agent import evidence as _evidence
from aiops.context.models import SectionStatus
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    SecurityMetadata,
    SourceProvenance,
)

# PromQL keys, taken from the agent rather than retyped, so a new query added to
# ``evidence.py`` shows up here as an unfilled key instead of a silent mismatch.
_DEP_GAUGE_QUERIES = (
    "mysql_connection_status",
    "postgres_connection_status",
    "redis_connection_status",
)
_ORDERS_FAILED_QUERY = _evidence.ORDERS_FAILED_QUERY
_PAYMENT_FAILURES_QUERY = _evidence.PAYMENT_FAILURES_QUERY
_PAYMENT_TIMEOUT_QUERY = _evidence.PAYMENT_TIMEOUT_QUERY
_CPU_QUERY = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="ecommerce"}[2m]))'
_MEM_QUERY = (
    'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce"} / '
    '(container_spec_memory_limit_bytes{namespace="ecommerce"} > 0))'
)
_RESTARTS_QUERY = 'kube_pod_container_status_restarts_total{namespace="ecommerce"}'
_TERMINATED_QUERY = 'kube_pod_container_status_last_terminated_reason{namespace="ecommerce"} == 1'


def _latency_query(bucket: str) -> str:
    return f"histogram_quantile(0.95, sum by (le) (rate({bucket}[5m])))"


# Truth-file metric name -> the latency histogram bucket it is measured by.
_LATENCY_BUCKETS = {
    "order_latency_seconds": "order_latency_seconds_bucket",
    "login_latency_seconds": "login_latency_seconds_bucket",
    "payment_latency_seconds": "payment_latency_seconds_bucket",
}

_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
_REASON_RE = re.compile(r'reason\s*=\s*"([^"]+)"')


def _expected_number(expect: str, default: float) -> float:
    """Pull a numeric target out of a truth file's prose ``expect`` string.

    ``"== 0"`` -> 0.0, ``"> 90"`` -> 90.0, ``"p95 > 5s"`` -> 5.0. The strings are
    written for humans, so anything unparseable falls back to ``default`` rather
    than failing the scenario — a missing number costs one series its precision,
    not the evaluation.
    """
    match = _NUMBER_RE.search(expect or "")
    if not match:
        return default
    try:
        return float(match.group(1))
    except ValueError:
        return default


def _row(value: float, **labels: str) -> dict[str, Any]:
    """One Prometheus instant-vector row, in the shape ``evidence._q`` expects."""
    return {"metric": dict(labels), "value": [1_754_222_400, str(value)]}


@dataclass
class SyntheticEvidence:
    """Rows, plus an honest account of what could not be represented."""

    metrics_raw: dict[str, Any] = field(default_factory=dict)
    logs_raw: dict[str, Any] = field(default_factory=dict)
    unrepresentable: list[str] = field(default_factory=list)
    """Truth-file signals RCA issues no query for. A coverage finding, not a bug in
    this module — see the module docstring."""

    represented: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Fraction of this scenario's declared signals RCA can actually observe."""
        total = len(self.represented) + len(self.unrepresentable)
        return round(len(self.represented) / total, 4) if total else 1.0


def _pod_for(service: str) -> str:
    """A plausible pod name for a service, matching the cluster's naming.

    The pod-level rollup is what ``resource_saturation`` and ``pod_state`` key on,
    so the label has to look like a real pod or the agent's own format strings
    render something an operator would not recognise.
    """
    return f"{service}-7d4f8b6c9-x2n4p"


def build_synthetic_evidence(truth: dict[str, Any]) -> SyntheticEvidence:
    """Project one truth file's observable symptoms into query-keyed telemetry.

    Order of construction matters in one place: dependency gauges are seeded to
    ``REACHABLE`` for every store *first*, then overridden by whatever the scenario
    declares unreachable. That is what produces genuine discriminating negative
    evidence — "MySQL REACHABLE, PostgreSQL UNREACHABLE, Redis REACHABLE" tells the
    agent which datastore is at fault and rules out the other two, whereas emitting
    only the failing gauge would leave the other two ``CHECKED_ABSENT``-shaped and
    hand the agent a much easier, less realistic problem.
    """
    out = SyntheticEvidence()
    payload = truth.get("expected_alert_payload") or {}
    labels = payload.get("labels") or {}
    service = str(payload.get("service") or truth.get("service") or "unknown")
    signals = truth.get("expected_signals") or {}
    pod = _pod_for(service)

    # --- firing alert -----------------------------------------------------
    alertname = str(labels.get("alertname") or "")
    if alertname:
        out.metrics_raw[_evidence.ALERTS_QUERY_ID] = {
            "alerts": [
                {
                    "state": "firing",
                    "labels": {
                        "alertname": alertname,
                        "severity": str(labels.get("severity") or "warning"),
                        "service": service,
                        "namespace": str(labels.get("namespace") or "ecommerce"),
                    },
                }
            ]
        }
        out.represented.append(f"alert:{alertname}")

    # --- dependency gauges: healthy by default, then overridden ----------
    gauges: dict[str, float] = dict.fromkeys(_DEP_GAUGE_QUERIES, 1.0)

    # --- declared metric signals -----------------------------------------
    reasons: list[tuple[str, float]] = []
    payment_reasons: list[tuple[str, float]] = []
    for entry in signals.get("metrics") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("signal") or "").strip()
        expect = str(entry.get("expect") or "")
        if not name:
            continue

        base = name.split("{")[0]
        if base in gauges:
            gauges[base] = _expected_number(expect, 0.0)
            out.represented.append(name)
        elif base in ("orders_failed_total", "payment_failures_total"):
            match = _REASON_RE.search(name)
            bucket = reasons if base == "orders_failed_total" else payment_reasons
            bucket.append((match.group(1) if match else "unknown", _expected_number(expect, 0.25)))
            out.represented.append(name)
        elif base == "payment_timeout_total":
            out.metrics_raw[_PAYMENT_TIMEOUT_QUERY] = {
                "results": [_row(_expected_number(expect, 0.4))]
            }
            out.represented.append(name)
        elif base in _LATENCY_BUCKETS:
            out.metrics_raw[_latency_query(_LATENCY_BUCKETS[base])] = {
                "results": [_row(_expected_number(expect, 5.0))]
            }
            out.represented.append(name)
        else:
            # e.g. payment_failures_total{reason="gateway_timeout"},
            # orders_created_total — real series the agent issues no query for.
            out.unrepresentable.append(name)

    # --- the alerting metric itself --------------------------------------
    # The alert's own metric and value are the most machine-readable observable in
    # the file, and several scenarios declare their decisive signal only here.
    alert_metric = str(payload.get("metric") or "").strip()
    alert_value = payload.get("value")
    if alert_metric and isinstance(alert_value, (int, float)):
        value = float(alert_value)
        base = alert_metric.split("{")[0]
        if base in gauges:
            gauges[base] = value
            out.represented.append(alert_metric)
        elif base in _LATENCY_BUCKETS:
            out.metrics_raw.setdefault(
                _latency_query(_LATENCY_BUCKETS[base]), {"results": [_row(value)]}
            )
            out.represented.append(alert_metric)
        elif base == "payment_timeout_total":
            out.metrics_raw.setdefault(_PAYMENT_TIMEOUT_QUERY, {"results": [_row(value)]})
            out.represented.append(alert_metric)
        elif base == "container_cpu_usage_seconds_total":
            out.metrics_raw[_CPU_QUERY] = {"results": [_row(value, pod=pod)]}
            out.represented.append(alert_metric)
        elif base == "container_memory_working_set_bytes":
            out.metrics_raw[_MEM_QUERY] = {"results": [_row(value, pod=pod)]}
            out.represented.append(alert_metric)
        elif base == "orders_failed_total":
            # RCA does query this, by reason. Without this branch the alerting
            # metric was counted as a coverage gap even when the declared signals
            # had already supplied it — understating coverage and putting a series
            # the agent can actually see on the gap list.
            if not reasons:
                match = _REASON_RE.search(alert_metric)
                reasons.append((match.group(1) if match else "unknown", value))
            out.represented.append(alert_metric)
        elif base == "up":
            # `up == 0` is scrape failure. RCA has no `up` query; the observable
            # consequence it *can* see is the pod's restart/termination state,
            # which the container signals below supply.
            out.unrepresentable.append(alert_metric)
        else:
            out.unrepresentable.append(alert_metric)

    # --- container signals: restarts, terminations, saturation -----------
    restarts: int | None = None
    terminated_reason: str | None = None
    for entry in signals.get("container") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("signal") or entry.get("name") or "").strip()
        expect = str(entry.get("expect") or "")
        if name == "restart_count":
            restarts = int(_expected_number(expect, 5.0))
            out.represented.append(name)
        elif name == "restart_reason":
            terminated_reason = expect.strip() or "Error"
            out.represented.append(name)
        elif name == "state":
            # "never healthy" is the crashloop tell-tale; the observable form is a
            # non-zero restart count with no successful start.
            restarts = restarts if restarts is not None else 5
            terminated_reason = terminated_reason or "Error"
            out.represented.append(name)
        elif name == "cpu_percent":
            cores = _expected_number(expect, 90.0) / 100.0
            out.metrics_raw.setdefault(_CPU_QUERY, {"results": [_row(round(cores, 2), pod=pod)]})
            out.represented.append(name)
        elif name in ("mem_usage", "memory_percent"):
            out.metrics_raw.setdefault(_MEM_QUERY, {"results": [_row(0.97, pod=pod)]})
            out.represented.append(name)
        else:
            out.unrepresentable.append(name)

    if restarts is not None:
        out.metrics_raw[_RESTARTS_QUERY] = {"results": [_row(restarts, pod=pod)]}
    if terminated_reason is not None:
        out.metrics_raw[_TERMINATED_QUERY] = {
            "results": [_row(1, pod=pod, reason=terminated_reason)]
        }

    # --- assemble the gauge + reason queries -----------------------------
    for metric, value in gauges.items():
        out.metrics_raw[metric] = {"results": [_row(value)]}
    for query, bucket in (
        (_ORDERS_FAILED_QUERY, reasons),
        (_PAYMENT_FAILURES_QUERY, payment_reasons),
    ):
        if bucket:
            out.metrics_raw[query] = {
                "results": [_row(rate, reason=reason) for reason, rate in bucket]
            }

    # --- logs -------------------------------------------------------------
    lines: list[list[Any]] = []
    for entry in signals.get("logs") or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("contains") or "").strip()
        if not text:
            continue
        # Shaped like a real application log line so the agent's 180-char truncation
        # and its `level`/`"error" in text` filter behave as they do in production.
        lines.append(["1754222400000000000", f"{entry.get('level', 'ERROR')} {service}: {text}"])
        out.represented.append(f"log:{text[:40]}")
    if lines:
        out.logs_raw[_evidence.LOGS_QUERY_ID] = {
            "streams": [{"stream": {"level": "ERROR", "service": service}, "values": lines}]
        }

    if signals.get("trace"):
        # RCA issues no trace query today, so a declared trace symptom is a real
        # coverage gap rather than something this module chose not to model.
        out.unrepresentable.append("trace")

    return out


def _section(status: SectionStatus, provider: str, raw: dict[str, Any] | None) -> ContextSection:
    return ContextSection(
        status=status,
        provenance=SourceProvenance(provider=provider, status=status),
        raw=raw if status.usable else None,
    )


def _absent(note: str) -> ContextSection:
    """A section this simulation does not provide.

    ``NOT_REQUESTED`` rather than ``UNAVAILABLE``: nothing was attempted and nothing
    failed, and the two must stay distinguishable — a consumer that reads this
    context is entitled to know the difference between a source the eval declined to
    simulate and one that was asked and could not answer.
    """
    return ContextSection(
        status=SectionStatus.NOT_REQUESTED,
        provenance=SourceProvenance(
            provider="synthetic", status=SectionStatus.NOT_REQUESTED, coverage_note=note
        ),
    )


def build_synthetic_context(
    truth: dict[str, Any], *, window_minutes: int = 15
) -> tuple[IncidentContext, SyntheticEvidence]:
    """A full ``IncidentContext`` carrying this scenario's simulated telemetry.

    Returns the pack and the :class:`SyntheticEvidence` behind it, because the
    coverage account is part of the result: a scenario scored against evidence RCA
    could only half observe needs to be reported that way, not as a plain miss.

    Timestamps are fixed, not ``now()``-relative, so two runs over the same truth
    file produce byte-identical contexts — the same reproducibility discipline
    ``aiops/context/ranker.py`` keeps by taking ``now`` as a parameter.
    """
    synthetic = build_synthetic_evidence(truth)
    payload = truth.get("expected_alert_payload") or {}
    labels = payload.get("labels") or {}
    service = str(payload.get("service") or truth.get("service") or "unknown")

    window_end = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    window_start = window_end - timedelta(minutes=window_minutes)

    metrics = _section(SectionStatus.COLLECTED, "synthetic-prometheus", synthetic.metrics_raw)
    logs = (
        _section(SectionStatus.COLLECTED, "synthetic-loki", synthetic.logs_raw)
        if synthetic.logs_raw
        # EMPTY, not NOT_REQUESTED: Loki *was* consulted and this scenario genuinely
        # produces no error lines (several do not). That is evidence, and it is what
        # licenses the agent to reason from the absence.
        else _section(SectionStatus.EMPTY, "synthetic-loki", {})
    )

    context = IncidentContext(
        incident=IncidentIdentity(
            service=service,
            severity=str(payload.get("severity_hint") or labels.get("severity") or "unknown"),
            window_start=window_start,
            window_end=window_end,
            correlation_id=f"synthetic-{truth.get('id') or service}",
            alert_id=str(payload.get("alert_id") or ""),
            alert_name=str(labels.get("alertname") or ""),
        ),
        built_at=window_end,
        metrics=metrics,
        logs=logs,
        traces=_absent("traces not simulated"),
        k8s_events=_absent("k8s events not simulated"),
        topology=_absent("topology not simulated"),
        dependencies=_absent("dependencies not simulated"),
        deployments=_absent("deployments not simulated"),
        incident_history=_absent("history withheld: truth-file corpus must not reach RCA"),
        oncall=_absent("oncall not relevant to RCA scoring"),
        cmdb=_absent("cmdb not simulated"),
        runbooks=_absent("runbooks not simulated"),
        security=SecurityMetadata(redaction_applied=False),
    )
    return context, synthetic


__all__ = ["SyntheticEvidence", "build_synthetic_context", "build_synthetic_evidence"]
