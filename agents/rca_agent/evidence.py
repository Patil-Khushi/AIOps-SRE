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
from collections.abc import Callable
from typing import Any, Protocol

from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# --- the retrieval seam -------------------------------------------------
#
# Every function below takes its row source as a parameter that defaults to the
# live, registry-backed one. Nothing about the evidence itself changed: the queries,
# the reporting floors, the NaN guard and every format string are exactly as they
# were, and with the defaults in place this module behaves identically.
#
# The seam exists so the Context Engineering Layer can supply rows it already
# collected instead of this module re-querying Prometheus. The alternative was for
# ``agents/rca_agent/context_adapter.py`` to reimplement all eight categories against
# context payloads, which would mean two copies of ``f"pod {pod}: cpu={cores:.2f}
# cores (limit 1)"`` and of the 20%/80% floors — and the copies would drift, silently
# changing the RCA prompt the first time someone edited one of them.
#
# Injecting instead makes byte-identity *structural*: the context path runs this exact
# code. ``tests/test_rca_context_adapter.py`` drives both paths from one fixture and
# asserts the outputs are equal, so the claim is enforced rather than asserted.
#
# Same dependency-injection shape ``agents/resolution_verifier/verifier.py`` already
# uses for its ITSM and metrics calls (``_default_itsm_call`` / ``_default_metrics_call``).

QueryFn = Callable[[str], list[dict[str, Any]]]
"""PromQL string -> Prometheus-shaped rows. Returns ``[]`` rather than raising."""

# Dependency-health gauges the services publish. 0 means "cannot reach it",
# which is the single most decisive signal available — it distinguishes a
# datastore outage from every other failure mode without ambiguity.
DEP_GAUGES = {
    "mysql_connection_status": "MySQL (user-service)",
    "postgres_connection_status": "PostgreSQL (order-service)",
    "redis_connection_status": "Redis (payment-service)",
}


# Which datastore StatefulSet backs each service, for a check that does NOT
# depend on the service's own exporter being alive. DEP_GAUGES above answers
# "does the service itself say it can reach its store" — but that gauge is
# published BY the service, so when the service's own pod is crash-looping,
# Prometheus has nothing to scrape and the gauge is simply absent, not "0".
# A real dependency outage then looks identical to "no data" using the gauge
# alone. This answers the same question a different way: is the datastore's
# own pod actually up, checked directly, independent of whether the service
# that depends on it can currently report anything about itself.
DATASTORE_STATEFULSETS = {
    "user-service": ("mysql", "MySQL"),
    "order-service": ("postgres", "PostgreSQL"),
    "payment-service": ("redis", "Redis"),
}


def datastore_ready_query(statefulset: str) -> str:
    return f'kube_statefulset_status_replicas_ready{{namespace="ecommerce", statefulset="{statefulset}"}}'


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


def _scalar(promql: str, q: QueryFn = _q) -> float | None:
    rows = q(promql)
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


def dependency_health(q: QueryFn = _q) -> list[str]:
    """Which backing stores are unreachable, per the services' own gauges."""
    out: list[str] = []
    for metric, label in DEP_GAUGES.items():
        v = _scalar(metric, q)
        if v is None:
            continue
        out.append(f"{label}: {'REACHABLE' if v >= 1 else 'UNREACHABLE (gauge=0)'}")
    return out


# The two by-reason error counters, as named constants so ``error_breakdown`` and
# ``required_promql_queries`` cannot disagree about them. They used to be inline
# literals duplicated between the two, which is the drift this file's own docstring
# warns about for every other query.
ORDERS_FAILED_QUERY = "sum by (reason) (rate(orders_failed_total[5m]))"
PAYMENT_FAILURES_QUERY = "sum by (reason) (rate(payment_failures_total[5m]))"
PAYMENT_TIMEOUT_QUERY = "rate(payment_timeout_total[5m])"


def _by_reason(metric: str, promql: str, q: QueryFn) -> list[str]:
    """Rows of a ``sum by (reason)`` counter, formatted, non-zero only."""
    out: list[str] = []
    for row in q(promql):
        reason = (row.get("metric") or {}).get("reason", "?")
        try:
            rate = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if rate > 0:
            out.append(f"{metric} reason={reason}: {rate:.3f}/s")
    return out


def error_breakdown(q: QueryFn = _q) -> list[str]:
    """Failures by reason — the label that names the mechanism.

    `reason` is the highest-value single field in the whole system:
    injected_500 / db_error / payment_failed / payment_timeout / user_invalid
    each point at a different root cause.

    ``payment_failures_total`` was missing here, and its absence was measurable. The
    RCA evaluation put ``payment_service.redis_down`` down to a DNS fault, and the
    reasoning was *correct given the evidence*: the system prompt says a genuine Redis
    outage shows ``payment_failures_total reason=redis_error``, this module never
    queried that counter, so the model found no ``redis_error`` and concluded DNS.
    ``reason`` also discriminates ``gateway_timeout`` from ``injected_500`` on the
    payment path, which no other series does — so the gap cost the agent the single
    most diagnostic label available for three of the twelve scenarios.
    """
    return [
        *_by_reason("orders_failed_total", ORDERS_FAILED_QUERY, q),
        *_by_reason("payment_failures_total", PAYMENT_FAILURES_QUERY, q),
        *(
            [f"payment_timeout_total: {timeouts:.3f}/s"]
            if (timeouts := _scalar(PAYMENT_TIMEOUT_QUERY, q)) is not None and timeouts > 0
            else []
        ),
    ]


# p95 histograms each service publishes. order_latency_seconds is the one the
# alert rule keys on; the other two are what localise a latency symptom to a
# service. Without login/payment p95 the model can see "something is slow" but
# not "the slow hop is /login", which is the difference between diagnosing
# user_service.high_latency and guessing.
LATENCY_HISTOGRAMS = {
    "order_latency_seconds_bucket": ("order", 2.0),
    "login_latency_seconds_bucket": ("login (user-service)", None),
    "payment_latency_seconds_bucket": ("payment (payment-service)", None),
}

# limits.cpu is 1000m on all three services, so CPU is reported in cores and
# 1.0 == the limit. These are REPORTING floors, not alert thresholds — anything
# quieter is idle noise that would bury the signal in a wall of near-zero rows.
_CPU_REPORT_FLOOR_CORES = 0.2
_MEM_REPORT_FLOOR_RATIO = 0.8

# Query text shared with ``required_promql_queries`` and with
# ``investigation/facts.py``. Named constants rather than inline literals repeated in
# each place, which is the drift this module's own docstring warns about: a query
# edited in one copy and not the other silently means the context path requests a
# series the gathering path never reads.
CPU_QUERY = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="ecommerce"}[2m]))'
MEM_QUERY = (
    'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce"} / '
    '(container_spec_memory_limit_bytes{namespace="ecommerce"} > 0))'
)
RESTARTS_QUERY = 'kube_pod_container_status_restarts_total{namespace="ecommerce"}'
TERMINATED_QUERY = 'kube_pod_container_status_last_terminated_reason{namespace="ecommerce"} == 1'


def service_pod_prefix(service: str) -> str:
    """Base pod-name prefix for a service name that may carry a namespace-style
    prefix.

    Alert labels name services ``ecommerce-user-service``; the pods themselves
    (and their Deployment) are named ``user-service-<hash>-<hash>``. Without
    stripping this prefix, every pod-scoping check below silently matches
    nothing and the caller falls back to reasoning over the whole namespace.
    """
    prefix = "ecommerce-"
    return service[len(prefix) :] if service.startswith(prefix) else service


def pod_belongs_to_service(pod: str, service: str) -> bool:
    """Whether ``pod`` (a full pod name) is this incident's own service.

    Namespace-wide pod-restart/CPU/memory queries return every pod in
    ``ecommerce``, not just the affected one — an unrelated pod's stale crash
    or resource spike must never outrank the affected service's own evidence.
    """
    base = service_pod_prefix(service)
    return bool(base) and pod.startswith(base + "-")


def latency_query(bucket: str) -> str:
    """The p95 query for one histogram bucket."""
    return f"histogram_quantile(0.95, sum by (le) (rate({bucket}[5m])))"


def latency(q: QueryFn = _q) -> list[str]:
    out: list[str] = []
    for bucket, (label, threshold) in LATENCY_HISTOGRAMS.items():
        p95 = _scalar(latency_query(bucket), q)
        if p95 is None:
            continue
        note = "  (ABOVE the 2s threshold)" if threshold and p95 > threshold else ""
        out.append(f"{label} p95 latency: {p95:.2f}s{note}")
    return out


def resource_saturation(service: str, q: QueryFn = _q) -> list[str]:
    """Container CPU and memory against their limits, for ``service``'s own pods.

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

    The queries themselves are namespace-wide (see ``CPU_QUERY``/``MEM_QUERY``),
    so rows are filtered to this incident's own service here — otherwise an
    unrelated pod's resource spike would report as if it were this service's.
    """
    out: list[str] = []
    for row in q(CPU_QUERY):
        pod = (row.get("metric") or {}).get("pod", "?")
        if not pod_belongs_to_service(pod, service):
            continue
        try:
            cores = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if cores >= _CPU_REPORT_FLOOR_CORES:
            out.append(f"pod {pod}: cpu={cores:.2f} cores (limit 1)")

    for row in q(MEM_QUERY):
        pod = (row.get("metric") or {}).get("pod", "?")
        if not pod_belongs_to_service(pod, service):
            continue
        try:
            ratio = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if ratio >= _MEM_REPORT_FLOOR_RATIO:
            out.append(f"pod {pod}: memory={ratio:.0%} of its limit")
    return out


def datastore_health(service: str, q: QueryFn = _q) -> list[str]:
    """Is this service's OWN backing datastore StatefulSet actually up.

    Checked at the cluster level via ``kube_statefulset_status_replicas_ready``,
    which is published by kube-state-metrics about the datastore's own pod(s) —
    not by the consuming service. It still answers correctly when the service
    depending on that store cannot report on itself at all (see
    ``DATASTORE_STATEFULSETS`` above for why that matters).
    """
    entry = DATASTORE_STATEFULSETS.get(service_pod_prefix(service))
    if not entry:
        return []
    statefulset, label = entry
    ready = _scalar(datastore_ready_query(statefulset), q)
    if ready is None:
        return []
    if ready < 1:
        return [f"{label} StatefulSet '{statefulset}': 0 ready replicas"]
    return [f"{label} StatefulSet '{statefulset}': {ready:g} ready replica(s)"]


def pod_state(service: str, q: QueryFn = _q) -> list[str]:
    """Restart counts and termination reasons, for ``service``'s own pods.

    This is what separates the two failures that share the EcommerceServiceDown
    alert: `OOMKilled` means the memory leak, plain `Error` before the port
    binds means the crashloop. Without it the model cannot tell them apart.

    Filtered to this incident's own service for the same reason as
    ``resource_saturation`` — ``RESTARTS_QUERY``/``TERMINATED_QUERY`` are
    namespace-wide, and an unrelated pod's stale restart count must not be
    reported (or scored) as if it belonged to the affected service.
    """
    out: list[str] = []
    for row in q(RESTARTS_QUERY):
        pod = (row.get("metric") or {}).get("pod", "?")
        if not pod_belongs_to_service(pod, service):
            continue
        try:
            n = int(float(row["value"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if n > 0:
            out.append(f"pod {pod}: restartCount={n}")
    for row in q(TERMINATED_QUERY):
        m = row.get("metric") or {}
        pod = m.get("pod", "?")
        if not pod_belongs_to_service(pod, service):
            continue
        if reason := m.get("reason"):
            out.append(f"pod {pod}: last terminated reason={reason}")
    return out


ALERTS_QUERY_ID = "observability.metrics.alerts"
"""Section-request key for the firing-alerts lookup.

Named after the capability rather than a made-up label: RCA has exactly one call to
this capability, so there is no second query on the same source to disambiguate from,
and reusing the capability name means anyone reading a context section's ``raw`` dict
recognises the key immediately. ``agents/rca_agent/context_adapter.py`` uses this same
constant when building the request and when reading the answer back, so the two
cannot drift apart.
"""

LOGS_QUERY_ID = "rca.recent_logs"
"""Section-request key for the recent-error-logs lookup. Namespaced with ``rca.``
because, unlike alerts, ``logs`` is a source other agents also request — an
unprefixed ``"recent"`` could collide with a different agent's label for the same
section in a future shared build."""


def _live_alerts() -> Any:
    return get_registry().call("observability.metrics.alerts")


def firing_alerts(fetch: Callable[[], Any] = _live_alerts) -> list[str]:
    try:
        res = fetch()
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


def otel_service_name(service: str) -> str:
    """Normalize to the OTel ``service.name`` convention this SUT actually uses.

    Both Loki's ``service_name`` label and Jaeger's process ``serviceName`` are
    ``ecommerce-<service>`` (confirmed live against
    ``/loki/api/v1/label/service_name/values`` and ``/api/services`` — e.g.
    ``ecommerce-payment-service``), but ``service`` here is the RCA agent's own
    identity, which arrives WITHOUT that prefix (e.g. ``payment-service``, from
    ``triage_verdict.affected_service``). Querying either backend with the raw
    service name silently matches nothing — same class of mismatch as
    ``pod_belongs_to_service`` above, just prefixed the opposite way.
    """
    return f"ecommerce-{service_pod_prefix(service)}"


def _live_logs(service: str) -> Any:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return get_registry().call(
        "observability.logs.query",
        service=otel_service_name(service),
        start=now - timedelta(minutes=15),
        end=now,
        limit=200,
    )


def _live_traces(service: str) -> Any:
    return get_registry().call(
        "observability.traces.search", service=otel_service_name(service), lookback="15m", limit=20
    )


def trace_health(service: str, fetch: Callable[[str], Any] = _live_traces) -> list[str]:
    """Recent traces for the service, and whether any carry a real error status.

    A third, independent evidence source alongside metrics and logs — a trace is
    an actual end-to-end request record, so "0 of N recent traces show an error"
    is itself decisive negative evidence, not merely an absent count. Never
    raises; returns [] on any failure or when nothing was found (this endpoint
    was just deployed onto a live SUT, so an empty trace window is common and
    not itself informative — unlike ``recent_logs``'s explicit "NONE" handling,
    a genuinely empty trace search is treated as unchecked rather than checked).
    """
    try:
        res = fetch(service)
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    traces = data.get("traces") or []
    total = len(traces)
    if total == 0:
        return []
    errors = sum(1 for t in traces if t.get("has_error"))
    out = [f"traces: {errors} of {total} recent traces show an error status"]
    durations = [t["duration_us"] for t in traces if isinstance(t.get("duration_us"), (int, float))]
    if durations:
        out.append(f"traces: slowest recent trace root span {max(durations) / 1000:.0f}ms")
    return out


def recent_logs(
    service: str,
    limit: int = 12,
    fetch: Callable[[str], Any] = _live_logs,
) -> list[str]:
    """Recent ERROR-level lines for the service.

    The ``streams[:5]`` / ``values[:limit]`` walk with its early return is
    grouping-order dependent, which is exactly why the context layer hands this
    function Loki's original ``streams`` structure rather than a flattened line list:
    flattening would silently change *which* twelve lines the model sees.
    """
    try:
        res = fetch(service)
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


def live_commits(path: str | None, limit: int) -> Any:
    return get_registry().call("scm.commit.history", path=path, limit=limit)


def recent_changes(
    path: str | None = None,
    limit: int = 5,
    fetch: Callable[[str | None, int], Any] = live_commits,
) -> list[str]:
    """Commits touching the service — change correlation.

    A deploy minutes before onset is the most common real-world root cause and
    the one signal metrics/logs/traces structurally cannot provide.

    Note this is a *different* query from ``agent.py::_fetch_change_evidence``: no
    ``since``, ``limit=5``, and a ``demo/ecommerce/<service>`` path rather than the
    curated ``_SERVICE_SOURCE_PATHS`` map. Both results are rendered into the same
    prompt as separate sections, so they must stay two queries — merging them would
    change the prompt.
    """
    try:
        res = fetch(path, limit)
    except Exception:
        return []
    if not getattr(res, "ok", False):
        return []
    data = getattr(res, "data", None) or {}
    return [
        f"{c.get('sha', '')} {c.get('date', '')} {c.get('message', '')}"
        for c in (data.get("commits") or [])
    ]


def required_promql_queries() -> tuple[str, ...]:
    """Every PromQL string ``gather`` will issue through ``Backend.query``.

    Derived from the same private constants the query functions read
    (``DEP_GAUGES``, ``LATENCY_HISTOGRAMS``) or literally copied where a function
    builds its own query text inline (``error_breakdown``, ``resource_saturation``,
    ``pod_state``) — never hand-duplicated. This is what
    ``agents/rca_agent/context_adapter.py`` calls to know which sections to request
    from the Context Engineering Layer *before* any evidence is gathered, and because
    it is derived rather than copied, a new gauge or histogram added to this module
    is requested automatically instead of silently missing from the context path.
    """
    return (
        *DEP_GAUGES,
        ORDERS_FAILED_QUERY,
        PAYMENT_FAILURES_QUERY,
        PAYMENT_TIMEOUT_QUERY,
        *(latency_query(bucket) for bucket in LATENCY_HISTOGRAMS),
        CPU_QUERY,
        MEM_QUERY,
        RESTARTS_QUERY,
        TERMINATED_QUERY,
        *(datastore_ready_query(statefulset) for statefulset, _ in DATASTORE_STATEFULSETS.values()),
    )


class Backend(Protocol):
    """Where ``gather`` gets its raw material.

    Four methods because the evidence comes from four different capabilities, not
    because the categories differ — six of the eight are PromQL and share ``query``.

    Implemented twice: ``LiveBackend`` below (the registry, unchanged behaviour) and
    ``agents/rca_agent/context_adapter.py`` (rows the Context Engineering Layer
    already collected). Everything downstream of these four methods — the floors, the
    NaN guard, every format string, the key insertion order — runs identically either
    way, which is what makes the two paths byte-identical by construction rather than
    by two people keeping two copies in sync.
    """

    def query(self, promql: str) -> list[dict[str, Any]]: ...
    def alerts(self) -> Any: ...
    def logs(self, service: str) -> Any: ...
    def commits(self, path: str | None, limit: int) -> Any: ...


class LiveBackend:
    """The default: every lookup goes to the tool registry, as it always has."""

    def query(self, promql: str) -> list[dict[str, Any]]:
        return _q(promql)

    def alerts(self) -> Any:
        return _live_alerts()

    def logs(self, service: str) -> Any:
        return _live_logs(service)

    def commits(self, path: str | None, limit: int) -> Any:
        return live_commits(path, limit)


class CachingBackend:
    """Memoises one backend for the life of a single investigation.

    Two consumers now read the same telemetry: ``gather`` builds the prompt's prose and
    ``investigation/facts.py`` builds typed facts for the reasoning stages. Without this
    they would each issue the full query set, doubling ~14 HTTP round-trips per incident
    against Prometheus — the provider talks to httpx directly with no cache of its own.

    It also buys correctness, not just speed: the prompt and the evidence matrix are
    guaranteed to describe *the same readings*. Querying twice across a live incident can
    return different numbers, which would leave an operator reading a prompt that cites a
    value the score was not computed from.

    Scoped per call and thrown away, so it can never serve stale data into a later
    incident — the cache lifetime is one ``analyze``.
    """

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self._queries: dict[str, list[dict[str, Any]]] = {}
        self._alerts: Any | None = None
        self._logs: dict[str, Any] = {}

    def query(self, promql: str) -> list[dict[str, Any]]:
        if promql not in self._queries:
            self._queries[promql] = self._inner.query(promql)
        return self._queries[promql]

    def alerts(self) -> Any:
        if self._alerts is None:
            self._alerts = self._inner.alerts()
        return self._alerts

    def logs(self, service: str) -> Any:
        if service not in self._logs:
            self._logs[service] = self._inner.logs(service)
        return self._logs[service]

    def commits(self, path: str | None, limit: int) -> Any:
        # Deliberately not cached: the two commit queries in this agent differ by path and
        # limit, and keying a cache on both for a call made at most twice buys nothing.
        return self._inner.commits(path, limit)


def gather(service: str, backend: Backend | None = None) -> dict[str, list[str]]:
    """Collect every evidence category. Never raises.

    **Key insertion order is a contract**, not an implementation detail:
    ``agent.py`` renders ``", ".join(f"{k}={len(v)}" for k, v in observed.items())``
    into ``RCAVerdict.audit_metadata.decision_trace``, which is dashboard-visible. It
    is also deliberately *different* from ``render``'s display order, which follows
    the ``titles`` table. Anything reproducing this function must reproduce this
    order, not that one.

    A category is omitted entirely when empty (``if rows :=``), because ``render``
    distinguishes an absent-but-required key — which it prints as
    ``NONE — <explanation>`` — from an absent-and-optional one, which it omits. Emitting
    ``{"latency": []}`` instead of omitting the key would change the prompt.
    """
    api: Backend = backend or LiveBackend()
    ev: dict[str, list[str]] = {}
    for name, fn in (
        ("firing_alerts", lambda: firing_alerts(api.alerts)),
        ("dependency_health", lambda: dependency_health(api.query)),
        ("error_breakdown", lambda: error_breakdown(api.query)),
        ("latency", lambda: latency(api.query)),
        ("pod_state", lambda: pod_state(service, api.query)),
        ("resource_saturation", lambda: resource_saturation(service, api.query)),
        ("datastore_health", lambda: datastore_health(service, api.query)),
        ("trace_health", lambda: trace_health(service)),
    ):
        try:
            if rows := fn():
                ev[name] = rows
        except Exception:
            logger.debug("evidence %s failed", name, exc_info=True)
    for name, fn2, arg in (
        ("recent_logs", lambda s: recent_logs(s, fetch=api.logs), service),
        (
            "recent_changes",
            lambda p: recent_changes(p, fetch=api.commits),
            f"demo/ecommerce/{service}",
        ),
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
        "datastore_health": (
            "Backing datastore health (cluster-level check, independent of the "
            "affected service's own gauge)"
        ),
        "trace_health": "Recent distributed traces (error status + latency, a third source independent of metrics/logs)",
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
