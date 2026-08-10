"""Gather real telemetry so the RCA Agent reasons from observation, not guesswork.

Before this existed the agent saw only a triage verdict — a service name, a
severity and a one-line summary — and had to *infer* a mechanism from the
service name alone. With a prompt that described the OTel Demo's flagd flags it
duly produced answers like "flagd feature flag `orderServiceFailure` is on" for
a system that has no feature flags at all. Confident, plausible, and wrong.

This module answers the questions an SRE would actually ask, against the live
stack, and hands the results to the model as evidence:

    Is a dependency gauge down?      -> mysql/postgres/redis_connection_status
    Are errors counting, and why?    -> orders_failed_total by reason
    Are calls timing out?            -> payment_timeout_total
    Which hop is slow?               -> order/login/payment p95
    Is the container starved?        -> container CPU cores, memory vs limit
    Did the pod die, and how?        -> OOMKilled vs Error vs CrashLoopBackOff
    What is actually alerting?       -> Prometheus /alerts
    What changed just before?        -> scm.commit.history

Every query goes through the tool registry, never a direct HTTP call or a
kubectl shell-out, so an unreachable backend degrades one line of evidence
instead of failing the RCA. Each lookup is individually guarded: a stack with
no Loki still produces metric evidence.

The evidence is deliberately NOT the answer. Nothing here says "fault X is
injected" — that would hand the model the conclusion and make the RCA a
lookup rather than a diagnosis. It reports symptoms; the model infers cause.
"""

from __future__ import annotations

import logging
from typing import Any

from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# Dependency-health gauges the services publish. 0 means "cannot reach it",
# which is the single most decisive signal available — it distinguishes a
# datastore outage from every other failure mode without ambiguity.
_DEP_GAUGES = {
    "mysql_connection_status": "MySQL (user-service)",
    "postgres_connection_status": "PostgreSQL (order-service)",
    "redis_connection_status": "Redis (payment-service)",
}


def _q(promql: str) -> list[dict[str, Any]]:
    """Run a PromQL instant query. Returns [] on any failure."""
    try:
        res = get_registry().call("observability.metrics.query", promql=promql)
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    if not isinstance(data, dict):
        return []
    # The seam normalises Prometheus's `data.result` to `results` (plural).
    # Reading `result` here silently returned [] for every query, so the agent
    # got zero evidence and fell back to reasoning from the prompt alone — with
    # no error anywhere to indicate it. `result` is kept as a fallback in case
    # a different provider serves this capability.
    rows = data.get("results")
    if rows is None:
        rows = data.get("result")
    return rows if isinstance(rows, list) else []


def _scalar(promql: str) -> float | None:
    rows = _q(promql)
    if not rows:
        return None
    try:
        v = float(rows[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    # Prometheus returns the literal string "NaN" for an aggregation over no
    # samples — histogram_quantile on an idle service is the common case.
    # float("NaN") is truthy and formats as "nans", which reached the model as
    # a nonsense observation it then had to explain. Treat it as "no data".
    if v != v:  # NaN
        return None
    return v


def dependency_health() -> list[str]:
    """Which backing stores are unreachable, per the services' own gauges."""
    out: list[str] = []
    for metric, label in _DEP_GAUGES.items():
        v = _scalar(metric)
        if v is None:
            continue
        out.append(f"{label}: {'REACHABLE' if v >= 1 else 'UNREACHABLE (gauge=0)'}")
    return out


def error_breakdown() -> list[str]:
    """Order failures by reason — the label that names the mechanism.

    `reason` is the highest-value single field in the whole system:
    injected_500 / db_error / payment_failed / payment_timeout / user_invalid
    each point at a different root cause.
    """
    out: list[str] = []
    for row in _q("sum by (reason) (rate(orders_failed_total[5m]))"):
        reason = (row.get("metric") or {}).get("reason", "?")
        try:
            rate = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if rate > 0:
            out.append(f"orders_failed_total reason={reason}: {rate:.3f}/s")
    timeouts = _scalar("rate(payment_timeout_total[5m])")
    if timeouts is not None and timeouts > 0:
        out.append(f"payment_timeout_total: {timeouts:.3f}/s")
    return out


# p95 histograms each service publishes. order_latency_seconds is the one the
# alert rule keys on; the other two are what localise a latency symptom to a
# service. Without login/payment p95 the model can see "something is slow" but
# not "the slow hop is /login", which is the difference between diagnosing
# user_service.high_latency and guessing.
_LATENCY_HISTOGRAMS = {
    "order_latency_seconds_bucket": ("order", 2.0),
    "login_latency_seconds_bucket": ("login (user-service)", None),
    "payment_latency_seconds_bucket": ("payment (payment-service)", None),
}

# limits.cpu is 1000m on all three services, so CPU is reported in cores and
# 1.0 == the limit. These are REPORTING floors, not alert thresholds — anything
# quieter is idle noise that would bury the signal in a wall of near-zero rows.
_CPU_REPORT_FLOOR_CORES = 0.2
_MEM_REPORT_FLOOR_RATIO = 0.8


def latency() -> list[str]:
    out: list[str] = []
    for bucket, (label, threshold) in _LATENCY_HISTOGRAMS.items():
        p95 = _scalar(f"histogram_quantile(0.95, sum by (le) (rate({bucket}[5m])))")
        if p95 is None:
            continue
        note = "  (ABOVE the 2s threshold)" if threshold and p95 > threshold else ""
        out.append(f"{label} p95 latency: {p95:.2f}s{note}")
    return out


def resource_saturation() -> list[str]:
    """Container CPU and memory against their limits.

    The alert rules for CPU and memory fire on these series, and the prompt
    forbids citing a metric absent from the observation block — so without this
    the agent could see a "…CPUHigh" alert firing and have nothing to justify a
    root cause with. It would either report low confidence or invent a number.

    Keyed on `pod`, NOT `container`. This cluster's cAdvisor publishes only
    pod-level rollups — the series carry `id`, `job`, `namespace` and `pod` and
    no `container` label at all — so a `container="user-service"` selector
    silently matches nothing. Each app pod runs a single container, so the
    rollup is the container figure.

    The `> 0` on the limit denominator is not decoration: an unlimited container
    reports a limit of 0, and `x / 0` is `+Inf`, which clears any threshold.
    """
    out: list[str] = []
    for row in _q(
        'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="ecommerce"}[2m]))'
    ):
        pod = (row.get("metric") or {}).get("pod", "?")
        try:
            cores = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if cores >= _CPU_REPORT_FLOOR_CORES:
            out.append(f"pod {pod}: cpu={cores:.2f} cores (limit 1)")

    for row in _q(
        'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce"} / '
        '(container_spec_memory_limit_bytes{namespace="ecommerce"} > 0))'
    ):
        pod = (row.get("metric") or {}).get("pod", "?")
        try:
            ratio = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if ratio >= _MEM_REPORT_FLOOR_RATIO:
            out.append(f"pod {pod}: memory={ratio:.0%} of its limit")
    return out


def pod_state() -> list[str]:
    """Restart counts and termination reasons.

    This is what separates the two failures that share the EcommerceServiceDown
    alert: `OOMKilled` means the memory leak, plain `Error` before the port
    binds means the crashloop. Without it the model cannot tell them apart.
    """
    out: list[str] = []
    for row in _q('kube_pod_container_status_restarts_total{namespace="ecommerce"}'):
        pod = (row.get("metric") or {}).get("pod", "?")
        try:
            n = int(float(row["value"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if n > 0:
            out.append(f"pod {pod}: restartCount={n}")
    for row in _q('kube_pod_container_status_last_terminated_reason{namespace="ecommerce"} == 1'):
        m = row.get("metric") or {}
        if reason := m.get("reason"):
            out.append(f"pod {m.get('pod', '?')}: last terminated reason={reason}")
    return out


def firing_alerts() -> list[str]:
    try:
        res = get_registry().call("observability.metrics.alerts")
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    alerts = data.get("alerts") if isinstance(data, dict) else None
    out: list[str] = []
    for a in alerts or []:
        labels = a.get("labels") or {}
        if a.get("state") == "firing":
            out.append(f"{labels.get('alertname', '?')} (severity={labels.get('severity', '?')})")
    return out


def recent_logs(service: str, limit: int = 12) -> list[str]:
    """Recent ERROR-level lines for the service."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    try:
        res = get_registry().call(
            "observability.logs.query",
            service=service,
            start=now - timedelta(minutes=15),
            end=now,
            limit=200,
        )
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    out: list[str] = []
    for stream in (data.get("streams") or [])[:5]:
        level = (stream.get("stream") or {}).get("level", "")
        for _ts, line in (stream.get("values") or [])[:limit]:
            text = str(line)
            if level.upper() in ("ERROR", "CRITICAL") or "error" in text.lower():
                out.append(text[:180])
            if len(out) >= limit:
                return out
    return out


def recent_changes(path: str | None = None, limit: int = 5) -> list[str]:
    """Commits touching the service — change correlation.

    A deploy minutes before onset is the most common real-world root cause and
    the one signal metrics/logs/traces structurally cannot provide.
    """
    try:
        res = get_registry().call("scm.commit.history", path=path, limit=limit)
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    return [
        f"{c.get('sha', '')} {c.get('date', '')} {c.get('message', '')}"
        for c in (data.get("commits") or [])
    ]


def gather(service: str) -> dict[str, list[str]]:
    """Collect every evidence category. Never raises."""
    ev: dict[str, list[str]] = {}
    for name, fn in (
        ("firing_alerts", firing_alerts),
        ("dependency_health", dependency_health),
        ("error_breakdown", error_breakdown),
        ("latency", latency),
        ("pod_state", pod_state),
        ("resource_saturation", resource_saturation),
    ):
        try:
            if rows := fn():
                ev[name] = rows
        except Exception:
            logger.debug("evidence %s failed", name, exc_info=True)
    for name, fn2, arg in (
        ("recent_logs", recent_logs, service),
        ("recent_changes", recent_changes, f"demo/ecommerce/{service}"),
    ):
        try:
            if rows := fn2(arg):
                ev[name] = rows
        except Exception:
            logger.debug("evidence %s failed", name, exc_info=True)
    return ev


def render(ev: dict[str, list[str]]) -> str:
    """Format evidence for the prompt. Empty string when nothing was collected."""
    if not ev:
        return ""
    titles = {
        "firing_alerts": "Currently firing alerts",
        "dependency_health": "Dependency health (from the services' own gauges)",
        "error_breakdown": "Error counters by reason",
        "latency": "Latency",
        "resource_saturation": "Container CPU and memory against limits",
        "pod_state": "Pod restarts and termination reasons",
        "recent_logs": "Recent error log lines",
        "recent_changes": "Recent commits touching this service",
    }
    # Categories that must ALWAYS appear, stating "NONE" when empty.
    #
    # Omitting an empty category communicates absence only by the lack of a
    # heading, which the model does not notice — it kept asserting
    # "orders_failed_total reason=injected_500" on a system where no error
    # counter was moving at all, because nothing in the block contradicted the
    # alert summary. An explicit "NONE" is a fact it can reason against.
    always = {
        "firing_alerts": "no alerts are currently firing",
        "error_breakdown": "no error counter is incrementing; no order failures recorded",
        "pod_state": "no pod restarts and no abnormal terminations",
        # Same reasoning as the others, and load-bearing for the CPU and memory
        # alerts: "no container is saturated" is what rules OUT a resource cause
        # when a shared alert could mean either that or a slow code path.
        "resource_saturation": ("no container is above 20% CPU or 80% of its memory limit"),
    }

    parts = ["", "Live observation from the running system:"]
    for key in list(titles):
        rows = ev.get(key)
        if rows:
            parts.append(f"\n{titles[key]}:")
            parts += [f"- {r}" for r in rows]
        elif key in always:
            parts.append(f"\n{titles[key]}:")
            parts.append(f"- NONE — {always[key]}")
    parts.append(
        "\nThese are OBSERVATIONS, not conclusions, and they are the complete set "
        "collected. A category marked NONE means that signal was checked and was "
        "absent — treat it as positive evidence AGAINST any cause that would "
        "produce it. Do not cite a metric that does not appear above."
    )
    return "\n".join(parts)
