"""Alert Triage agent (RA-001) — 8-stage triage flow.

Entry point: ``triage(alert: Alert) -> TriageVerdict``.

Stages (each appends an entry to ``audit_metadata.decision_trace``):

    1. Validate           (pydantic on Alert construction)
    2. Normalize          (canonical Alert built by caller in v1)
    3. Dedup              (rule-based cluster_key + optional embedding similarity)
    4. Correlate          (registry → Prometheus metrics + Jaeger traces)
    5. Severity classify  (rule-based first, LLM consult if ambiguous)
    6. Ownership          (registry → CMDB lookup + on-call lookup)
    7. Summary            (LLM-generated, deterministic fallback)
    8. Assemble verdict   (TriageVerdict + AuditMetadata)

Vendor-neutrality: this module imports ``aiops.llm`` and ``aiops.tools`` only.
No SDK imports. Tool calls go through ``get_registry().call(capability, ...)``.

Embedding dedup: optional dependency ``sentence-transformers`` (install via
``uv sync --extra embeddings``). When absent, dedup falls back to rule-based.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.tools import get_registry

# Side-effect imports: register providers with the registry.
# observability registers live Prometheus + Jaeger; mock_providers contributes
# only the CMDB + on-call lookups (static tables, no live CMDB/PagerDuty wired).
import aiops.tools.observability  # noqa: F401, E402
import aiops.tools.mock_providers  # noqa: F401, E402

from agents.alert_triage.models import (  # noqa: E402
    Alert,
    AuditMetadata,
    Severity,
    Status,
    TriageVerdict,
)
from agents.alert_triage.prompts import (  # noqa: E402
    SEVERITY_PROMPT_USER,
    SUMMARY_PROMPT_USER,
    SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# ─── tunables ───────────────────────────────────────────────────────────────
_CUSTOMER_FACING = {
    "frontend", "frontend-proxy", "checkout", "payment", "cart",
    "currency", "ad", "recommendation", "shipping",
}
_DEDUP_WINDOW = timedelta(minutes=5)
_EMBEDDING_SIM_THRESHOLD = 0.85
_DEDUP_HISTORY_MAX = 1000

# ─── embedding model (lazy, optional) ───────────────────────────────────────
_EMBED_MODEL: Any = None  # None=unloaded, False=unavailable, else model object


def _get_embed_model() -> Any | None:
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer

            _EMBED_MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            logger.info("loaded sentence-transformers embedding model")
        except ImportError:
            logger.info("sentence-transformers not installed; using rule-based dedup only")
            _EMBED_MODEL = False
    return _EMBED_MODEL if _EMBED_MODEL else None


def _alert_text_for_embedding(alert: Alert) -> str:
    parts = [f"{alert.metric} on {alert.service} value={alert.value}"]
    desc = alert.annotations.get("description") or alert.annotations.get("summary")
    if desc:
        parts.append(desc)
    return " | ".join(parts)


# ─── dedup store (in-memory, process-local) ─────────────────────────────────


@dataclass
class _Cluster:
    cluster_key: str
    alerts: list[Alert] = field(default_factory=list)
    embeddings: list[Any] = field(default_factory=list)  # numpy arrays
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class _DedupStore:
    """In-memory dedup store, 5-min sliding window. Single-process; not
    thread-safe. Phase-1 follow-up: swap to Redis with TTL keys (capability
    ``cache.kv.set`` / ``cache.kv.get`` — not yet defined)."""

    def __init__(self) -> None:
        self._clusters: dict[str, _Cluster] = {}
        self._order: deque[str] = deque(maxlen=_DEDUP_HISTORY_MAX)

    def _evict_expired(self, now: datetime) -> None:
        expired = [k for k, c in self._clusters.items() if now - c.last_seen > _DEDUP_WINDOW]
        for k in expired:
            del self._clusters[k]

    def find_or_create(self, alert: Alert) -> tuple[_Cluster, bool, str]:
        """Returns (cluster, is_new, dedup_method).

        dedup_method is "exact" | "embedding" | "new"."""
        now = alert.timestamp
        self._evict_expired(now)

        # Stage 1: exact key match
        key = alert.cluster_key()
        if key in self._clusters:
            c = self._clusters[key]
            c.alerts.append(alert)
            c.last_seen = now
            return c, False, "exact"

        # Stage 2: embedding similarity
        model = _get_embed_model()
        new_emb_norm = None
        if model is not None:
            try:
                import numpy as np  # noqa: PLC0415 — optional dep, only when embeddings enabled

                emb = model.encode(_alert_text_for_embedding(alert), convert_to_numpy=True)
                new_emb_norm = emb / (float(np.linalg.norm(emb)) + 1e-9)
                for c in self._clusters.values():
                    if not c.embeddings:
                        continue
                    sim = float(np.dot(new_emb_norm, c.embeddings[-1]))
                    if sim >= _EMBEDDING_SIM_THRESHOLD:
                        c.alerts.append(alert)
                        c.embeddings.append(new_emb_norm)
                        c.last_seen = now
                        return c, False, "embedding"
            except Exception as exc:  # noqa: BLE001
                logger.warning("embedding similarity skipped: %s", exc)

        # New cluster
        c = _Cluster(
            cluster_key=key,
            alerts=[alert],
            embeddings=[new_emb_norm] if new_emb_norm is not None else [],
            last_seen=now,
        )
        self._clusters[key] = c
        self._order.append(key)
        return c, True, "new"


# Module-level singleton. Resettable by tests via ``reset_dedup_store()``.
_DEDUP = _DedupStore()


def reset_dedup_store() -> None:
    """Wipe the dedup memory. For tests/evals that need a clean slate."""
    global _DEDUP
    _DEDUP = _DedupStore()


# ─── stage 5: severity classification ───────────────────────────────────────


def _is_customer_facing(service: str) -> bool:
    s = service.lower()
    return s in _CUSTOMER_FACING or any(cf in s for cf in _CUSTOMER_FACING)


def _classify_severity_rule_based(alert: Alert) -> tuple[Severity | None, float]:
    """Rule-based classifier. Returns (severity, confidence) or (None, 0.5)
    when the rules don't apply and the LLM should consult."""
    s_hint = (alert.severity_hint or "").lower()
    if s_hint:
        if "critical" in s_hint or "p1" in s_hint or "sev-1" in s_hint:
            return "Sev-1", 0.95
        if "high" in s_hint or "p2" in s_hint or "sev-2" in s_hint:
            return "Sev-2", 0.90
        if "warning" in s_hint or "p3" in s_hint or "sev-3" in s_hint:
            return "Sev-3", 0.85
        if "info" in s_hint or "low" in s_hint or "p4" in s_hint or "sev-4" in s_hint:
            return "Sev-4", 0.85

    cust = _is_customer_facing(alert.service)

    if alert.threshold is not None and alert.threshold > 0:
        ratio = alert.value / alert.threshold
        if ratio >= 2.0 and cust:
            return "Sev-1", 0.90
        if ratio >= 1.5 and cust:
            return "Sev-2", 0.85
        if ratio >= 1.0 and cust:
            return "Sev-2", 0.75
        if ratio >= 1.0:
            return "Sev-3", 0.80
        return "Sev-4", 0.75

    metric_lower = alert.metric.lower()
    if "cpu" in metric_lower or "memory" in metric_lower:
        if alert.value >= 95:
            return ("Sev-1", 0.85) if cust else ("Sev-2", 0.80)
        if alert.value >= 80:
            return ("Sev-2", 0.75) if cust else ("Sev-3", 0.75)

    return None, 0.5


def _classify_severity_llm(alert: Alert) -> tuple[Severity, float]:
    """LLM consult for ambiguous severity. Returns (severity, confidence).
    Defensive: falls back to (Sev-3, 0.4) if the LLM response can't be parsed
    — happens with the stub provider, which is fine for v1 wiring."""
    user_prompt = SEVERITY_PROMPT_USER.format(
        service=alert.service,
        metric=alert.metric,
        value=alert.value,
        threshold=alert.threshold,
        labels=alert.labels,
    )
    try:
        # GPT-5 and o-series spend tokens on internal reasoning *before* emitting
        # text; budget needs to cover that or the response is empty. 1000 is a
        # safe floor (~800 reasoning + 200 output for a short Sev classification).
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        text = resp.text
        sev_m = re.search(r"\bSev-([1-4])\b", text)
        conf_m = re.search(r"confidence[:\s]+([0-9.]+)", text, re.IGNORECASE)
        if sev_m:
            sev: Severity = f"Sev-{sev_m.group(1)}"  # type: ignore[assignment]
            conf = float(conf_m.group(1)) if conf_m else 0.6
            return sev, min(max(conf, 0.0), 1.0)
    except Exception as exc:  # noqa: BLE001 — boundary
        logger.warning("LLM severity classify failed: %s", exc)
    return "Sev-3", 0.4


# ─── stage 4: correlate ─────────────────────────────────────────────────────


def _build_promql_queries(alert: Alert) -> dict[str, str]:
    """Build a small bundle of PromQL queries that give the LLM real context.

    Targets the OpenTelemetry demo's HTTP client instrumentation metrics
    (every service exports ``http_client_duration_milliseconds_*`` for its
    outbound calls). 5-minute rate window because the demo's scrape interval
    is ~30 s, so 1 m is too narrow to compute a rate reliably.

    Note: this chart does NOT enable the spanmetrics processor, so
    ``traces_span_metrics_*`` series are absent. If you turn it on via Helm
    values, switch these queries to that family — they're more accurate.
    """
    svc = alert.service.lower().strip()
    metric = alert.metric.lower()
    queries: dict[str, str] = {
        # Request rate (calls/s) by service
        "request_rate": (
            f'sum by (service_name) '
            f'(rate(http_client_duration_milliseconds_count{{service_name="{svc}"}}[5m]))'
        ),
        # Error rate — http_status_code 5xx
        "error_rate_5xx": (
            f'sum by (service_name) ('
            f'rate(http_client_duration_milliseconds_count'
            f'{{service_name="{svc}",http_status_code=~"5.."}}[5m])'
            f')'
        ),
        # p95 duration (ms) — the most common alerting metric
        "latency_p95_ms": (
            f'histogram_quantile(0.95, sum by (le, service_name) ('
            f'rate(http_client_duration_milliseconds_bucket{{service_name="{svc}"}}[5m])'
            f'))'
        ),
    }
    # Metric-specific add-on for alerts that name CPU / memory explicitly.
    if "cpu" in metric:
        queries["cpu_seconds_rate"] = (
            f'sum(rate(otelcol_process_cpu_seconds_total{{service_name="{svc}"}}[5m]))'
        )
    elif "memory" in metric or "mem" in metric:
        queries["memory_bytes"] = (
            f'avg(otelcol_process_memory_rss_bytes{{service_name="{svc}"}})'
        )
    return queries


def _fetch_metric_context(alert: Alert, trace: list[str]) -> dict[str, Any] | None:
    """Stage-5 metric correlation. Runs several PromQL queries against the
    OTel demo's span-metrics; returns a bundle of named result-sets so the
    summary stage has actual numbers to reason over."""
    registry = get_registry()
    queries = _build_promql_queries(alert)
    results: dict[str, Any] = {}
    for name, promql in queries.items():
        try:
            res = registry.call("observability.metrics.query", promql=promql)
        except KeyError:
            trace.append("metrics_ctx: capability observability.metrics.query not registered")
            return None
        except Exception as exc:  # noqa: BLE001
            trace.append(f"metrics_ctx[{name}]: error ({type(exc).__name__})")
            continue
        if not res.ok:
            trace.append(f"metrics_ctx[{name}]: prometheus error ({res.error})")
            continue
        rows = (res.data or {}).get("results", [])
        # Each Prometheus instant-vector row is [{"metric":{...}, "value":[ts, "v"]}]
        # Reduce to a single scalar per query for the LLM (last sample value).
        for row in rows:
            value = (row.get("value") or [None, None])[1]
            try:
                results[name] = float(value) if value is not None else None
            except (TypeError, ValueError):
                results[name] = value
            break  # one series is enough for the summary
        if name not in results:
            results[name] = None
    if not results:
        return None
    return {"queries": queries, "results": results}


def _fetch_trace_context(alert: Alert, trace: list[str]) -> dict[str, Any] | None:
    candidates = [
        alert.service.lower(),
        alert.service.lower().replace(" ", "-"),
        alert.service.lower().replace(" api", "").replace("-api", ""),
    ]
    for cand in candidates:
        try:
            res = get_registry().call(
                "observability.traces.search", service=cand, lookback="15m", limit=5
            )
        except KeyError:
            trace.append("trace_ctx: capability observability.traces.search not registered")
            return None
        except Exception as exc:  # noqa: BLE001
            trace.append(f"trace_ctx: error ({type(exc).__name__})")
            return None
        if res.ok and (res.data or {}).get("trace_count", 0) > 0:
            return res.data
    trace.append("trace_ctx: no traces found for service")
    return None


# ─── stage 7: summary ───────────────────────────────────────────────────────


def _template_summary(alert: Alert) -> str:
    """Deterministic fallback used when the LLM can't help (stub provider, etc)."""
    if alert.threshold is not None:
        return (
            f"{alert.service} {alert.metric} at {alert.value} above threshold "
            f"{alert.threshold} (source: {alert.source})."
        )
    return f"{alert.service} {alert.metric} reported value {alert.value} (source: {alert.source})."


def _generate_summary(
    alert: Alert,
    metrics_ctx: dict[str, Any] | None,
    traces_ctx: dict[str, Any] | None,
) -> str:
    metric_results = (metrics_ctx or {}).get("results", {}) if metrics_ctx else {}
    # Render the metric bundle as `key=value` pairs so the LLM sees real numbers.
    metric_samples = (
        ", ".join(f"{k}={v}" for k, v in metric_results.items() if v is not None)
        or "no series"
    )
    user_prompt = SUMMARY_PROMPT_USER.format(
        service=alert.service,
        metric=alert.metric,
        value=alert.value,
        threshold=alert.threshold,
        metric_samples=metric_samples,
        trace_count=(traces_ctx or {}).get("trace_count", 0) if traces_ctx else 0,
    )
    try:
        # See note in _classify_severity_llm — GPT-5 burns ~800 tokens on
        # reasoning before producing output, so the budget has to cover both.
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        text = (resp.text or "").strip()
        # Detect stub-provider echo (it prefixes "[stub] echoing user message:")
        if not text or text.startswith("[stub]"):
            return _template_summary(alert)
        # Take first non-empty line, cap length
        first = next((ln for ln in text.split("\n") if ln.strip()), "")
        return first[:200]
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM summary failed: %s", exc)
        return _template_summary(alert)


# ─── entry point ────────────────────────────────────────────────────────────


def triage(alert: Alert) -> TriageVerdict:
    """Triage a single alert. Returns a structured verdict.

    Read-only with respect to external systems — does not open tickets, page
    anyone, or run remediation (those are downstream agents).
    """
    decision_trace: list[str] = []
    # Stage 1+2: validate + normalize — done by Pydantic on Alert construction.
    decision_trace.append(
        f"received alert_id={alert.alert_id} service={alert.service} source={alert.source}"
    )

    # Stage 3: deduplicate
    cluster, is_new, dedup_method = _DEDUP.find_or_create(alert)
    duplicate_count = len(cluster.alerts)
    if is_new:
        decision_trace.append("new alert cluster")
        status: Status = "Active"
    else:
        decision_trace.append(
            f"matched duplicate alert cluster via {dedup_method} match (size={duplicate_count})"
        )
        status = "Suppressed"

    # Stage 4: correlate
    metrics_ctx = _fetch_metric_context(alert, decision_trace)
    if metrics_ctx is not None:
        results = metrics_ctx.get("results", {})
        non_null = {k: v for k, v in results.items() if v is not None}
        if non_null:
            decision_trace.append(
                f"fetched metric bundle: {', '.join(f'{k}={v}' for k, v in non_null.items())}"
            )
        else:
            decision_trace.append(
                f"queried {len(results)} metric series but all returned empty"
            )
    traces_ctx = _fetch_trace_context(alert, decision_trace)
    if traces_ctx is not None:
        decision_trace.append(
            f"fetched {traces_ctx.get('trace_count', 0)} trace summaries from Jaeger"
        )

    # Stage 5: severity
    sev, conf = _classify_severity_rule_based(alert)
    if sev is None:
        sev, conf = _classify_severity_llm(alert)
        decision_trace.append(f"severity inferred from LLM consult ({sev}, confidence={conf:.2f})")
    else:
        decision_trace.append(f"severity from rule-based mapping ({sev}, confidence={conf:.2f})")

    # Stage 6: ownership
    registry = get_registry()
    team = "Platform On-Call"
    runbook: str | None = None
    try:
        cmdb = registry.call("itsm.cmdb.lookup", service=alert.service)
        if cmdb.ok and cmdb.data:
            team = cmdb.data.get("team") or team
            runbook = cmdb.data.get("runbook")
            decision_trace.append(f"assigned to {team} via CMDB lookup")
        else:
            decision_trace.append("CMDB lookup returned no match; defaulted to Platform On-Call")
    except KeyError:
        decision_trace.append("itsm.cmdb.lookup not registered; defaulted to Platform On-Call")

    engineer: str | None = None
    try:
        oc = registry.call("oncall.schedule.lookup", team=team)
        if oc.ok and oc.data:
            engineer = oc.data.get("engineer_email")
            if engineer:
                decision_trace.append(f"on-call engineer resolved ({engineer})")
    except KeyError:
        decision_trace.append("oncall.schedule.lookup not registered; no engineer assigned")

    # Stage 7: summary
    summary = _generate_summary(alert, metrics_ctx, traces_ctx)
    decision_trace.append("generated incident summary")

    # Stage 8: assemble
    audit = AuditMetadata(
        created_at=datetime.now(timezone.utc),
        created_by="RA-001",
        source_alerts=[a.alert_id for a in cluster.alerts],
        decision_trace=decision_trace,
    )
    return TriageVerdict(
        affected_service=alert.service,
        severity=sev,
        confidence_score=conf,
        alert_summary=summary,
        assigned_team=team,
        assigned_engineer=engineer,
        recommended_runbook=runbook,
        duplicate_alert_count=duplicate_count,
        status=status,
        audit_metadata=audit,
    )
