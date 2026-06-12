# RA-007 — Log Correlation Agent

Reactive-Active phase. Fills the evidence gap between Auto-Ticketing and RCA:

```
Alert Triage (RA-001) → Incident Classifier (RA-002) → Auto-Ticketing (RA-003)
        → Log Correlation (RA-007) → RCA Agent (PRS-008)
```

Takes a triaged/classified incident (service + time window), pulls **logs**
(OpenSearch — the OTel demo's built-in log store; Loki also supported),
**traces** (Jaeger), and **metrics** (Prometheus) for that window, correlates
them on a shared timeline, fingerprints recurring errors, and names the most
likely failing component(s). Output is the catalog's **"correlated
evidence pack"** + **"suspect components"** — designed to drop straight into the
RCA agent as evidence.

**Status:** Phase 1. Read-only — HITL level **None** (like RA-001). The
`observability.*` capabilities map to `level=none` at the platform gate, so the
agent physically cannot take a write action. The authoritative contract is the
RA-007 row of `docs/Adaptive_AIOps_Agent_Catalog.xlsx` (Reactive-Active sheet).

| Catalog field | Value |
|---|---|
| Agent ID | RA-007 |
| Inputs | logs, traces, metrics, topology |
| Outputs | correlated evidence pack, suspect components |
| HITL | None |
| KPI | MTTI reduction, evidence completeness |

## Public surface

```python
from agents.log_correlation import (
    CorrelationInput, CorrelationResult, CorrelatedSignal, TimeWindow,
    correlate, run, reset_state,
)
```

### Contract

| Symbol | Shape |
|---|---|
| `CorrelationInput` | `{ service, window: {start, end}, triage_verdict?: dict, classification?: dict, topology?: {service: [deps]} }`. The upstream verdicts are carried as **dicts** (RA-001/RA-002 JSON), not imported classes — RA-007 couples only to the wire contract so it stays independently sellable. |
| `CorrelatedSignal` | `{ source: logs\|traces\|metrics, signature, timestamp, severity, sample }`. |
| `CorrelationResult` | `{ service, summary, timeline: [CorrelatedSignal], top_signatures: [str], suspected_dependencies: [str], confidence (0..1), audit_metadata }`. `suspected_dependencies` is the catalog's "suspect components"; the whole object is the "evidence pack". |
| `correlate(payload)` | `CorrelationInput → CorrelationResult`. Rule-based correlation first (timeline order, signature clustering, first-error, error-rate spike, topology-aware suspects), then an LLM consult that summarizes/ranks — with a deterministic fallback when the gateway is unreachable. |

## How it works

1. **Topology** — uses the supplied `topology` map, else resolves downstream
   dependencies via the `itsm.cmdb.dependencies` capability.
2. **Fan-out fetch** — logs / traces / metrics queries run in parallel in a
   `ThreadPoolExecutor` (same pattern as RA-001's metric correlation). Every
   call goes through `get_registry().call(...)`; no direct SDKs. The logs
   backend is provider-swappable via `AIOPS_LOGS_PROVIDER` (`opensearch`
   default, `loki` alternate) — the agent passes `service` + window, never a
   backend-specific query, so the swap needs no code change.
3. **Synthetic fallback** — when the backends are unreachable (no cluster, the
   default for `--fixture` and CI), deterministic signals keyed by service are
   synthesized so the demo is still meaningful. `audit_metadata.signal_source`
   records `live` vs `synthetic` so a verdict is never mistaken for live data.
   (Same idea as the RCA agent's deterministic fallback.)
4. **Rule-based correlation** — order by timestamp, fingerprint + group
   signatures, flag the first error and any error-rate spike, and derive
   suspect components topology-aware (a downstream dep named in the evidence
   wins; otherwise the error is service-internal).
5. **LLM summary** — ranks the evidence into a one-paragraph headline; falls
   back to a deterministic template on stub/unreachable gateway.

## Run locally

```powershell
uv run python -m agents.log_correlation --list
uv run python -m agents.log_correlation --fixture slow_product_catalog
uv run python -m evals.harness --agent log_correlation
```

No cluster required — the synthetic fallback makes every fixture demoable
offline. Point at a live stack by setting `AIOPS_OPENSEARCH_URL` (or
`AIOPS_LOKI_URL`) / `AIOPS_PROMETHEUS_URL` / `AIOPS_JAEGER_URL` (defaults are
the `start.ps1` port-forwards) and the agent uses real signals automatically.
Browse the underlying logs in Grafana Explore (`:8080/grafana/explore`) once the
OpenSearch datasource is applied (`kubectl apply -f infra/opensearch-grafana-datasource.yaml`).

## Feeding RCA

`CorrelationResult` is accepted by the RCA agent as optional evidence:

```python
from agents.log_correlation import correlate, CorrelationInput
from agents.rca_agent import analyze

evidence = correlate(CorrelationInput(service="product-catalog", window=window))
verdict = analyze(triage_verdict, correlation=evidence.model_dump(mode="json"))
```

When `correlation` is supplied, RCA folds the ranked signatures, suspect
components, and summary into its reasoning prompt. The wiring is additive —
RCA's behavior is unchanged when it's omitted.

## Layout

```
agents/log_correlation/
├── README.md
├── __init__.py
├── __main__.py          # CLI runner (--list / --fixture)
├── agent.py             # entry point: correlate() + fan-out + rule-based + LLM + synthetic fallback
├── models.py            # CorrelationInput, CorrelatedSignal, CorrelationResult, TimeWindow
├── prompts.py           # SYSTEM_PROMPT, SUMMARY_PROMPT_USER
└── evals/
    └── golden.json      # hand-built golden cases
```
