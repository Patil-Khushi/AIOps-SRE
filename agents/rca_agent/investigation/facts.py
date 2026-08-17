"""Structured telemetry facts, extracted over the same seam ``evidence.py`` uses.

``evidence.gather`` returns *formatted prose* — ``"pod x: cpu=0.85 cores (limit 1)"``
— because its output goes into a prompt. Reasoning needs the number, not the sentence,
and regex-parsing those strings back into floats would make every format change a
silent reasoning bug.

So this module reads the same rows through the same ``evidence.Backend`` protocol and
produces typed facts. Nothing is re-queried when a Context Pack is supplied (the
context backend serves rows it already holds), and the PromQL constants are imported
from ``evidence`` rather than retyped, so the two cannot ask different questions.

Availability, and why it is inferred rather than assumed
--------------------------------------------------------
The hard part is not extraction, it is knowing whether an absent signal was *checked*.
``evidence._q`` returns ``[]`` both when Prometheus says "no samples" and when the call
failed — one value for two opposite facts. Treating the ambiguous case as
``CHECKED_ABSENT`` would let the agent rule out a cause it never observed, which is the
failure mode the whole negative-evidence design exists to prevent.

Two ways out, used in order:

1. **A Context Pack knows.** ``ContextSection.status`` distinguishes ``EMPTY``
   (queried, genuinely nothing) from ``UNAVAILABLE`` / ``FAILED``. When the caller has
   one, it passes the statuses in and this module trusts them.
2. **Otherwise, infer from siblings.** If *any* query against a source returned rows,
   that source is reachable, so an empty sibling is genuinely empty. If *none* did, the
   source is treated as unavailable. Conservative in the right direction: the only
   error it can make is declining to use real negative evidence, never inventing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from agents.rca_agent import evidence as _evidence


class Availability(StrEnum):
    """Whether a source answered, and so whether absence means anything."""

    CHECKED = "checked"
    """The source answered. An absent signal is genuinely absent — usable as evidence
    *against* a cause that would have produced it."""

    UNAVAILABLE = "unavailable"
    """Could not ask, or cannot tell. Absence carries no information."""

    @property
    def absence_is_evidence(self) -> bool:
        return self is Availability.CHECKED


@dataclass(frozen=True)
class DependencyGauge:
    """A service's own view of whether it can reach a backing store.

    The single most decisive signal available: it separates a datastore outage from
    every other failure mode without ambiguity. ``0`` is published by the application
    itself, so it means "I tried and could not connect", not "the scrape failed".
    """

    metric: str
    label: str
    value: float

    @property
    def reachable(self) -> bool:
        return self.value >= 1


@dataclass(frozen=True)
class DatastoreReadiness:
    """A backing datastore's OWN pod readiness, checked at the cluster level.

    Unlike :class:`DependencyGauge` (published by the *consuming* service),
    this is read directly off the datastore's StatefulSet via
    ``kube_statefulset_status_replicas_ready`` — so it still answers correctly
    when the consuming service's own pod is crash-looping and cannot report
    anything about itself at all.
    """

    label: str
    statefulset: str
    ready_replicas: float

    @property
    def ready(self) -> bool:
        return self.ready_replicas >= 1


@dataclass(frozen=True)
class ErrorRate:
    """One ``reason``-labelled error counter. ``reason`` names the mechanism."""

    metric: str
    reason: str
    rate: float


@dataclass(frozen=True)
class LatencyP95:
    hop: str
    seconds: float
    threshold: float | None = None

    @property
    def breaches_threshold(self) -> bool:
        return self.threshold is not None and self.seconds > self.threshold


@dataclass(frozen=True)
class PodResource:
    pod: str
    cpu_cores: float | None = None
    memory_ratio: float | None = None


@dataclass(frozen=True)
class PodLifecycle:
    pod: str
    restarts: int | None = None
    terminated_reason: str | None = None

    @property
    def oom_killed(self) -> bool:
        return (self.terminated_reason or "").strip().lower() == "oomkilled"

    @property
    def died_without_oom(self) -> bool:
        """Terminated for a non-OOM reason — the crash-loop shape.

        The distinction is load-bearing: ``OOMKilled`` and a plain ``Error`` share the
        same "service is down" alert, and only the termination reason separates a
        memory leak from a process that dies before it can bind its port.
        """
        reason = (self.terminated_reason or "").strip()
        return bool(reason) and not self.oom_killed


@dataclass(frozen=True)
class TraceSummary:
    """Recent distributed traces for the affected service — a third,
    independent evidence source alongside metrics and logs.

    Unlike :class:`DependencyGauge` or log lines, a trace is an actual
    end-to-end request record: ``errors == 0`` with ``total > 0`` is itself a
    real, checked observation (the traces were sampled and none carried an
    error status), not merely an absent signal.
    """

    total: int
    errors: int
    slow_duration_ms: float | None = None

    @property
    def has_errors(self) -> bool:
        return self.errors > 0


@dataclass(frozen=True)
class FiringAlert:
    name: str
    severity: str = "unknown"


@dataclass
class ObservedFacts:
    """Everything the investigation reasons from, with per-source availability."""

    gauges: list[DependencyGauge] = field(default_factory=list)
    datastore_readiness: list[DatastoreReadiness] = field(default_factory=list)
    trace_summary: TraceSummary | None = None
    error_rates: list[ErrorRate] = field(default_factory=list)
    latencies: list[LatencyP95] = field(default_factory=list)
    resources: list[PodResource] = field(default_factory=list)
    lifecycles: list[PodLifecycle] = field(default_factory=list)
    alerts: list[FiringAlert] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)

    metrics: Availability = Availability.UNAVAILABLE
    logs: Availability = Availability.UNAVAILABLE

    @property
    def any_observation(self) -> bool:
        return bool(
            self.gauges
            or self.datastore_readiness
            or self.error_rates
            or self.latencies
            or self.resources
            or self.lifecycles
            or self.alerts
            or self.log_lines
        )

    @property
    def unreachable_stores(self) -> list[DependencyGauge]:
        return [g for g in self.gauges if not g.reachable]

    @property
    def unready_datastores(self) -> list[DatastoreReadiness]:
        """Datastores confirmed down at the cluster level, independent of
        whether the consuming service's own gauge could be scraped at all."""
        return [d for d in self.datastore_readiness if not d.ready]

    @property
    def saturated_cpu(self) -> list[PodResource]:
        """Pods at or above :data:`CPU_SATURATION_CORES` of their 1-core limit."""
        return [r for r in self.resources if (r.cpu_cores or 0.0) >= CPU_SATURATION_CORES]

    @property
    def pressured_memory(self) -> list[PodResource]:
        return [r for r in self.resources if (r.memory_ratio or 0.0) >= MEMORY_PRESSURE_RATIO]

    def reason_rates(self, *needles: str) -> list[ErrorRate]:
        """Error counters whose ``reason`` contains any of ``needles``.

        Substring rather than equality because the vocabulary is open — a service may
        emit ``gateway_timeout`` or ``upstream_timeout`` for the same class of failure,
        and a rule that matched only one spelling would silently stop firing when a
        service was re-instrumented.
        """
        lowered = tuple(n.lower() for n in needles)
        return [e for e in self.error_rates if any(n in e.reason.lower() for n in lowered)]


# Thresholds for reading a raw number as a *condition*. Deliberately above
# ``evidence.py``'s REPORTING floors (0.2 cores / 80% memory): those decide what is
# worth printing, these decide what counts as saturated. Using the reporting floor to
# mean "saturated" would call an idle-but-visible pod a resource failure.
CPU_SATURATION_CORES = 0.8
"""limits.cpu is 1000m on every ecommerce service, so 1.0 == the limit and 0.8 is
80% of it — sustained CPU at that level is where throttling starts to show in
latency."""

MEMORY_PRESSURE_RATIO = 0.85
"""Fraction of the memory limit at which reclaim pressure is real. Below the OOM
threshold on purpose: the point is to catch pressure *before* the kernel kills, since
one of the two memory failure modes never OOMs at all."""


def _rows_value(rows: list[dict], index: int = 0) -> float | None:
    """The scalar from one Prometheus row, with the NaN guard ``evidence`` uses.

    ``histogram_quantile`` over an idle service returns the literal string ``"NaN"``,
    which ``float()`` accepts and which formats as ``"nans"`` — nonsense a consumer
    then has to explain. Treated as no-data, exactly as ``evidence._scalar`` does.
    """
    try:
        value = float(rows[index]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None if value != value else value


def _label(row: dict, key: str, default: str = "?") -> str:
    return str((row.get("metric") or {}).get(key, default))


def collect_facts(
    service: str,
    backend: _evidence.Backend | None = None,
    *,
    metrics_available: bool | None = None,
    logs_available: bool | None = None,
) -> ObservedFacts:
    """Extract typed facts for ``service``. Never raises.

    ``metrics_available`` / ``logs_available`` come from a Context Pack's section
    statuses when the caller has one. ``None`` means "infer from whether anything came
    back" — see the module docstring for why that inference is safe in only one
    direction.
    """
    api = backend or _evidence.LiveBackend()
    facts = ObservedFacts()
    metric_rows_seen = False

    def query(promql: str) -> list[dict]:
        nonlocal metric_rows_seen
        try:
            rows = api.query(promql)
        except Exception:
            return []
        if rows:
            metric_rows_seen = True
        return rows

    # Dependency gauges.
    for metric, label in _evidence.DEP_GAUGES.items():
        value = _rows_value(query(metric))
        if value is not None:
            facts.gauges.append(DependencyGauge(metric=metric, label=label, value=value))

    # Backing datastore readiness, checked at the cluster level (see
    # DatastoreReadiness's docstring for why this is a separate source from
    # the gauges above rather than folded into them).
    entry = _evidence.DATASTORE_STATEFULSETS.get(_evidence.service_pod_prefix(service))
    if entry:
        statefulset, label = entry
        ready = _rows_value(query(_evidence.datastore_ready_query(statefulset)))
        if ready is not None:
            facts.datastore_readiness.append(
                DatastoreReadiness(label=label, statefulset=statefulset, ready_replicas=ready)
            )

    # Recent traces — a third, independent evidence source. Deliberately calls
    # the live registry directly rather than going through the injected
    # ``backend``/``query`` seam, same precedent as ``recent_changes`` in
    # ``evidence.py`` (see that module's docstring): this is a live-only
    # category, not part of the Context Engineering Layer's byte-identity
    # parity contract.
    try:
        trace_res = _evidence._live_traces(service)
        if getattr(trace_res, "ok", False):
            traces = (trace_res.data or {}).get("traces") or []
            if traces:
                durations = [
                    t["duration_us"]
                    for t in traces
                    if isinstance(t.get("duration_us"), (int, float))
                ]
                facts.trace_summary = TraceSummary(
                    total=len(traces),
                    errors=sum(1 for t in traces if t.get("has_error")),
                    slow_duration_ms=(max(durations) / 1000) if durations else None,
                )
    except Exception:
        pass

    # Error counters, by reason.
    for metric, promql in (
        ("orders_failed_total", _evidence.ORDERS_FAILED_QUERY),
        ("payment_failures_total", _evidence.PAYMENT_FAILURES_QUERY),
    ):
        for row in query(promql):
            rate = _rows_value([row])
            if rate is not None and rate > 0:
                facts.error_rates.append(
                    ErrorRate(metric=metric, reason=_label(row, "reason", "unknown"), rate=rate)
                )
    timeouts = _rows_value(query(_evidence.PAYMENT_TIMEOUT_QUERY))
    if timeouts is not None and timeouts > 0:
        facts.error_rates.append(
            ErrorRate(metric="payment_timeout_total", reason="timeout", rate=timeouts)
        )

    # Latency.
    for bucket, (hop, threshold) in _evidence.LATENCY_HISTOGRAMS.items():
        p95 = _rows_value(query(_evidence.latency_query(bucket)))
        if p95 is not None:
            facts.latencies.append(LatencyP95(hop=hop, seconds=p95, threshold=threshold))

    # Resource saturation. CPU and memory arrive from two queries and are merged per
    # pod, so a rule can ask "is this pod CPU-bound *and* memory-clean?" without
    # correlating two lists itself.
    # Both queries are namespace-wide (every pod in `ecommerce`), so rows are
    # filtered to this incident's own service here — otherwise an unrelated
    # pod's stale restart/resource reading gets scored as if it were the
    # affected service's own evidence.
    by_pod: dict[str, dict[str, float]] = {}
    for promql, key in ((_evidence.CPU_QUERY, "cpu"), (_evidence.MEM_QUERY, "mem")):
        for row in query(promql):
            pod = _label(row, "pod")
            if not _evidence.pod_belongs_to_service(pod, service):
                continue
            value = _rows_value([row])
            if value is not None:
                by_pod.setdefault(pod, {})[key] = value
    facts.resources = [
        PodResource(pod=pod, cpu_cores=v.get("cpu"), memory_ratio=v.get("mem"))
        for pod, v in sorted(by_pod.items())
    ]

    # Pod lifecycle, likewise merged per pod and filtered to this service.
    life: dict[str, dict[str, object]] = {}
    for row in query(_evidence.RESTARTS_QUERY):
        pod = _label(row, "pod")
        if not _evidence.pod_belongs_to_service(pod, service):
            continue
        count = _rows_value([row])
        if count is not None:
            life.setdefault(pod, {})["restarts"] = int(count)
    for row in query(_evidence.TERMINATED_QUERY):
        pod = _label(row, "pod")
        if not _evidence.pod_belongs_to_service(pod, service):
            continue
        reason = _label(row, "reason", "")
        if reason:
            life.setdefault(pod, {})["reason"] = reason
    facts.lifecycles = [
        PodLifecycle(
            pod=pod,
            restarts=int(v["restarts"]) if isinstance(v.get("restarts"), int) else None,
            terminated_reason=str(v["reason"]) if v.get("reason") else None,
        )
        for pod, v in sorted(life.items())
    ]

    # Firing alerts. Reuses ``evidence.firing_alerts``' parsing and then splits the
    # formatted line back apart, rather than duplicating the payload walk — one place
    # decides what "firing" means.
    alerts_ok = False
    try:
        for line in _evidence.firing_alerts(api.alerts):
            alerts_ok = True
            name, _, tail = line.partition(" (severity=")
            facts.alerts.append(
                FiringAlert(name=name.strip(), severity=tail.rstrip(")") or "unknown")
            )
    except Exception:
        pass

    logs_ok = False
    try:
        facts.log_lines = _evidence.recent_logs(service, fetch=api.logs)
        logs_ok = bool(facts.log_lines)
    except Exception:
        facts.log_lines = []

    facts.metrics = (
        Availability.CHECKED
        if (metrics_available if metrics_available is not None else (metric_rows_seen or alerts_ok))
        else Availability.UNAVAILABLE
    )
    facts.logs = (
        Availability.CHECKED
        if (logs_available if logs_available is not None else logs_ok)
        else Availability.UNAVAILABLE
    )
    return facts


__all__ = [
    "CPU_SATURATION_CORES",
    "MEMORY_PRESSURE_RATIO",
    "Availability",
    "DependencyGauge",
    "ErrorRate",
    "FiringAlert",
    "LatencyP95",
    "ObservedFacts",
    "PodLifecycle",
    "PodResource",
    "collect_facts",
]
