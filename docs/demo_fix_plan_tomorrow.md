# Demo Fix Plan — POC tomorrow (2026-05-15)

**Scope:** make the four agents that exist in this repo (Alert Triage, Incident Classifier, Auto-Ticketing, Notification Router) demoable end-to-end on this Windows + Rancher Desktop laptop tomorrow. Nothing more.

**Out of scope tonight:** building RCA / Remediation / Log Correlation, deploying Loki/Tempo, fixing CI, refactoring seams, rotating PDI password, finishing Incident Classifier v1. Anything not on the list below — leave it.

**Inputs:**

- [docs/demo_readiness_audit.md](demo_readiness_audit.md) — v1, read-only first pass.
- [docs/demo_readiness_audit_v2.md](demo_readiness_audit_v2.md) — v2, with `start.ps1` live + harness run. **This plan acts on v2's P0 list only.**

---

## 0. One decision needed before anything else

The single thing blocking a clean demo is that `alert_triage` evals come back 5/8 against the live ServiceNow PDI because the PDI's CMDB doesn't have `ad` → `Ads Team` or `accounting` → `Finance Systems` mappings. Three forks:

| Option | What you do | Demo audience sees | Effort tonight |
|---|---|---|---|
| **A. Mock everything** | `AIOPS_USE_MOCK_ITSM=true` in [.env](.env). | Triage + ticketing all in-process; no live ServiceNow record. Notification Router writes to `demo/chatops_outbox.json`. | **2 minutes.** |
| **B. Seed live PDI CMDB** | Log into `dev195902.service-now.com`, add CIs for `ad` (assigned to `Ads Team`, support_group_email `oncall@ads.example.com`) and `accounting` (assigned to `Finance Systems`). Keep `AIOPS_USE_MOCK_ITSM=false`. | Real ServiceNow ticket appears in the PDI mid-demo. Strongest "real integration" story. | **~30 min** of ServiceNow clicking. |
| **C. Hybrid: cherry-pick scenarios** | Keep live PDI; **only demo scenarios whose CMDB hits already work** (`payment_cpu_spike`, `checkout_latency_p95_high`, `severity_hint_critical_direct`, `severity_hint_p2_high`, `cmdb_miss_unknown_service`). Skip the three that fail. | Real ServiceNow ticket for payment/checkout cases. Don't run the `ad`/`accounting` cases live. | **5 min** of script editing. |

**Recommendation: Option C** for tomorrow. Keeps the "real PDI" story your branch name promises, costs almost no time, fails closed. Move to B post-demo when you have a calm afternoon.

If you pick A or B, follow the per-option steps in §3. Everything else in this plan is option-agnostic.

---

## 1. The demo narrative (agreed, then we work backwards)

Walk the audience through one Reactive flow, *using only what exists*:

1. **Inject a failure** — `uv run python -m demo.failure_injection.inject slow-product-catalog`. Show the dashboard reacting.
2. **Alert Triage** — `uv run python -m agents.alert_triage --fixture <case>`. Show severity, assigned team, runbook URL.
3. **Incident Classifier** — **DO NOT RUN LIVE.** Either skip with one sentence ("v0 stub — next sprint lands the classifier model"), or pre-bake a screenshot of the contract from [agents/incident_classifier/README.md](../agents/incident_classifier/README.md). On a projector, never call `--fixture`.
4. **Auto-Ticketing** — `uv run python -c "from agents.auto_ticketing.agent import run; print(run(<verdict>))"`. Switch to ServiceNow PDI tab; show the freshly-created ticket. (Option A: mock ticket only; Option B/C: real ticket.)
5. **Notification Router** — `uv run python -m agents.notification_router --fixture <verdict>`. Open `demo/chatops_outbox.json` and show the routed message. One sentence: "in production, the same adapter targets Slack/Teams."
6. **Clear the failure** — `uv run python -m demo.failure_injection.inject --clear`. Show the dashboard recovering.

**Narrative rules:**

- Do not say the words "RCA", "Remediation", or "Log Correlation" unless someone asks. They don't exist in this repo. If asked: "on the roadmap, after the Reactive backbone stabilises — Phase 2 in the solution design."
- Do not say "Loki" or "Tempo". The cluster has Prom + Jaeger only. Metrics and traces are the story.
- Say "Personal Developer Instance" when you say ServiceNow. Don't oversell.

---

## 2. Tonight — the actual fix list

Run these in order. Each row has its verification step. Stop the first time something fails; do not move on.

### 2.1 Pre-flight (5 min)

| # | Step | Command | Pass = |
|---|---|---|---|
| 2.1.1 | Working tree clean | `git status` | only the audit files + `.tmp_eval.txt` should be new/dirty |
| 2.1.2 | On the right branch | `git rev-parse --abbrev-ref HEAD` | `feat/demo-readiness-cmdb-and-llm-health` |
| 2.1.3 | Cluster reachable | `kubectl get nodes` | node `Ready` |
| 2.1.4 | OTel demo healthy | `kubectl get pods -n otel-demo \| grep -v Running` | no extra rows (only the header) |

### 2.2 Tear down, then bring up cleanly (5 min)

Both `start.ps1` runs and any partial state from earlier should be flushed before tonight's rehearsal.

| # | Step | Command | Pass = |
|---|---|---|---|
| 2.2.1 | Stop existing PFs (in *each* PowerShell window that had `start.ps1`) | `.\stop.ps1` | `Get-Job -Name 'pf-*'` empty in that window |
| 2.2.2 | Clear any leftover flag injection | `uv run python -m demo.failure_injection.inject --clear` | exit 0, no error |
| 2.2.3 | Confirm flagd is back to defaults | `kubectl -n otel-demo get configmap flagd-config -o jsonpath='{.data.demo\.flagd\.json}' \| Select-String 'productCatalogFailure' -Context 0,3` | `defaultVariant: off` for `productCatalogFailure` and `kafkaQueueProblems` |

### 2.3 Apply the chosen option from §0 (2–30 min)

**If Option A:**

| # | Step | Pass = |
|---|---|---|
| 2.3.A.1 | Edit [.env](.env) line 50: `AIOPS_USE_MOCK_ITSM=true` | grep confirms |
| 2.3.A.2 | (Re)start the UI window: `.\start.ps1` | UI on `http://localhost:8765` returns 200 |
| 2.3.A.3 | Eval pass rate ≥ 0.85 | `uv run python -m evals.harness --agent alert_triage \| Select-String 'overall_pass_rate'` shows ≥ 0.85 |

**If Option B:**

| # | Step | Pass = |
|---|---|---|
| 2.3.B.1 | Log into `https://dev195902.service-now.com` with the admin creds from [.env:46-47](.env). | dashboard loads |
| 2.3.B.2 | Create CMDB CI `ad` (class: Application; support group: `Ads Team`; support_group_email: `oncall@ads.example.com`). | record visible in `cmdb_ci_appl.list` |
| 2.3.B.3 | Create CMDB CI `accounting` (class: Application; support group: `Finance Systems`). | record visible |
| 2.3.B.4 | Eval pass rate ≥ 0.85 | `uv run python -m evals.harness --agent alert_triage` shows ≥ 0.85 |

**If Option C:**

| # | Step | Pass = |
|---|---|---|
| 2.3.C.1 | In your rehearsal script (§4), only invoke the 5 known-passing fixture ids: `payment_cpu_spike`, `checkout_latency_p95_high`, `severity_hint_critical_direct`, `cmdb_miss_unknown_service`, `severity_hint_p2_high`. | script saved |
| 2.3.C.2 | Hide / don't run the 3 known-failing fixtures during the demo: `ad_low_traffic_early_warning`, `accounting_memory_high`, `sev_4_below_threshold_boundary`. | none of these in script |
| 2.3.C.3 | Sanity: run just the demo subset and confirm all pass | manual loop over the 5 ids, all `passed: true` |

### 2.4 Rehearse end-to-end (15 min)

Walk through §1 steps 1–6, top to bottom, **once**, in the actual PowerShell windows you'll use tomorrow. Stopwatch it. Target ≤ 10 minutes; if it runs longer, something needs to be pre-baked.

**Pass criteria:**

- All commands run without a stack trace.
- ServiceNow PDI tab shows a new ticket (Options B/C only).
- `demo/chatops_outbox.json` has a new entry from Notification Router.
- `--clear` brings the dashboard back to baseline within 60 s.

---

## 3. Morning-of pre-flight (10 min, before audience joins)

Run in this order. Don't skip — Rancher Desktop sometimes loses the bridge overnight on Windows.

```powershell
# Window A (long-lived — DO NOT CLOSE)
cd C:\Projects\AIops
.\start.ps1
# Wait for "Up and running" — should take < 60 s
# Then in your browser confirm:
#   http://localhost:8765/dashboard/   (React)
#   http://localhost:8080/grafana/     (loads)
#   http://localhost:8080/jaeger/ui/   (loads)

# Window B (work window)
cd C:\Projects\AIops
uv run python -m demo.failure_injection.inject --list      # confirms 3 scenarios
uv run python -m demo.failure_injection.inject --clear     # belt-and-suspenders
uv run python -m evals.harness --agent alert_triage | Select-String 'overall_pass_rate'
# expect >= 0.85 (Option A/B) or pre-baked 1.0 over the 5-case subset (Option C)
```

**If any of these fails:** see §5.

---

## 4. Demo command crib sheet

Tape this on the second monitor.

```powershell
# 1) Inject the failure (audience sees dashboard go red within 30-60 s)
uv run python -m demo.failure_injection.inject slow-product-catalog

# 2) Show Alert Triage verdict
uv run python -m agents.alert_triage --fixture checkout_latency_p95_high

# 3) (Skip Incident Classifier live. Show screenshot of the contract instead.)

# 4) Auto-Ticketing -> ServiceNow PDI
$verdict = uv run python -m agents.alert_triage --fixture checkout_latency_p95_high --json
uv run python -c "import json, sys; from agents.auto_ticketing.agent import run; v=json.loads(sys.stdin.read()); print(run(v))" <<< $verdict
# Then switch to the PDI tab; the new INC record appears.

# 5) Notification Router
uv run python -m agents.notification_router --fixture <same verdict>
Get-Content demo\chatops_outbox.json -Tail 5

# 6) Clear (audience sees dashboard recover)
uv run python -m demo.failure_injection.inject --clear
```

**Caveat for Option A:** in step 4, the PDI tab will not show a new ticket — the mock adapter writes locally. Have a slide or pre-recorded screenshot ready instead, and label it as such.

---

## 5. Failure handling — if X breaks during the demo

| Symptom | Don't panic. Do this. |
|---|---|
| `start.ps1` errors with "Cannot reach the Kubernetes API" | Open Rancher Desktop, wait for tray icon `Kubernetes: running`, re-run `start.ps1`. |
| Port 8765 / 9090 / 16686 / 8080 doesn't open | The previous window was closed. Open a fresh window, `cd C:\Projects\AIops`, `.\stop.ps1`, `.\start.ps1`. |
| Alert Triage raises an LLM error | Switch to stub: `$env:AIOPS_LLM_PROVIDER='stub'`, re-run the same command. Stub gives canned answers — narrate it as "fallback mode". |
| ServiceNow PDI returns 401 / 403 | The admin password expired again (issue #43). Switch to Option A live: `$env:AIOPS_USE_MOCK_ITSM='true'` and re-run from step 4. Skip the ServiceNow tab. |
| Dashboard shows blank / 503 on `/dashboard/` | `cd demo\dashboard; npm install --silent; npm run build`. Then re-open `http://localhost:8765/dashboard/`. |
| flagd injection seems stuck | `uv run python -m demo.failure_injection.inject --clear`. Wait 30 s. If still stuck: `kubectl -n otel-demo rollout restart deployment/flagd` (this is the *one* allowed direct kubectl action — it doesn't patch chart-owned data). |
| Someone asks about RCA / Remediation / Log Correlation | "Phase 2 in the solution design — Reactive backbone first. RCA is the headline differentiator for the next milestone." Don't promise a date. |
| Incident Classifier comes up in Q&A | "v0 contract is locked; v1 model lands next sprint. The fixture interface is stable today." |

---

## 6. What stays broken on purpose (do not fix tonight)

Listed so nobody panics when they see them:

- `accounting` pod OOMKills every few hours (120 Mi limit). The pod recovers each time. Chart-owned value; fix via Helm values post-demo. Reference: v2 audit P2-1.
- `product-catalog` shows restart count 18. Historical, not active — pod has been stable since v1 audit. Won't move tomorrow unless a scenario stresses it.
- `paymentUnreachable: on` is the upstream chart's baked-in default. Background noise. Don't call attention to it.
- [infra/port-forward.ps1](infra/port-forward.ps1) has a kuberlr trap. **Don't use it tomorrow.** Use `.\start.ps1`. Period.
- Incident Classifier's `--fixture` raises `NotImplementedError`. **Don't invoke it live.**
- Loki/Tempo aren't deployed. Don't mention them.

---

## 7. Post-demo cleanup (5 min, after the audience leaves)

```powershell
.\stop.ps1                                         # tear down PFs + UI
uv run python -m demo.failure_injection.inject --clear   # idempotent reset
Remove-Item C:\Projects\AIops\.tmp_eval.txt -ErrorAction SilentlyContinue
```

Then write a one-paragraph post-mortem (what worked, what didn't, audience questions) and append it to the bottom of this file. That's how the v3 audit gets started.

---

## 8. Timing budget tonight

| Block | Time | Cumulative |
|---|---|---|
| §2.1 pre-flight | 5 min | 0:05 |
| §2.2 tear down + clean bring-up | 5 min | 0:10 |
| §2.3 (Option C) — script edit + 5-case sanity | 10 min | 0:20 |
| §2.4 rehearsal end-to-end | 15 min | 0:35 |
| Slack a teammate to dry-run audience Qs | 10 min | 0:45 |
| **Total** | **~45 min** | — |

If you pick Option B instead of C, add 30 min for ServiceNow CMDB seeding (§2.3.B), total ~75 min.

---

*Plan ends. This is an execution document, not analysis. If anything below the §2 line is "interesting" rather than "needed", cut it tomorrow morning.*
