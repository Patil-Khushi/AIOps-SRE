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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiops.tools.mock_providers

# Side-effect imports: register providers with the registry.
# observability registers live Prometheus + Jaeger; mock_providers contributes
# only the CMDB + on-call lookups (static tables, no live CMDB/PagerDuty wired).
import aiops.tools.observability  # noqa: F401
from agents.alert_triage.models import (
    Alert,
    AuditMetadata,
    Severity,
    Status,
    TriageVerdict,
)
from agents.alert_triage.prompts import (
    SEVERITY_PROMPT_USER,
    SUMMARY_PROMPT_USER,
    SYSTEM_PROMPT,
)
from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.state import repository as state_repo
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# ─── tunables ───────────────────────────────────────────────────────────────
_CUSTOMER_FACING = {
    "frontend", "frontend-proxy", "checkout", "payment", "cart",
    "currency", "ad", "recommendation", "shipping",
}
_DEDUP_WINDOW = timedelta(minutes=5)
_EMBEDDING_SIM_THRESHOLD = 0.85

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


# ─── dedup (persisted clusters + in-memory embedding cache) ────────────────
#
# Cluster identity (key, alert list, last_seen) lives in SQLite via
# aiops.state.repository so dedup survives uvicorn restarts. Embedding vectors
# stay in-process — they're large (~1.5 KB each), per-cluster, and cheap to
# regenerate. After a restart, the exact-key path keeps working from the first
# new alert; the embedding-similarity path is cold for one 5-min window.


@dataclass
class _DedupHit:
    cluster_key: str
    is_new: bool
    method: str  # "exact" | "embedding" | "new"
    alert_count: int
    source_alerts: list[str]


# cluster_key -> latest L2-normalized embedding (numpy array). Bounded by the
# 5-min eviction sweep below.
_EMBED_CACHE: dict[str, Any] = {}


def _evict_embed_cache(active_keys: set[str]) -> None:
    stale = [k for k in _EMBED_CACHE if k not in active_keys]
    for k in stale:
        _EMBED_CACHE.pop(k, None)


def _dedup(alert: Alert) -> _DedupHit:
    """Resolve ``alert`` against the persistent cluster store.

    Stage 1 — exact cluster_key match against an active cluster.
    Stage 2 — embedding cosine similarity ≥ threshold against any active
              cluster whose embedding is in the in-memory cache.
    Stage 3 — new cluster.
    """
    state_repo.evict_expired_clusters(_DEDUP_WINDOW)

    key = alert.cluster_key()
    existing = state_repo.find_active_cluster(key, window=_DEDUP_WINDOW)
    if existing is not None:
        updated = state_repo.upsert_cluster(
            cluster_key=key,
            service=alert.service,
            metric=alert.metric,
            alert_id=alert.alert_id,
            seen_at=alert.timestamp,
        )
        return _DedupHit(
            cluster_key=key,
            is_new=False,
            method="exact",
            alert_count=updated["alert_count"],
            source_alerts=updated["source_alerts"],
        )

    # Embedding-similarity path
    model = _get_embed_model()
    new_emb_norm = None
    if model is not None:
        try:
            import numpy as np

            emb = model.encode(_alert_text_for_embedding(alert), convert_to_numpy=True)
            new_emb_norm = emb / (float(np.linalg.norm(emb)) + 1e-9)
            active = state_repo.list_active_clusters(_DEDUP_WINDOW)
            _evict_embed_cache({c["cluster_key"] for c in active})
            for cluster in active:
                cached = _EMBED_CACHE.get(cluster["cluster_key"])
                if cached is None:
                    continue
                sim = float(np.dot(new_emb_norm, cached))
                if sim >= _EMBEDDING_SIM_THRESHOLD:
                    updated = state_repo.upsert_cluster(
                        cluster_key=cluster["cluster_key"],
                        service=alert.service,
                        metric=alert.metric,
                        alert_id=alert.alert_id,
                        seen_at=alert.timestamp,
                    )
                    _EMBED_CACHE[cluster["cluster_key"]] = new_emb_norm
                    return _DedupHit(
                        cluster_key=cluster["cluster_key"],
                        is_new=False,
                        method="embedding",
                        alert_count=updated["alert_count"],
                        source_alerts=updated["source_alerts"],
                    )
        except Exception as exc:
            logger.warning("embedding similarity skipped: %s", exc)

    created = state_repo.upsert_cluster(
        cluster_key=key,
        service=alert.service,
        metric=alert.metric,
        alert_id=alert.alert_id,
        seen_at=alert.timestamp,
    )
    if new_emb_norm is not None:
        _EMBED_CACHE[key] = new_emb_norm
    return _DedupHit(
        cluster_key=key,
        is_new=True,
        method="new",
        alert_count=created["alert_count"],
        source_alerts=created["source_alerts"],
    )


def reset_dedup_store() -> None:
    """Wipe the in-memory embedding cache. Persistent cluster rows are
    untouched — call ``aiops.state`` directly if a test needs a clean DB."""
    _EMBED_CACHE.clear()


def reset_state() -> None:
    """Eval-harness hook (A11). Wipe persistent cluster rows + the in-memory
    embedding cache so each golden case starts from a clean dedup state."""
    state_repo.delete_all_clusters()
    reset_dedup_store()


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
    except Exception as exc:
        logger.warning("LLM severity classify failed: %s", exc)
    return "Sev-3", 0.4


# ─── stage 4: correlate ─────────────────────────────────────────────────────


def _build_promql_queries(alert: Alert) -> dict[str, str]:
    """Build a small bundle of PromQL queries that give the LLM real context.

    Targets the OpenTelemetry demo's HTTP client instrumentation metrics
    (every service exports ``http_client_duration_milliseconds_*`` for its
    outbound calls). 5-minute rate window leaves enough samples even when
    a service's traffic is sparse.
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
        except Exception as exc:
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
        except Exception as exc:
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
    except Exception as exc:
        logger.warning("LLM summary failed: %s", exc)
        return _template_summary(alert)


# ─── entry point ────────────────────────────────────────────────────────────


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out shim around ``triage``."""
    return triage(Alert(**input)).model_dump(mode="json")


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

    # Stage 3: deduplicate (persisted via aiops.state)
    hit = _dedup(alert)
    duplicate_count = hit.alert_count
    if hit.is_new:
        decision_trace.append("new alert cluster")
        status: Status = "Active"
    else:
        decision_trace.append(
            f"matched duplicate alert cluster via {hit.method} match (size={duplicate_count})"
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

    # Stage 8: assemble + persist
    audit = AuditMetadata(
        created_at=datetime.now(UTC),
        created_by="RA-001",
        source_alerts=list(hit.source_alerts),
        decision_trace=decision_trace,
    )
    verdict = TriageVerdict(
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
    try:
        state_repo.save_verdict(verdict, cluster_key=hit.cluster_key)
    except Exception as exc:
        logger.warning("verdict persistence failed: %s", exc)
    return verdict
