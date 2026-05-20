# Demo Readiness Audit v2 — `start.ps1` live

**Audit host:** Windows 11, Rancher Desktop k3s (WSL2), node `gglw42nlpc2015`, 6d uptime.
**Auditor:** Claude (read-only second pass; harness was run, no files edited).
**Date:** 2026-05-14 (~25 min after [v1](docs/demo_readiness_audit.md)).
**Repo state:** branch `feat/demo-readiness-cmdb-and-llm-health` @ `2f065e9`, unchanged since v1.
**What changed:** the user opened a dedicated PowerShell window and ran `.\start.ps1` between v1 and v2.

---

## 1. Executive summary

**Can the full Reactive flow demo on this laptop today? Yes — with one real, code-level gap that v1 did not catch and three findings v1 got wrong.**

The single new real blocker: **`alert_triage` evals pass 5/8 = 62.5 % against live ServiceNow PDI** (below the CI gate of 0.85). The failure mode is the same on three cases — CMDB lookups for `ad` and `accounting` resolve to the generic `Platform On-Call` / `Software` teams instead of the seeded `Ads Team` / `Finance Systems`. The mock CMDB at [aiops/tools/itsm/_demo_cmdb.py](aiops/tools/itsm/_demo_cmdb.py) has the right mappings; the live PDI does not. Flip `AIOPS_USE_MOCK_ITSM=true` for the demo, or populate the PDI CMDB before the demo. This is the real "Auto-Ticketing / downstream agents are flaky on this laptop" effect.

Everything else v1 flagged either dissolved when `start.ps1` came up (P0-1) or turned out to be wrong (P0-2, parts of §2). The four agents that exist are seam-clean; smoke tests pass 12/12; both `aiops.tools.observability.prometheus.query()` and `aiops.tools.observability.jaeger.services()` work correctly against the live cluster — verified end-to-end through the seams, not just by HTTP probe.

---

## 2. Cluster snapshot (refreshed)

### 2.1 Port-forward table — all green

| Target | Listening | Verified via | Notes |
|---|---|---|---|
| 8080 frontend-proxy | ✅ `::1:8080` (pid 26576) | `curl http://localhost:8080/` → 200 (11447 b) | Grafana/Jaeger UI reachable through here. |
| 8765 demo UI (FastAPI) | ✅ `127.0.0.1:8765` (pid 22892, `uvicorn`) | `/api/health` → 200 (527 b) | React dashboard served at `/dashboard/`. |
| 9090 Prometheus | ✅ `::1:9090` (pid 24608) | `up` query returns 2 series | — |
| 16686 Jaeger | ✅ `::1:16686` (pid 23992) | seam returns 18 services | API path is `/jaeger/ui/api/*` (v2 base_path), already handled by the tool. |
| 3100 Loki / 3200 Tempo | ❌ not listening | — | Not deployed (unchanged from v1). |

`Get-Job -Name 'pf-*'` in this shell is empty — confirms `start.ps1` was run in a separate window (correct usage). 3 `kubectl port-forward` child processes are alive from PID 10380/21780/26060.

### 2.2 Node + pod state (unchanged since v1)

- Memory still 7258 Mi / 7850 Mi = **94 %** committed. No pressure events. No new OOMKills since v1 (`accounting` still at 17 — flat, not actively flapping).
- `product-catalog` restart count is **still 18** — i.e. the pod has been stable for the last ~30 min. **v1's "stuck slow-product-catalog injection" diagnosis was wrong** (see §3, fallacy F-2). flagd-config confirms `productCatalogFailure: off`.
- One new finding from inspecting flagd directly: **`paymentUnreachable: on`** is the default in the chart's flagd-config. That's upstream behaviour and is why `payment` and `frontend-proxy` continuously see retries in the load generator's traffic. Not a blocker — useful context.

### 2.3 Live flagd flag inventory (replaces v1 §3 "drift potential")

The actual flag list in `flagd-config` (15 flags):

| Flag | Default | Notes |
|---|---|---|
| `productCatalogFailure` | off | Wired in scenarios. |
| `kafkaQueueProblems` | off | Wired in scenarios. |
| `recommendationCacheFailure` | off | — |
| `adManualGc` / `adHighCpu` / `adFailure` | off | **v1 subagent said `adServiceFailure` — wrong; real name is `adFailure`.** |
| `cartFailure` | off | **v1 subagent said `cartServiceFailure` — wrong; real name is `cartFailure`.** |
| `paymentFailure` | off | — |
| `paymentUnreachable` | **on** | **New finding.** Default-on. |
| `loadGeneratorFloodHomepage` | off | — |
| `imageSlowLoad` | off | — |
| `emailMemoryLeak` | off | — |
| `failedReadinessProbe` | off | **New finding.** Not in any v1 list. |
| `llmInaccurateResponse` | off | **New finding.** Useful for LLM-quality scenarios. |
| `llmRateLimitError` | off | **New finding.** Useful for LLM-quality scenarios. |

---

## 3. Fallacy diff — what v1 got wrong

| # | v1 claim | Reality on this re-pass | Why v1 was wrong |
|---|---|---|---|
| **F-1** | **P0-1: "agents can't reach Prom/Jaeger/frontend-proxy because no PFs are listening"** | **Confirmed cause, dissolved by remediation.** All four ports now listening; both seams verified end-to-end via `aiops.tools.observability.*`. | Not a fallacy — it was the right call. Listed here for completeness. |
| **F-2** | **P0-2: "`product-catalog` has 18 restarts → stuck `slow-product-catalog` injection; run `inject.py --clear`."** | **Wrong.** `flagd-config.flags.productCatalogFailure.defaultVariant == "off"`. Restart count is unchanged from v1 (18 → 18) — pod is stable, just has a high lifetime count from the cluster bounce 94m before v1. | v1 inferred from a co-occurrence (low memory limit + crash exit code) without inspecting flagd state. Confirmation bias. |
| **F-3** | **v1 cluster table called out "Jaeger" reachable on `:16686`** but the **HTTP probe in v2 §1 initially returned 404 on `/api/services`**. v1 implicitly assumed `:16686` would serve the v1 Jaeger API path. | **Misleading-but-tolerable.** The OTel demo ships Jaeger 2.x with `base_path: /jaeger/ui`, so the real endpoint is `:16686/jaeger/ui/api/services`. [aiops/tools/observability/jaeger.py:13](aiops/tools/observability/jaeger.py#L13) already handles this via `AIOPS_JAEGER_API_PREFIX` defaulting to `/jaeger/ui` — verified the seam returns 18 services. | I almost wrote a v2 P0 "Jaeger seam misconfigured" before testing the actual seam. The code is already correct; the bare-port probe was the misleading signal. |
| **F-4** | **v1 §3 cross-agent table cited the OTel-demo flag list as `paymentFailure, cartServiceFailure, recommendationCacheFailure, adServiceFailure, adManualGc, …`** | **Two names are wrong.** Real names are `cartFailure` (no "Service") and `adFailure` (no "Service"). v1 also missed `paymentUnreachable` (on by default), `failedReadinessProbe`, `llmInaccurateResponse`, `llmRateLimitError`. | The v1 sub-agent inferred flag names from Prometheus alert rule files and the upstream OTel-demo README, neither of which is authoritative for the chart version we're running. Should have read `kubectl get configmap flagd-config -o jsonpath='{.data}'`. |
| **F-5** | **v1 implied `incident_classifier` is a NotImplementedError stub** | **Confirmed** in [README.md:7](agents/incident_classifier/README.md#L7) and [__main__.py:8](agents/incident_classifier/__main__.py#L8). Not a fallacy — listed for completeness. | — |
| **F-6** | **v1 said no Slack/Teams webhook env vars are referenced anywhere** | **Confirmed.** Notification Router uses the jsonfile chatops adapter. | Not a fallacy. |
| **F-7** | **v1 said `log_correlation/`, `rca/`, `remediation/` don't exist on any branch** | **Confirmed.** `git log --all --name-only` returns no rows. | Not a fallacy — and remains the single most important strategic finding. |

**Net:** of v1's four P0s, **only P0-1 was real**. **P0-2 was a wrong diagnosis. P0-3 and P0-4 stand** (missing agents in narrative; Incident Classifier stub). One additional fallacy (F-4) was confined to a sub-agent's flag list and didn't make it into a fix line, but it would have if I'd recommended a "scenario drift cleanup" fix.

---

## 4. New findings v1 missed (because v1 was read-only)

### 4.1 P0 — `alert_triage` eval pass rate is 62.5 % against live PDI

**Symptom.** `uv run python -m evals.harness --agent alert_triage` returns `"overall_pass_rate": 0.625` (5/8). CI gate in CLAUDE.md is `--min-pass-rate 0.85`. CI will fail; demo audience will see "3 out of 8 cases failed" if anyone scrolls the harness output.

**Failing cases & what specifically breaks (from `.tmp_eval.txt`):**

| Case | Failing check(s) | Actual | Expected |
|---|---|---|---|
| `ad_low_traffic_early_warning` | `assigned_team`, `assigned_engineer_contains` | `Platform On-Call` / `oncall@platform-on-call.example.com` | `Ads Team` / contains `oncall@ads` |
| `accounting_memory_high` | `assigned_team` | `Software` | `Finance Systems` |
| `sev_4_below_threshold_boundary` | `assigned_team`, `assigned_engineer_contains` | `Platform On-Call` / `oncall@platform-on-call.example.com` | `Ads Team` / contains `oncall@ads` |

**Root cause.** All three failures are the CMDB lookup falling through to the generic fallback team. With [.env:50](.env) `AIOPS_USE_MOCK_ITSM=false`, [aiops/tools/itsm](aiops/tools/itsm) routes `itsm.cmdb.lookup` to the live ServiceNow PDI (`dev195902.service-now.com`). The PDI's CMDB does not have entries for `ad` or `accounting` mapped to `Ads Team` / `Finance Systems`. The mock CMDB at [aiops/tools/itsm/_demo_cmdb.py](aiops/tools/itsm/_demo_cmdb.py) does — that's what the golden set was written against.

**Two viable fixes (pick one — both are one-liners):**

1. **For the demo: switch to mock CMDB.** Edit [.env:50](.env): `AIOPS_USE_MOCK_ITSM=true`. Re-run `.\start.ps1` (or restart the uvicorn job alone) so the agent re-reads. Verification: `uv run python -m evals.harness --agent alert_triage` → `overall_pass_rate >= 0.85`.
2. **For correctness: populate the PDI CMDB.** Add CMDB CIs in the dev PDI for `ad` → `Ads Team` / `oncall@ads.example.com` and `accounting` → `Finance Systems`. Slower; survives across machines.

If the goal of the `feat/demo-readiness-cmdb-and-llm-health` branch is "real PDI end-to-end", option 2 is on-brand; option 1 is honest demo expedience.

### 4.2 P1 — Eval harness output isn't captured by `start.ps1`

When I tail-captured the harness output, line-50 of the JSON tail was the only data preserved — easy to draw wrong conclusions from. Not a code bug, just a sharp edge: when triaging eval results, write to file first (`> eval.json`) then grep, don't tail-pipe. Worth a sentence in CLAUDE.md "Common commands" alongside the existing harness invocation.

### 4.3 P2 — `paymentUnreachable=on` is the cluster's baseline

The chart ships with `paymentUnreachable: on` by default. Every demo runs against an already-degraded payment path. Not a blocker — but worth surfacing in the demo narrative ("the cluster has a known fault baked in; here we *inject* a new one") so the audience doesn't think the agent is hallucinating payment errors.

---

## 5. Per-agent matrix (refreshed — verdicts upgrade now PFs are up)

| Agent | v1 verdict | v2 verdict | Delta |
|---|---|---|---|
| **alert_triage** | "RUNS (degraded without PFs)" | **RUNS but FAILS EVAL** (5/8 cases pass) | Was masked in v1; live run exposes CMDB gap. |
| **incident_classifier** | "STUB BY DESIGN" | unchanged | — |
| **auto_ticketing** | "RUNS against live PDI" | unchanged — would still need live `verdict` to drive end-to-end | — |
| **notification_router** | "RUNS (no live Slack)" | unchanged | — |

Seam compliance, truth-file coverage, golden-set inventory: all unchanged. Smoke tests pass 12/12.

---

## 6. Prioritised fix list (v2)

### P0 — must be green before demo

| # | Action | Verification |
|---|---|---|
| **P0-1** *(v2 new)* | For the demo, set `AIOPS_USE_MOCK_ITSM=true` in [.env:50](.env) and restart the UI job. If the branch's purpose is "real PDI", instead pre-seed `ad` / `accounting` CIs in the PDI CMDB. | `uv run python -m evals.harness --agent alert_triage` → `overall_pass_rate >= 0.85`. |
| **P0-2** *(carryover from v1, unchanged)* | Reconcile the missing-agents premise: either pull the un-pushed branch the teammate is using, or cut the demo narrative back to the 4 agents that exist. | Updated demo script does not mention RCA / Remediation / Log Correlation. |
| **P0-3** *(carryover from v1, unchanged)* | Decide what to do about Incident Classifier `--fixture` raising `NotImplementedError`: skip it in the live demo, pre-record its output, or finish v1. | Demo rehearsal completes without a stack trace. |
| ~~v1 P0-1 (port-forwards)~~ | **Resolved.** `start.ps1` is now up. | — |
| ~~v1 P0-2 (stuck flagd injection)~~ | **Withdrawn.** `productCatalogFailure: off`; the 18 restarts were historical. | — |

### P1 — correctness / will bite during demo if you're unlucky

| # | Action | Verification |
|---|---|---|
| **P1-1** *(carryover from v1)* | Fix the kuberlr-wrapper trap in [infra/port-forward.ps1:29-32](infra/port-forward.ps1#L29-L32) by passing the standalone kubectl directory into the `Start-Job` ArgumentList (mirror [start.ps1:75-81](start.ps1#L75-L81)). | `Get-Job -Name 'pf-prometheus' \| Receive-Job -Keep` shows "Forwarding from …", not a SHA-mismatch error. |
| **P1-2** *(carryover from v1)* | Add `AIOPS_PROMETHEUS_URL` / `AIOPS_JAEGER_URL` overrides to `.env.example` so future port changes don't silently break agents. | `uv run python -c "from aiops.tools.observability.jaeger import _URL, _API_PREFIX; print(_URL, _API_PREFIX)"`. |
| **P1-3** *(carryover from v1)* | Seed `agents/incident_classifier/evals/golden.json` from the three truth files before promoting RA-002 to v1. | `uv run python -m evals.harness --agent incident_classifier` reports >0 cases. |
| **P1-4** *(v2 new)* | Even if you pick mock-CMDB for the demo, fix the **eval gold set** to match what live PDI actually returns, or document that `alert_triage` evals require `AIOPS_USE_MOCK_ITSM=true`. Otherwise the next dev hits this. | A clean-machine run reproduces 8/8 without manual env tinkering. |
| **P1-5** *(v2 new)* | Update the v1 audit's reference to OTel-demo flag names: `cartFailure` (not `cartServiceFailure`), `adFailure` (not `adServiceFailure`). Note `paymentUnreachable: on` as the cluster baseline. | grep of the audit + any scenario files matches the real configmap names. |

### P2 — tech debt

| # | Action |
|---|---|
| **P2-1** *(carryover)* | Bump `accounting` Helm memory limit (chart values) — currently 120 Mi, OOMKilled periodically. Never `kubectl patch`. |
| **P2-2** *(carryover)* | Reconcile CLAUDE.md "Reference POC stack" claim that Loki + Tempo are present. They're not deployed. Either install them or correct the doc. |
| **P2-3** *(v2 new)* | When demo work depends on flagd flag names, prefer `kubectl get configmap flagd-config -o jsonpath='{.data}'` over README-derived assumptions. The chart's actual flag set drifts from upstream READMEs. |
| **P2-4** *(carryover)* | Consider deprecating [infra/port-forward.ps1](infra/port-forward.ps1) once P1-1 lands; `start.ps1` is the one path that's known-good on Windows. |
| **P2-5** *(carryover)* | Rotate ServiceNow PDI admin password after the demo (per [.env:42-47](.env) self-note about issue #43). |
| **P2-6** *(v2 new)* | Document the eval-output capture trick (write to file, then grep) in CLAUDE.md "Common commands" — saves the next person from drawing conclusions from a 50-line tail. |

---

## 7. Memory & skill updates — revised proposal

### Updates to **add** vs v1's proposal

| File | Reason |
|---|---|
| **`project_alert_triage_live_pdi_cmdb_gap.md`** *(new — type=project)* | Document that on this branch (`feat/demo-readiness-cmdb-and-llm-health`), live ServiceNow PDI lacks CMDB CIs for `ad` / `accounting`; mock CMDB has them; eval pass rate flips from 5/8 → 8/8 based on `AIOPS_USE_MOCK_ITSM`. Decay-stamp 2026-05-14. |
| **`reference_otel_demo_flagd_flags.md`** *(new — type=reference)* | List the 15 real flag names from the running `flagd-config`, including `paymentUnreachable: on` baseline, `cartFailure` (not `cartServiceFailure`), `adFailure` (not `adServiceFailure`), and the four LLM-quality flags. Source: `kubectl get configmap flagd-config -o jsonpath='{.data}'` on chart `opentelemetry-demo-0.40.8`. |
| **`feedback_diagnose_via_seam_not_curl.md`** *(new — type=feedback)* | Rule: when checking whether `aiops/tools/observability/*` works, call the seam function directly (`uv run python -c "from … import …; print(…)"`), don't `curl` the bare endpoint and infer. **Why:** v1→v2 caught a near-fallacy where Jaeger's `/jaeger/ui/` base_path made the bare port look broken; the seam handles it correctly. |
| **`feedback_check_flagd_directly.md`** *(new — type=feedback)* | Rule: for any claim about OTel-demo flags, read the live `flagd-config` configmap, not the upstream README. **Why:** v1 sub-agent named two flags wrong because it inferred from docs. |

### Updates to **revise** vs v1's proposal

| File | Reason |
|---|---|
| ~~`feedback_port_forwards_dedicated_window.md`~~ | Keep — confirmed real and reusable. |
| ~~`project_agents_actually_in_repo.md`~~ | Keep — F-7 still stands. |
| ~~`project_incident_classifier_v0_stub.md`~~ | Keep. |
| ~~`reference_cluster_stack_reality.md`~~ | Keep, but **fold in** the flagd-flag list from above. |
| `project_environment_constraints.md` (existing) | Add gotcha: "**v1↔v2 audit confirmed**: bare-port HTTP probes to Jaeger 16686 return 404; the OTel demo's Jaeger 2.x serves at `/jaeger/ui/api/*`. The seam handles this — don't add an env override unless you've actually called the seam first." |

### Built-in skills

- **`fewer-permission-prompts`** — still recommended. v2 added several read-only `kubectl get configmap`, `helm list`, `uv run python -m evals.harness` invocations that would benefit from an allowlist.
- **`update-config`** — still no setting.json hook needed; the fix is in `.env`, not in Claude config.

---

## 8. Honest self-assessment

v1 was a read-only sweep. That kept the blast radius small but it also meant v1 could only flag the *visible* state of the host — no ports listening, low memory headroom, `accounting` OOMKilled, `product-catalog` with a high restart count. v1 could not see the **live integration gap** with the PDI because nothing was actually run against the PDI. v2 ran 8 evals end-to-end and found a concrete 3-case failure that v1 had no way to see. The lesson for future "read-only first" audits: do a *one-shot live probe of the agent seam* even in the read-only pass — `uv run python -m evals.harness --agent <one>` is reversible, no state changes, and costs ~10 s of LLM calls. It would have caught P0-1 of v2 in v1.

---

*End of v2. No files modified. `.tmp_eval.txt` is the only artifact written outside `docs/` — safe to delete.*
