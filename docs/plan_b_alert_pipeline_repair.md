# Plan B — Alert Pipeline Repair

**Goal:** When the user clicks **Inject** on any scenario in the dashboard's *Failure Injection* panel, an alert appears in *Alert Stream* within ~60 s and drives the existing agent chain (Triage → Classifier → Auto-Ticket → Notify).

**Branch posture:** Snapshot of the demo-ready state is frozen at `demo-stable-2026-05-15` (`34cd318`). Plan B work happens on `feat/demo-readiness-cmdb-and-llm-health`. If anything below blows up, `git reset --hard demo-stable-2026-05-15` restores tomorrow's demo unchanged.

**Time budget:** **30 min** for the recommended tier (T1). Hard ceiling: **90 min** total before reverting and reverting to Plan A (UI fixture endpoint, already documented in `RUNNING.md`).

---

## 0. TL;DR

Add **one** Prometheus alert rule to [demo/otel-demo/values.yaml](../demo/otel-demo/values.yaml) that fires when any feature flag is observed in a non-default variant, using the OTel demo's already-scraped `feature_flag_flagd_impression_total` counter. ~12 lines of YAML, one `helm upgrade --reuse-values`, one probe to verify. **No code changes, no agent changes, no infra topology changes.**

---

## 1. Why the existing rules don't fire (re-confirmed)

The current rules in [demo/otel-demo/values.yaml](../demo/otel-demo/values.yaml) (PaymentErrorRateHigh, ProductCatalogErrorRateHigh, AdErrorRateHigh, RecommendationErrorRateHigh) all filter on `status_code="STATUS_CODE_ERROR"`. Live probe (clean baseline, no injections):

| Service | What `traces_span_metrics_calls_total` shows |
|---|---|
| `payment` | only `STATUS_CODE_UNSET` on the user-facing `oteldemo.PaymentService/Charge` span; the only `STATUS_CODE_ERROR` series is on the unrelated `flagd.evaluation.v1.Service/EventStream` plumbing span |
| `product-catalog` | only `STATUS_CODE_UNSET` (2.0/s) |
| `ad` | mostly `STATUS_CODE_UNSET`, ~0.004/s `STATUS_CODE_ERROR` from background noise — not from `adFailure` |

**Conclusion:** the OTel demo's services don't tag their *user-facing* spans as `STATUS_CODE_ERROR` even when the failure flags force them to return errors to clients. The instrumentation gap is in the application, not in the rules. Fixing the rule predicate alone won't help (we can't widen to `!="STATUS_CODE_OK"` because that catches successful traffic too).

---

## 2. Hypothesis ladder

### T1 — `feature_flag_flagd_impression_total` rule (RECOMMENDED)

**What:** add one alert rule that fires when any flag is observed in a non-default variant.

**Why it works:** Prometheus is already scraping the OpenFeature SDK's client-side metrics from every service. Confirmed live: 5 distinct metric names (`feature_flag_evaluation_active_count`, `feature_flag_evaluation_requests_total`, `feature_flag_evaluation_success_total`, `feature_flag_flagd_impression_total`, `feature_flag_flagd_result_reason_total`). The impression counter has labels `feature_flag_key` and `feature_flag_result_variant`. As soon as any service evaluates an injected flag and observes the non-default variant, the impression counter for that `(key, variant!=off)` tuple increments.

**Sample of current series** (baseline — flags all off):

```
feature_flag_key=cartFailure            variant=off  rate=0.13/s
feature_flag_key=emailMemoryLeak        variant=off  rate=0.13/s
feature_flag_key=failedReadinessProbe   variant=off  rate=0.03/s
feature_flag_key=paymentFailure         variant=off  rate=0.00/s
feature_flag_key=paymentUnreachable     variant=off  rate=0.00/s
feature_flag_key=kafkaQueueProblems     variant=off  rate=0.00/s
```

**Confidence:** **HIGH**. Direct, real signal from already-emitted instrumentation. No new collector config, no application code changes, no synthetic gauges.

**Caveat:** evaluation rates vary by flag — `cart`/`email` are evaluated frequently (high baseline rate), `payment`/`kafka` rarely (0.0/s today, only when load-generator hits relevant paths). The rule window must be wide enough that even a low-evaluation-rate flag fires within 60 s of injection. Use `[2m]` window with `for: 15s` to balance liveness vs robustness.

### T2 — Latency-histogram-based rules (FALLBACK)

**What:** add `*LatencyP95High` rules with empirically-calibrated thresholds for services that visibly slow down under their flags.

**Why it might work:** the `traces_span_metrics_duration_milliseconds_bucket` histograms exist for at least `ad` (baseline p95=7.8ms) and `image-provider` (baseline p95=1.9ms). When `adManualGc` or `imageSlowLoad` is injected, p95 should spike well past these baselines.

**Confidence:** **MEDIUM**. Fires only for *latency-affecting* flags (adManualGc, adHighCpu, imageSlowLoad, kafkaQueueProblems). Does **not** cover error-injection flags (paymentFailure, cartFailure, productCatalogFailure) — those need T1 or T3.

**Risk:** thresholds calibrated on a quiet baseline may false-fire if traffic increases naturally. Requires injecting each flag and observing baseline-shifted p95 to set thresholds.

### T3 — Synthetic `scenario_active` gauge (LAST RESORT)

**What:** add a `/metrics` endpoint to the UI server ([demo/ui/server.py](../demo/ui/server.py)) that exports a Prometheus gauge `scenario_active{scenario_id="..."} 1` for every flag currently in non-default variant, and add a scrape config + alert rule for it.

**Why it would work:** UI server already knows the scenario state via `/api/scenarios`. Exporting it as a gauge bypasses the entire collector/spanmetrics chain.

**Confidence:** **VERY HIGH** that it fires, **LOW** narrative purity — this is a synthetic signal that says "we know we injected something", not "the application is unhealthy". Audience might rightly ask "is this real monitoring or just telling me what I just clicked?"

**Risk:** requires app code change (FastAPI endpoint + Prometheus client library — `prometheus_client` is probably already in deps but check), plus a `ServiceMonitor` or scrape-config addition. Bigger blast radius than T1.

---

## 3. Decision flow

```
START
  │
  ├─ Probe T1: does feature_flag_flagd_impression_total go up
  │   for the injected variant within 60 s?
  │     │
  │     ├─ YES (expected) ──▶ ship T1. DONE.
  │     │
  │     └─ NO  ──▶ inspect: is the service evaluating the flag at all?
  │                  │
  │                  ├─ YES, but rate too low ──▶ widen window to [5m],
  │                  │                              re-test.
  │                  │
  │                  └─ NO  ──▶ fall through to T2.
  │
  ├─ Try T2 for latency flags only. Document that error-injection flags
  │   require T1 or T3 to surface as alerts.
  │     │
  │     └─ If T2 also doesn't fire ──▶ fall through to T3.
  │
  └─ T3 final fallback. ~60 min of work. If it doesn't ship cleanly,
      `git reset --hard demo-stable-2026-05-15` and demo via Plan A.
```

---

## 4. T1 implementation (the recommended path, end-to-end)

### 4.1 The exact rule to add

In [demo/otel-demo/values.yaml](../demo/otel-demo/values.yaml), inside the `prometheus.serverFiles."alerting_rules.yml".groups[0].rules` list (the same list that already contains `PaymentErrorRateHigh` et al.), add:

```yaml
- alert: ScenarioFlagActive
  expr: |
    sum by (feature_flag_key, feature_flag_result_variant) (
      rate(feature_flag_flagd_impression_total{feature_flag_result_variant!="off"}[2m])
    ) > 0
  for: 15s
  labels:
    severity: high
    alert_type: scenario_active
    # service label intentionally omitted — this is a control-plane alert,
    # not a per-service one. The agent chain's CMDB lookup will use
    # feature_flag_key to derive the affected service.
  annotations:
    summary: "Failure-injection scenario active: {{ $labels.feature_flag_key }}"
    description: |
      flag={{ $labels.feature_flag_key }} variant={{ $labels.feature_flag_result_variant }}
      eval_rate={{ $value }}/s
    flag_hint: "toggle this off at http://localhost:8080/feature/"
    runbook: "Investigate as a real fault — when this fires unintentionally, a
              chaos test or scenario injection was left on."
```

### 4.2 The exact deployment command

```powershell
# In Window B
cd C:\Projects\AIops
helm upgrade otel-demo open-telemetry/opentelemetry-demo `
  --namespace otel-demo `
  --values demo/otel-demo/values.yaml `
  --reuse-values=false `
  --wait `
  --timeout 5m
```

> **Why `--reuse-values=false`:** we want the new `values.yaml` to fully replace the in-cluster values for this release, ensuring our rule changes propagate. `--reuse-values` (which CLAUDE.md's "common commands" suggests for hot-fixes) would *merge* and may not pick up rule-list reorderings.

### 4.3 Watch for the SSA conflict trap (PR #42 fixed an instance of this)

If the upgrade errors with `Apply failed with N conflicts: conflicts with "kubectl-patch"`, it means the `flagd-config` configmap got patched imperatively at some point (we've established this is not our case today, but be ready). Recovery:

```powershell
.\infra\teardown.ps1
.\infra\bootstrap.ps1
# then re-apply the rule edit and helm upgrade
```

If teardown/bootstrap is needed, the time budget shifts to ~45 min and the demo-stable rollback (§6) becomes the more attractive option.

### 4.4 Verification (4 steps, ~3 minutes total)

```powershell
# 1. Prometheus knows about the new rule
(Invoke-RestMethod http://localhost:9090/api/v1/rules).data.groups |
  ForEach-Object { $_.rules | Where-Object { $_.name -eq 'ScenarioFlagActive' } } |
  Select-Object name, health, query

# 2. Inject one scenario via the dashboard. Or via API:
Invoke-RestMethod -Method POST http://localhost:8765/api/scenarios/ad_manual_gc/inject -TimeoutSec 15

# 3. Within 60 s, the rule should transition to firing
1..6 | ForEach-Object {
  Start-Sleep 10
  $a = (Invoke-RestMethod http://localhost:9090/api/v1/alerts).data.alerts |
       Where-Object { $_.labels.alertname -eq 'ScenarioFlagActive' }
  Write-Host ("t+{0}s  state={1}  flag={2}" -f ($_ * 10), $a.state, $a.labels.feature_flag_key)
}

# 4. Confirm Alert Stream tab in the dashboard shows the alert
Start-Process http://localhost:8765/alerts
# Look for a row with alert_type=scenario_active.

# 5. Reset to clean
.\reset.ps1
```

---

## 5. Verification matrix (per flag)

After T1 ships, every UI scenario must surface an alert. Test grid:

| UI scenario | Flag key | Expected to fire in | Notes |
|---|---|---|---|
| Payment service failure | `paymentFailure` | ≤ 90 s | low baseline eval rate; may need wider window |
| Payment unreachable | `paymentUnreachable` | ≤ 90 s | same as above |
| Cart service failure | `cartFailure` | ≤ 30 s | high baseline eval rate (0.13/s) |
| Ad service failure | `adFailure` | ≤ 30 s | ad service evaluates often |
| Ad service CPU saturation | `adHighCpu` | ≤ 30 s | same |
| Ad service GC stall | `adManualGc` | ≤ 30 s | same |
| Product catalog slow | `productCatalogFailure` | ≤ 60 s | medium baseline rate |
| Recommendation cache failure | `recommendationCacheFailure` | ≤ 60 s | same |
| Image slow load | `imageSlowLoad` | ≤ 60 s | same |
| Kafka queue buildup | `kafkaQueueProblems` | ≤ 90 s | very low baseline (0.0/s today) |
| Email memory leak | `emailMemoryLeak` | ≤ 30 s | high baseline (0.13/s) |
| Failed readiness probe | `failedReadinessProbe` | ≤ 30 s | medium baseline |

**Acceptance gate:** at least **8 of 12** must fire within 60 s. The slow-eval flags (`payment*`, `kafka*`) are allowed to take up to 90 s.

---

## 6. Rollback

Two levels, ordered by cost:

### 6.1 Cheap rollback (Prometheus rule only — ~30 s)

```powershell
git diff HEAD demo/otel-demo/values.yaml   # confirm only the rule changed
git checkout HEAD -- demo/otel-demo/values.yaml
helm upgrade otel-demo open-telemetry/opentelemetry-demo `
  --namespace otel-demo `
  --values demo/otel-demo/values.yaml `
  --reuse-values=false --wait --timeout 5m
```

### 6.2 Nuclear rollback (entire branch — ~10 s + 5 min for helm)

```powershell
git checkout feat/demo-readiness-cmdb-and-llm-health
git reset --hard demo-stable-2026-05-15
# any helm upgrade you did is still in-cluster — re-apply the snapshot's values:
helm upgrade otel-demo open-telemetry/opentelemetry-demo `
  --namespace otel-demo `
  --values demo/otel-demo/values.yaml `
  --reuse-values=false --wait --timeout 5m
```

> **What this does NOT undo:** the new ScenarioFlagActive rule, if helm upgrade applied it before you reset git. The git reset reverts the file; the helm upgrade re-pushes the (now reverted) file to the cluster. Both branches end in the same on-disk + in-cluster state as `demo-stable-2026-05-15`.

---

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `helm upgrade` hits SSA conflict on `flagd-config` | Low (we've already reset cleanly) | High (~30 min recovery via teardown/bootstrap) | Have §6.2 nuclear rollback at the ready. If we hit this, **stop and revert** — don't keep digging at 9 PM the night before the demo. |
| New rule fires constantly even with no flags injected | Low | Medium (noise in Alert Stream) | The `feature_flag_result_variant!="off"` filter is precise. If baseline somehow shows non-off impressions, narrow filter to `feature_flag_result_variant=~"on\|enabled\|100%"`. |
| New rule names conflict with existing rule (causes Prometheus to refuse to load both) | Low | High (no alerts at all) | Pre-check: `grep -n "ScenarioFlagActive" demo/otel-demo/values.yaml` must return zero before the edit. After helm upgrade: `curl /api/v1/rules \| jq '.data.groups[].rules[].name'` must show the new name *once*. |
| Eval-rate too low → alert misses 90 s window for `paymentFailure` / `kafka*` | Medium | Low | Widen window: `[2m]` → `[5m]` and `for: 15s` → `for: 30s`. Cost is 30 s extra to fire; benefit is reliable firing for all flags. |
| Alert appears in Alert Stream but agent chain doesn't pick it up | Medium | Medium | Validate explicitly: trigger ScenarioFlagActive via inject, then check whether the existing alert→triage WebSocket path forwards the alert to the agent chain. If not, we shipped the *Prometheus* half of the fix but not the *agent ingestion* half. **This is the single biggest unknown.** Investigate as part of §4.4 step 4. |
| We spend > 90 min and demo is at risk | Medium | High | Hard stop at 90 min. `git checkout demo-stable-2026-05-15` and demo via Plan A. |

---

## 8. The unknown that needs the most attention

§7's "agent chain doesn't pick up the new alert" risk is the one I'm least sure about. The UI server's `/api/triage/live` and `/api/live-alerts` endpoints currently pull from `/api/v1/alerts` on Prometheus — see [aiops/tools/observability/prometheus.py](../aiops/tools/observability/prometheus.py) and [demo/ui/server.py](../demo/ui/server.py). The path most likely Just Works because the new alert is structurally identical to the old ones (same alertname/labels shape). But there's a chance that:

- the UI's alert-row rendering hard-codes which labels to display (would still show the alert, just ugly)
- the `Triage` button on each alert builds a payload that assumes a `service` label (our new rule omits it intentionally — see §4.1)

**Pre-mitigation:** add a `service` label to the new rule, derived from the flag key via Prometheus's label-replace. If the agent chain needs `service`, we have to map flag→service. Crude mapping table:

| Flag | Service to assign |
|---|---|
| `paymentFailure`, `paymentUnreachable` | payment |
| `cartFailure` | cart |
| `adFailure`, `adHighCpu`, `adManualGc` | ad |
| `productCatalogFailure` | product-catalog |
| `recommendationCacheFailure` | recommendation |
| `imageSlowLoad` | image-provider |
| `kafkaQueueProblems` | kafka |
| `emailMemoryLeak` | email |
| `failedReadinessProbe` | (multiple — leave as `flagd`) |

This is doable in Prometheus rule annotations but ugly. Easier: ship the rule WITH `service: "{{ $labels.feature_flag_key }}"` in labels for now (so the agent's CMDB lookup gets *something*, even if it's the flag name) — then iterate post-demo.

---

## 9. Out of scope (do NOT touch)

- The existing 4 rules (`PaymentErrorRateHigh` et al.) — leave them. They don't fire today but they don't hurt anything.
- The OTel collector's `spanstatus` mapping. Real fix lives there but it's a 2-day investigation.
- The application code that emits spans (Go/Python services in the OTel demo).
- The CMDB seeding work (issue #43, the live-PDI gap surfaced in v2 audit).
- Auto-Ticketing / Notification Router / Classifier wiring — they all consume whatever Triage produces.
- `RUNNING.md` (already snapshotted on `demo-stable-2026-05-15`).

---

## 10. Time budget

| Block | Time | Cumulative |
|---|---|---|
| Edit `values.yaml` (add one rule) | 5 min | 0:05 |
| `helm upgrade --reuse-values=false` | 5 min | 0:10 |
| §4.4 verification probes | 5 min | 0:15 |
| §5 verification matrix (12 flags × ~30 s each, parallelised) | 10 min | 0:25 |
| §8 agent-chain pickup validation | 5 min | 0:30 |
| **T1 done** | | **0:30** |
| Slack-time for SSA conflict / unexpected breakage | 30 min | 1:00 |
| Slack-time for T2 fallback investigation if T1 fails | 30 min | 1:30 |
| **HARD STOP — revert to `demo-stable-2026-05-15`** | | **1:30** |

---

## 11. Go / no-go gates

After each gate, **stop and decide**: continue or revert.

| # | Gate | Pass criterion | If fail |
|---|---|---|---|
| G1 | `helm upgrade` exit code | `exit 0` and no SSA conflicts in output | §6.1 cheap rollback; investigate why the chart can't apply |
| G2 | Prometheus loads the rule | `/api/v1/rules` lists `ScenarioFlagActive` with `health=ok` | §6.1; check YAML indentation / quoting |
| G3 | Rule fires on inject | `state=firing` within 90 s of POST /api/scenarios/{id}/inject | Widen rate window; re-test once |
| G4 | Alert appears in dashboard's Alert Stream | row visible in `/alerts` page with the right summary | acceptable risk — log to fix post-demo, demo via Plan A |
| G5 | Triage button on the alert fires the agent chain | new verdict appears in AI Reasoning page within 30 s | log as known issue; demo via Plan A |

---

## 12. After the demo (post-Plan-B follow-ups regardless of outcome)

Whichever path tomorrow goes, these are the persistent post-demo work items this investigation surfaced:

1. **File a real bug in the OTel demo's payment / product-catalog / cart services**: spans aren't marked `STATUS_CODE_ERROR` when the failure flag causes the service to error. Reference span: `oteldemo.PaymentService/Charge` under `paymentFailure=100%`.
2. **Audit every other `*ErrorRateHigh` rule** in `values.yaml` to confirm they're documented as "may not fire on the current OTel demo build" or upgrade them to use `feature_flag_flagd_impression_total` as a secondary signal.
3. **Add a smoke test** in `tests/` that asserts the demo cluster produces at least one `STATUS_CODE_ERROR` series for each service named in the alert rules — so we catch this kind of regression at PR time, not at demo time.
4. **Decide** whether to keep the `ScenarioFlagActive` rule long-term or remove it once the per-service rules genuinely fire. It's a useful safety net even when the others work (catches "left a chaos test running").

---

*End of original Plan B doc. Continue reading §13 below for the revision based on tonight's deeper probes.*

---

## 13. Probe results (2026-05-15 ~04:35 UTC) — T1 recommendation revised

After this doc was first drafted, I ran a deeper round of probes against the live cluster. They contradict §2.T1's "HIGH confidence" claim for the slow-evaluating flags. The plan needs revising.

### 13.1 The live test that changed my mind

Procedure:

1. Clean baseline via `.\reset.ps1`. Confirmed `paymentFailure: off, rate=0/s`.
2. `POST /api/scenarios/payment_failure/inject` — confirmed `injected ok`.
3. Wait 45 seconds (well beyond Prom's 15s scrape interval).
4. Query `sum by (feature_flag_result_variant) (rate(feature_flag_flagd_impression_total{feature_flag_key="paymentFailure"}[2m]))`.

**Result:** `variant=off, rate=0/s`. **The active variant never appeared.** Even the cumulative counter for `paymentFailure` is at **value=1** — meaning the payment service has evaluated this flag exactly **once** since pod startup. By contrast:

| Flag | Cumulative impressions since startup | Per-second baseline |
|---|---|---|
| `cartFailure` | 75 | 0.13/s |
| `emailMemoryLeak` | 75 | 0.13/s |
| `failedReadinessProbe` | 52 | 0.03/s |
| `paymentFailure` | **1** | **0/s** |
| `paymentUnreachable` | **1** | 0/s |
| `kafkaQueueProblems` | **1** | 0/s |

**Conclusion:** the payment service (and likely kafka, paymentUnreachable) caches the flag value via the OpenFeature SDK and only re-evaluates on cold startup or on a specific trigger. **T1 will not fire `ScenarioFlagActive` for paymentFailure-class flags within the 60–90 s acceptance window.**

### 13.2 What this means for the plan

The §2 hypothesis ladder still holds, but the recommended order changes:

| Tier | Original ranking | Revised ranking | Why |
|---|---|---|---|
| **T1** (impression-rate rule) | RECOMMENDED, HIGH confidence | **Complementary, MIXED confidence** | Fires for cart/email/ad/failed-readiness (high eval rate). Does NOT fire for payment/kafka/paymentUnreachable. |
| **T2** (latency histograms) | FALLBACK, MEDIUM | Unchanged | Covers latency-affecting flags only. |
| **T3** (synthetic gauge from UI server) | LAST RESORT, narrative-impure | **PROMOTED to RECOMMENDED for universal coverage** | Only path that fires reliably for ALL flags within seconds. ~30 min of work vs T1's 30 min, but covers 100% of scenarios. |

### 13.3 Revised primary path

**Ship T3 first.** It's the only path that fires for every flag the dashboard offers. Then optionally add T1 for fast-eval flags as a bonus real-data signal that runs alongside T3.

**T3 implementation outline** (replaces §4 for the primary path):

1. **Add a `/metrics` endpoint to [demo/ui/server.py](../demo/ui/server.py)**. Body sketch:

   ```python
   from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
   from fastapi import Response

   _SCENARIO_ACTIVE = Gauge(
       "aiops_scenario_active",
       "1 when the scenario flag is in a non-off variant per the UI's view of flagd.",
       ["scenario_id", "flag", "service"],
   )

   def _refresh_scenario_gauge() -> None:
       # Reuse the same in-process logic /api/scenarios uses.
       # For each scenario in the manifest, set the gauge to 1 if its
       # current_variant != "off", else 0. Keep all label tuples present
       # so the gauge correctly drops to 0 on reset (no stale label).
       ...

   @app.get("/metrics")
   def metrics() -> Response:
       _refresh_scenario_gauge()
       return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
   ```

2. **Confirm `prometheus_client` is available.** Quick check:

   ```powershell
   uv run python -c "import prometheus_client; print(prometheus_client.__version__)"
   ```

   If `ModuleNotFoundError`: `uv add prometheus-client` (adds ~50 lines to `pyproject.toml`/`uv.lock`).

3. **Make Prometheus scrape the UI server.** Two paths:
   - **3a (preferred):** add to `demo/otel-demo/values.yaml`:

     ```yaml
     prometheus:
       extraScrapeConfigs: |
         - job_name: aiops-ui
           scrape_interval: 15s
           static_configs:
             - targets: ['host.docker.internal:8765']
     ```

   - **3b (fallback if `host.docker.internal` doesn't resolve from Rancher Desktop's k3s VM):** run the UI server as a single-replica Deployment inside the cluster. Bigger change — defer to T1-only if 3a fails.

4. **Add the alert rule** (in the same rules list as the existing ones):

   ```yaml
   - alert: ScenarioActive
     expr: aiops_scenario_active > 0
     for: 15s
     labels:
       severity: high
       alert_type: scenario_active
       service: "{{ $labels.service }}"
     annotations:
       summary: "Scenario {{ $labels.scenario_id }} active on {{ $labels.service }}"
       description: "flag={{ $labels.flag }} is in a non-off variant"
       flag_hint: "reset via .\\reset.ps1 or dashboard 'Reset all'"
   ```

5. **`helm upgrade --reuse-values=false`** (same command as §4.2).

6. **Verification:** `curl http://localhost:8765/metrics | Select-String aiops_scenario_active` → confirm gauge format. Then §4.4 steps 2–5 unchanged.

### 13.4 Revised time budget

| Block | Time |
|---|---|
| Add `/metrics` endpoint + `_refresh_scenario_gauge` to server.py | 20 min |
| Verify `prometheus_client` is in deps; `uv add` if not | 5 min |
| Edit `values.yaml` (scrape config + ScenarioActive rule) | 10 min |
| `helm upgrade --reuse-values=false` | 5 min |
| Verification (§4.4 adapted) | 10 min |
| `host.docker.internal` resolution test (probe from inside a test pod) | 5 min |
| Slack for SSA conflict / scrape-config issues | 30 min |
| **HARD STOP and revert** | **at 90 min** |

### 13.5 Pre-mitigation for the §8 unknown (agent-chain pickup)

§8 worried that the agent chain might not consume the new alert because of label-shape assumptions. With T3, the alert carries `service` as a label (derived from the scenario manifest's `service` field). This satisfies the most likely assumption (`assigned_service = labels.service`). If the WebSocket forwarding still drops it, we ship the rule and accept the agent-chain pickup as a known follow-up — the Failure Injection panel → Alert Stream → Triage button flow still works because that's all client-side state.

### 13.6 What to keep from the original §1–§12

Unchanged:

- §1 diagnosis of why STATUS_CODE_ERROR-based rules don't fire — still valid.
- §5 verification matrix — still the right list, just routed through T3 first.
- §6 rollback procedure — unchanged.
- §7 risk register — augment with "T3-specific: `host.docker.internal` may not resolve from k3s VM" (mitigation: §13.3 step 3b fallback or revert to T1-only).
- §8 agent-chain pickup unknown — pre-mitigated in §13.5.
- §9 out of scope — unchanged.
- §11 go/no-go gates — adapt G1–G5 for T3:
  - G1: `curl /metrics` returns 200 with `aiops_scenario_active` gauge present.
  - G2: Prometheus scrape target `aiops-ui` shows `health=up` at `/api/v1/targets`.
  - G3: Rule fires within 30 s of inject (faster than T1 — gauge updates synchronously).
  - G4, G5: unchanged.
- §12 post-demo follow-ups — unchanged.

---

*End of revised plan. Recommendation now: T3 first (primary), T1 second (bonus signal for fast-eval flags), T2 only if both fail. Ready to execute on your go.*
