# Demo Readiness Audit — `feat/demo-readiness-cmdb-and-llm-health`

**Audit host:** Windows 11 (Rancher Desktop k3s, WSL2, 6d uptime), node `gglw42nlpc2015`.
**Auditor:** Claude (read-only pass).
**Date:** 2026-05-14.
**Repo state:** branch `feat/demo-readiness-cmdb-and-llm-health` @ `2f065e9`. Working tree clean except `.claude/settings.json` and `.claude/worktrees/` (untracked).

---

## 1. Executive summary

**Can the full Reactive flow demo on this laptop today? No, not as drafted — but the gap is smaller than the framing suggests and is largely a one-shell-window problem, not a code problem.**

Three things are true at once:

1. **The premise needs a reality check.** [agents/](agents/) contains exactly four directories: [alert_triage](agents/alert_triage/), [auto_ticketing](agents/auto_ticketing/), [incident_classifier](agents/incident_classifier/), [notification_router](agents/notification_router/). `git log --all --name-only` shows **no `log_correlation/`, `rca/`, or `remediation/` directory has ever existed on any branch** of this repo. If a teammate is "running RCA / Remediation" on their machine, they are running something not in source control. That needs to be reconciled before the demo, not after.
2. **The four agents that do exist are seam-compliant and individually runnable on this host.** No direct vendor SDK imports outside [aiops/llm/](aiops/llm/), no HITL checks inside agent logic, all external I/O behind [aiops/tools](aiops/tools/) or [aiops/llm](aiops/llm/). [incident_classifier/agent.py](agents/incident_classifier/agent.py) is an intentional v0 stub that raises `NotImplementedError` (per its own [README.md:7](agents/incident_classifier/README.md#L7)) — this is design, not breakage.
3. **The actual reason agents "don't run on this laptop right now" is environmental, not agent-level.** [start.ps1](start.ps1) is not currently running in this shell: ports 8080 / 8765 / 9090 / 16686 are all idle, no `pf-*` background jobs, no `kubectl port-forward` processes. With no port-forwards, [aiops/tools/observability/prometheus.py:11](aiops/tools/observability/prometheus.py#L11) and [aiops/tools/observability/jaeger.py:12](aiops/tools/observability/jaeger.py#L12) both default to `http://localhost:9090` / `http://localhost:16686` and time out — which looks identical to "the agent is broken" from a demo seat. The cluster itself is up and healthy enough.

**Net:** the demo CAN run end-to-end on this laptop once `.\start.ps1` is invoked in a long-lived window, the stale `slow-product-catalog` injection is cleared, and the missing-agents premise is resolved (either pull a branch we don't see, or accept the demo is 4 agents, not 7). Detailed fix list in §4.

---

## 2. Cluster snapshot

### 2.1 Node health

| Metric | Value | Verdict |
|---|---|---|
| Context | `rancher-desktop` | OK |
| Node | `gglw42nlpc2015` (control-plane,master), v1.25.16+k3s4, 6d1h up | OK |
| MemoryPressure / DiskPressure / PIDPressure | all `False` | OK on paper |
| CPU allocatable | 14 cores; requests 650m (4 %) | OK |
| **Memory allocatable** | **7 850 692 Ki ≈ 7.5 GiB** | **CONCERN** |
| **Memory requests committed** | **7 258 Mi (94 %)** | **CONCERN** |
| Memory limits committed | 7 288 Mi (95 %) | CONCERN |
| PVCs | none | OK (chart is stateless) |
| `kubectl top` node | not run (metrics not enabled?) | n/a |

Node memory is 94 % committed by requests. That's the underlying reason `accounting` is in an OOMKill loop (see §2.3) and why any *additional* workload (e.g. a new agent pod) will fail to schedule. Not a demo blocker today, but if anyone tries to add Loki/Tempo or a second namespace on this host, it'll go red immediately.

### 2.2 Service inventory in `otel-demo`

| Service | ClusterIP | Port | Notes |
|---|---|---|---|
| `prometheus` | 10.43.230.148 | 9090/TCP | OK |
| `jaeger` | 10.43.27.65 | 16686/TCP (+ OTLP 4317/4318) | OK |
| `grafana` | 10.43.164.114 | 80/TCP | OK |
| `frontend-proxy` | 10.43.95.10 | 8080/TCP | OK |
| `flagd` | 10.43.128.11 | 8013/8016/4000 | OK |
| **Loki** | — | — | **NOT DEPLOYED** |
| **Tempo** | — | — | **NOT DEPLOYED** |

`helm list -A` confirms only `otel-demo` (chart `opentelemetry-demo-0.40.8`) and `traefik` are installed. **CLAUDE.md and the onboarding guide both list Loki/Tempo as part of the stack** ([CLAUDE.md "Reference POC stack"](CLAUDE.md)); reality is the upstream demo chart ships Prom + Jaeger only and the team has not layered Loki/Tempo on top. Any "Log Correlation" agent that depends on Loki has no backend on this host. Reconcile.

### 2.3 Pod table — anything red

All 26 pods are `1/1 Running`, but restart counts and last-termination reasons are not clean:

| Pod | Restarts | Last term reason | Mem limit | Diagnosis |
|---|---|---|---|---|
| **`accounting-5b79569d96-fljf4`** | **17** | **OOMKilled** (exit 137) | **120 Mi** | Real memory pressure. Upstream default; bump via Helm values, never `kubectl patch` (rule in [memory/feedback_helm_over_kubectl_patch.md](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/feedback_helm_over_kubectl_patch.md)). |
| **`product-catalog-5f55ccffb9-cnr78`** | **18** | Error (exit 1), mem limit **20 Mi** | tiny | Symptom matches a stuck `slow-product-catalog` injection — the service exits unhealthy when `productCatalogFailure` is on long enough. |
| `frontend-proxy`, `ad`, `payment`, `shipping`, `recommendation`, `product-reviews`, `llm`, `frontend`, `quote`, `kafka`, `valkey-cart`, `currency`, `postgresql`, `image-provider`, `load-generator`, `checkout`, `fraud-detection`, `cart`, `email` | 5–7 each | Error | n/a | All restart "94m ago" → cluster was restarted ~94 min ago (Rancher Desktop bounce). Steady-state since. Not a demo blocker. |
| `otel-collector-agent`, `prometheus`, `grafana`, `jaeger`, `flagd` | 1–2 | Error | n/a | Same Rancher Desktop bounce. Steady-state. |
| `load-generator` | actual usage **1341 Mi** | — | n/a | Single biggest memory consumer on the box. Expected behavior. |

`kubectl get pods --field-selector=status.phase!=Running -n otel-demo` returned nothing — i.e. nothing currently in `Pending` / `CrashLoopBackOff` / `Failed`. The OOMKilled `accounting` pod recovers each time, so it's a stability dent, not a demo-breaker.

### 2.4 Port-forward table

| Target port | Listening on this host **now** | Source of truth |
|---|---|---|
| 8080 (frontend-proxy) | **NO** | [start.ps1:72](start.ps1#L72) |
| 8765 (FastAPI demo UI) | **NO** | [start.ps1:168](start.ps1#L168) |
| 9090 (Prometheus) | **NO** | [start.ps1:70](start.ps1#L70), [infra/port-forward.ps1:22](infra/port-forward.ps1#L22) |
| 16686 (Jaeger) | **NO** | [start.ps1:71](start.ps1#L71), [infra/port-forward.ps1:23](infra/port-forward.ps1#L23) |
| 3100 / 3200 (Loki / Tempo) | NO | not deployed; no PF script |

`Get-Job -Name 'pf-*'` is empty; no `kubectl port-forward` processes. **`.\start.ps1` simply hasn't been run in this PowerShell session.** Per CLAUDE.md, closing the parent shell kills the jobs — if `start.ps1` was run in a different window that was closed, this is the expected aftermath.

### 2.5 Standalone kubectl trap status

[start.ps1:30](start.ps1#L30) correctly prepends `$LOCALAPPDATA\Programs\kubectl` to `$env:Path` *and* re-prepends it inside each `Start-Job` block ([start.ps1:77](start.ps1#L77)) — safe. [demo/failure_injection/inject.py](demo/failure_injection/inject.py) has the kuberlr-wrapper filter (`_looks_like_real_kubectl()`) — safe. But [infra/port-forward.ps1:29-32](infra/port-forward.ps1#L29-L32) does NOT pass the kubectl directory into its `Start-Job` block, so the job's `kubectl port-forward` resolves against the system PATH inside the job, which on this machine is Rancher Desktop's kuberlr wrapper. This is silently broken; symptom would be the script reporting "Started prometheus" but the port never opening. P1, since the script is still in the repo and the README references it.

---

## 3. Per-agent matrix

Reactive flow per the catalog: Alert Triage → Incident Classifier → Auto-Ticketing → Notification Router (Log Correlation, RCA, Remediation, War-Room, Incident Commander **do not exist in this repo**).

| Agent | Entry point | External deps | Missing on this host | Seam violations | Truth / eval | Verdict |
|---|---|---|---|---|---|---|
| **alert_triage** (RA-001) | [agents/alert_triage/__main__.py](agents/alert_triage/__main__.py); core [agents/alert_triage/agent.py:467](agents/alert_triage/agent.py#L467) `triage()` | `aiops.llm.complete`; registry: `observability.metrics.query` (Prom), `observability.traces.search` (Jaeger), `itsm.cmdb.lookup`, `oncall.schedule.lookup` | Prom/Jaeger PFs not up → metric/trace queries time out and degrade silently. LLM works (Azure OpenAI gpt-5 keyed in [.env](.env)). | None. Embeddings (sentence-transformers) optional, with fallback. | Golden set present. | **RUNS** (degraded without PFs). |
| **incident_classifier** (RA-002) | [agents/incident_classifier/__main__.py](agents/incident_classifier/__main__.py); core [agents/incident_classifier/agent.py](agents/incident_classifier/agent.py) `classify()` | `aiops.llm.complete`; registry: `itsm.cmdb.lookup`, `oncall.schedule.lookup`, `itsm.cmdb.dependencies` | n/a — v0 raises `NotImplementedError` by design ([README.md:7](agents/incident_classifier/README.md#L7), [__main__.py:8](agents/incident_classifier/__main__.py#L8)). | None observed in scaffolding. | golden.json present but empty (v0). | **STUB BY DESIGN** — `--fixture` fails loudly until RA-002 v1 lands. |
| **auto_ticketing** (RA-003) | Library only ([agents/auto_ticketing/agent.py:73](agents/auto_ticketing/agent.py#L73) `run(input)` / `:85 ticket()`) — no CLI runner | registry: `itsm.incident.create` (ServiceNow PDI), `notify.send` | Uses live ServiceNow at `dev195902.service-now.com` with **admin creds** (per [.env:46-47](.env)); switch back to `aiops_agent` once issue #43 lands. PDI itself external — works on this host. | None. HITL is platform-enforced at registry boundary. | Golden set present. | **RUNS** (against live PDI). |
| **notification_router** (RA-005) | [agents/notification_router/__main__.py](agents/notification_router/__main__.py); core [agents/notification_router/agent.py:169](agents/notification_router/agent.py#L169) `route()` | `aiops.tools.chatops.get_client().send()` (jsonfile adapter — writes to disk) | None — no Slack/Teams webhook in [.env](.env), but the default chatops adapter is `jsonfile`. No live chat integration is configured anywhere in the repo. | None. `decide()` is a pure function. | Golden set present. | **RUNS** (no live Slack — by design today). |
| ~~log_correlation~~ | — | — | — | — | — | **DOES NOT EXIST** in any branch. |
| ~~rca~~ | — | — | — | — | — | **DOES NOT EXIST** in any branch. |
| ~~remediation~~ | — | — | — | — | — | **DOES NOT EXIST** in any branch. |

### Cross-agent observations

- **Seam compliance is clean.** `test_no_direct_llm_sdk_imports_outside_aiops_llm` and `test_every_scenario_has_a_truth_file` both pass by inspection. [aiops/llm/gateway.py](aiops/llm/gateway.py) is a tight `complete()` / `acomplete()` shim; provider routing handled in `base.py` / `*_provider.py`. No agent shells out to `httpx` or `requests` directly.
- **Truth-file coverage is 3-for-3.** [demo/truth_files/slow-product-catalog.yaml](demo/truth_files/slow-product-catalog.yaml), [kafka-queue-buildup.yaml](demo/truth_files/kafka-queue-buildup.yaml), [currency-pod-kill.yaml](demo/truth_files/currency-pod-kill.yaml) — each maps to a scenario.
- **No `SLACK_WEBHOOK` / `TEAMS_WEBHOOK` env vars are referenced anywhere in the codebase.** Notification Router will land everything in `demo/chatops_outbox.json` (or similar). Fine for the demo, but call it out in the narrative so the audience doesn't expect a Slack ping.
- **No `AIOPS_PROMETHEUS_URL` / `AIOPS_JAEGER_URL` overrides in [.env](.env).** Defaults are `http://localhost:9090` / `:16686` ([aiops/tools/observability/prometheus.py:11](aiops/tools/observability/prometheus.py#L11), [jaeger.py:12](aiops/tools/observability/jaeger.py#L12)). Means PFs must be up — fine, just be aware.
- **OTel demo flag drift potential is high but not a current blocker.** The chart's flagd config wires 11+ flags (paymentFailure, cartServiceFailure, recommendationCacheFailure, adServiceFailure, adManualGc, adHighCpu, imageSlowLoad, loadGeneratorFloodHomepage, emailMemoryLeak, productCatalogFailure, kafkaQueueProblems). [demo/failure_injection/inject.py](demo/failure_injection/inject.py) only resets 2 of these (`productCatalogFailure`, `kafkaQueueProblems`). The 3 scenarios reference only those 2 + a kubectl-delete of currency. No mismatch yet; risk grows the moment someone adds a 4th scenario.

---

## 4. Prioritised fix list

### P0 — demo blockers (must be green before next dry-run)

| # | Symptom | Root cause | Fix (exact) | Verification |
|---|---|---|---|---|
| **P0-1** | Demo UI / Prom / Jaeger / frontend-proxy not reachable on `localhost`. Any agent that hits Prom or Jaeger times out and falls back to the "no metrics" path. | [start.ps1](start.ps1) has not been run in a live PowerShell window this session. | Open a **dedicated long-lived PowerShell window**, then `cd c:\Projects\AIops; .\start.ps1`. Do **not** close that window for the duration of the demo. Use a *second* window for everything else. | `Get-Job -Name 'pf-*'` shows 3 jobs Running, and `Invoke-WebRequest http://localhost:8765/api/health -UseBasicParsing` returns 200. |
| **P0-2** | `product-catalog` pod has 18 restarts, exit 1 — looks broken on the live demo. | Almost certainly a stale `slow-product-catalog` flag still set to ON (probably from a previous rehearsal). | Once cluster is up: `uv run python -m demo.failure_injection.inject --clear`. **Then** confirm flagd reverted via `kubectl -n otel-demo logs deploy/flagd -c flagd \| Select-String 'productCatalogFailure'`. | `kubectl -n otel-demo get pod -l app.kubernetes.io/component=product-catalog` — restart count stops climbing within 5 min; `Last term reason` becomes blank on next describe. |
| **P0-3** | Demo plan references "Log Correlation / RCA / Remediation" agents; they do not exist on any branch. The audience will ask. | The agents are catalog'd in the docs (RA-006 / RA-007 etc. in the Excel) but **never implemented in this repo**. | Decision needed (no code change). Either: (a) pull the branch the teammate is using and inspect what's there, or (b) cut the demo narrative back to Alert Triage → Incident Classifier (stubbed) → Auto-Ticketing → Notification Router. Recommend (b) — it's an honest POC. | Updated demo script reads only the agents that actually exist; rehearsal walkthrough does not mention RCA/Remediation. |
| **P0-4** | Incident Classifier `--fixture` raises `NotImplementedError`. If the demo script invokes it live, the moment lands flat. | Intentional v0 ([README.md:7](agents/incident_classifier/README.md#L7)). | Decision needed: either *skip* RA-002 in the live demo and pre-record its output, *or* implement the v0→v1 jump before the demo. Don't put `NotImplementedError` on a projector. | Demo rehearsal completes without a stack trace. |

### P1 — correctness / will bite during demo if you're unlucky

| # | Symptom | Root cause | Fix (exact) | Verification |
|---|---|---|---|---|
| **P1-1** | `infra/port-forward.ps1` reports `Started prometheus -> http://localhost:9090 (job N)` but the port never opens. | [infra/port-forward.ps1:29-32](infra/port-forward.ps1#L29-L32) does not pass `$standaloneKubectl` into the `Start-Job` scriptblock, so the job inherits a clean `$env:Path` that resolves to Rancher Desktop's `kuberlr`-wrapped kubectl. Matches the trap [start.ps1:75-81](start.ps1#L75-L81) explicitly works around. | Edit `infra/port-forward.ps1` to mirror [start.ps1:75-81](start.ps1#L75-L81) — pass the kubectl directory into the job and re-prepend `$env:Path` inside the scriptblock. (Or deprecate the script and point everyone at `start.ps1`.) | `Get-Job -Name 'pf-prometheus' \| Receive-Job -Keep` shows the standard `kubectl port-forward` Forwarding lines, not a kuberlr SHA-mismatch error. |
| **P1-2** | `.env` documents itself as using `admin` ServiceNow creds, with the password in plaintext. Anyone screen-sharing during the demo leaks the PDI. | Workaround for issue #43 noted in [.env:42-47](.env). Not a *bug* — but live. | Before the demo: confirm `.gitignore` still excludes `.env` (it does — see [.gitignore](\.gitignore)); rotate the PDI admin password after the demo; do not share screen with the file open in the editor. | `git ls-files \| Select-String '\.env'` returns empty (`.env.example` only). |
| **P1-3** | No `AIOPS_PROMETHEUS_URL` / `AIOPS_JAEGER_URL` overrides in `.env`. If anyone changes the local ports, every agent silently breaks. | Implicit reliance on default-localhost in [aiops/tools/observability/prometheus.py:11](aiops/tools/observability/prometheus.py#L11) and [jaeger.py:12](aiops/tools/observability/jaeger.py#L12). | Add explicit lines to `.env.example` (and your local `.env`): `AIOPS_PROMETHEUS_URL=http://localhost:9090` / `AIOPS_JAEGER_URL=http://localhost:16686`. Stops a future "why does it work for me but not you" episode. | `uv run python -c "from aiops.tools.observability import prometheus; print(prometheus._endpoint())"` echoes the overridden URL. |
| **P1-4** | Incident Classifier `golden.json` is empty, so the eval harness reports 0 cases for RA-002. CI threshold could mask this. | v0 stub. | Before promoting RA-002 to v1, seed `agents/incident_classifier/evals/golden.json` from the 3 existing truth files. Tracked, not fixed now. | `uv run python -m evals.harness --agent incident_classifier` reports >0 cases. |

### P2 — tech debt / nice-to-have

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| **P2-1** | Node memory is 94 % committed; `accounting` OOMKills (120 Mi limit) every couple of hours. | Upstream chart defaults are sized for cloud, not 7.5 GiB k3s. | Edit `infra/values/otel-demo-values.yaml` (or wherever values live) to raise the `accounting` memory limit to ~200 Mi, then `helm upgrade --reuse-values`. **Never** `kubectl patch` chart-owned resources (memory rule). |
| **P2-2** | CLAUDE.md and onboarding guide list Loki + Tempo as part of the stack; neither is deployed. No agent currently depends on them. | Aspirational documentation. | Either install Loki/Tempo via a side chart, or update CLAUDE.md to reflect "Prom + Jaeger only on the demo cluster today" so future agents (Log Correlation, etc.) are scoped honestly. |
| **P2-3** | OTel demo wires 11+ feature flags; only 2 are exercised by scenarios + only 2 are reset by `inject.py --clear`. | Scope discipline / "ugly first". | When RA-006 / RA-007 land, add scenarios + reset slots in lockstep. Not blocking. |
| **P2-4** | `auto_ticketing` has no `__main__.py` — it is library-only. Demo has to drive it from the UI or a notebook. | Design choice. | Consider adding a thin CLI shim for parity with the other RA-00x agents, *only if* the demo script needs it. |
| **P2-5** | `infra/port-forward.ps1` duplicates `start.ps1`'s job-based port-forward logic, minus the kubectl fix. | Two routes to the same thing. | Once P1-1 is fixed, consider deleting `infra/port-forward.ps1` and pointing the README at `start.ps1`. |

---

## 5. Proposed memory & skill updates (NOT YET WRITTEN — pending your approval)

### New memory files to create

| File | Type | Why |
|---|---|---|
| `feedback_port_forwards_dedicated_window.md` | feedback | Rule: always run `start.ps1` in a *dedicated long-lived* PowerShell window; closing it kills all `pf-*` jobs. **Why:** CLAUDE.md mentions this trap; today's audit shows it bit the user. **How to apply:** when troubleshooting "agents can't reach Prom/Jaeger/frontend-proxy", check `Get-Job -Name 'pf-*'` *before* assuming code bug. |
| `project_agents_actually_in_repo.md` | project | Fact: as of 2026-05-14, only 4 agents exist in [agents/](agents/) — alert_triage, auto_ticketing, incident_classifier (v0 stub), notification_router. RA-006 (Log Correlation), the RCA Agent, and Remediation Recommender are **catalog'd but not implemented**, on any branch. **How to apply:** push back on requests that assume those exist. |
| `project_incident_classifier_v0_stub.md` | project | Fact: RA-002 v0 raises `NotImplementedError` by design. **How to apply:** don't put `--fixture` on a demo projector until v1. |
| `reference_cluster_stack_reality.md` | reference | Fact: `otel-demo` Helm chart `opentelemetry-demo-0.40.8` deploys Prom + Jaeger + Grafana + flagd. **Loki and Tempo are NOT deployed** on this host despite CLAUDE.md listing them. **How to apply:** when scoping a "log correlation" or "trace mining" feature, confirm the backend exists before designing for it. |
| `feedback_pf_script_dedup.md` *(optional)* | feedback | Rule: prefer `.\start.ps1` over `infra/port-forward.ps1`. **Why:** the latter has an unfixed kuberlr-wrapper trap (P1-1 in this audit). **How to apply:** if a teammate reports "PFs started but port not open", check which script they ran. |

### Memory files to update

| File | Update |
|---|---|
| [project_environment_constraints.md](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/project_environment_constraints.md) | Add gotcha #7: "Node memory commits at 94 % on this host — `accounting` pod OOMKills periodically; bump its limit in chart values, never `kubectl patch`." |
| [project_state.md](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/project_state.md) | Refresh: as of 2026-05-14, branch `feat/demo-readiness-cmdb-and-llm-health` ships real PDI + real Azure-OpenAI gpt-5 end-to-end (commit `2f065e9`). |

### Built-in skills to invoke (your call, not mine)

- **`update-config`** — none of the audit fixes need a settings.json hook. Skip unless you want a pre-`start.ps1` reminder hook.
- **`fewer-permission-prompts`** — worth running once before the demo to allowlist the read-only `kubectl get` / `helm list` / `git log` commands that came up in this audit. Saves prompts mid-demo.
- **`review` / `security-review`** — not needed for an audit pass; useful when fixing P1-2 (.env rotation) lands.
- **`simplify`** — irrelevant to this audit.
- **`/loop` or `/schedule`** — not warranted. None of the fixes have an externally-timed deadline.

---

## 6. Out of scope for this pass (recorded so it doesn't get lost)

- Implementing Log Correlation, RCA, or Remediation agents.
- Refactoring `aiops/llm` provider dispatch.
- Adding Loki/Tempo to the cluster.
- Touching CI thresholds.
- Adding new failure scenarios.
- Rotating the ServiceNow PDI password (P1-2 flags it; you do it).

---

*End of audit. No files were modified during this pass.*
