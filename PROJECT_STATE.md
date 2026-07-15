# Project State — Detailed Context Snapshot

> **Snapshot date:** 2026-07-14 · **Pinned to:** `origin/main @ a2ac883` (2026-07-09)
> **Generated for:** a returning developer (or a fresh Claude session) who needs the full current picture in one read.
> **How this relates to the other docs:** `CLAUDE.md` = principles + guardrails · `ARCHITECTURE.md` = architecture prose · `SESSION_NOTES.md` = dated session log · **this file = the comprehensive current-state snapshot** tying them together with live git/ownership/pending detail. When they disagree, code + git win; this file is a point-in-time snapshot and will drift.

---

## 1. At a glance

- **What:** Adaptive AIOps + SRE Ops — a vendor-neutral, multi-agent platform that turns an alert into a triaged → classified → ticketed → (safely) remediated incident. Vision = 30 modular, individually-sellable agents across four maturity phases; headline differentiator = an **RCA Agent** emitting executable fix steps + rollback.
- **This repo:** the **POC** build. Runs entirely locally on Rancher Desktop k3s against the OpenTelemetry Demo (Astronomy Shop). No cloud, no Docker, no real customer data.
- **Core rule:** agents are plain Python; everything external goes through a **platform seam** under `aiops/` (LLM, tools, policy, state, runtime). No agent imports a vendor SDK; no agent self-approves a destructive action.
- **Status:** Phases 1–2 shipped; RA-007 Log Correlation (Loki) and a hardened Knowledge Synthesizer just landed (Jul 8–9). **~10 agents** implemented; ~60 API routes; 15 failure scenarios each with a truth file.
- **Your local clone is 3 commits behind `origin/main`.**

## 2. Product & scope

| | |
|---|---|
| **Problem** | Alert fatigue (target −60–75% noise), slow MTTA/MTTR (target MTTA <2 min, MTTR −40–55%), toil (target ≥500 hrs/qtr eliminated). Plus two bets: no vendor lock-in, and *solving* RCA (executable fixes, not a cause list). |
| **Four phases** | Reactive-Active ("what broke?") → Proactive ("what's starting to look wrong?") → Predictive ("what will break & when?") → Prescriptive-Adaptive ("what should we do — and can we do it?"). |
| **POC discipline** | 6–10 agents end-to-end on one Reactive→Prescriptive flow; synthetic data only; end-to-end-ugly-first; the original 6-min demo (Jun 14) has passed and its RCA/HITL/PagerDuty tracks all shipped. |
| **Authoritative agent contracts** | `docs/Adaptive_AIOps_Agent_Catalog.xlsx` (the vision catalog — 30 rows). **Note:** the shipped product merged several rows, so catalog IDs and shipped directories do **not** map 1:1 (see §4). |

## 3. Architecture — the seams

Agents call these, never vendors. Every external call crosses exactly one seam.

```
aiops/
├── llm/          LLM gateway — the only place a model is called
│   ├── gateway.py            complete() / acomplete(); dispatch by AIOPS_LLM_PROVIDER
│   ├── base.py               LLMRequest / LLMResponse / Message types
│   ├── health.py             ping() cached 1-token health probe (feeds /api/health)
│   └── {anthropic,openai,ollama,stub}_provider.py
├── tools/        Tool registry — capabilities, not vendors; invokes the HITL gate before each call
│   ├── registry.py           get_registry().call(capability, **kwargs) -> ToolResult
│   ├── alerts/               normalize raw monitoring → canonical Alert
│   │   └── {prometheus,datadog,cloudwatch,alertmanager}_adapter.py   (Prom live; others = vendor-neutrality proof)
│   ├── observability/        read-only (autonomy NONE): prometheus.py, jaeger.py, grafana.py
│   ├── itsm/                 servicenow.py (+ _demo_cmdb.py); itsm.incident.create/update, cmdb.lookup
│   ├── chatops/              client.py + models.py + war_room_bridge.py
│   │   └── adapters/         jsonfile.py, slack.py, slack_bot.py, pagerduty.py, _slack_user_map.py
│   ├── feature_flags/        flagd ConfigMap SSA adapter (ARCH-1) — the ONLY sanctioned scenario-mutation path
│   ├── oncall.py             on-call roster routing            knowledge.py   rca_remediation.py
│   ├── resolvers.py          past-resolver / SME recall        mock_providers.py  itsm_close.py
├── policy/       HITL gate + approvals
│   ├── gate.py               get_gate().check(action, ctx) -> Decision; DEFAULT_LEVELS map (None/Optional/Required)
│   └── approvals.py          ApprovalRegistry — opens pending request, posts to chatops, blocks until human decides (fail-closed)
├── runtime/
│   └── orchestrator.py       run_reactive_flow(alert) — the ONE entry point for RA-001→003→005+006
├── state/        persistence (only importer of SQLModel)
│   ├── repository.py         save_verdict / save_classification / ... (SQLite default; Postgres = URL swap)
│   ├── oncall_repository.py  on-call roster store
│   └── models.py
└── runbooks/     store.py + models.py — runbook definitions for auto_healer_lite / runbook_executor
```

**Canonical data flow:**

```
Alert → alerts.normalize → RA-001 triage()+classify() → TriageVerdict + Classification
   ├─ RA-003 ticket()  → ServiceNow incident (+ Grafana screenshot)
   └─ RA-005+006 notify() → one Slack/PagerDuty msg (war-room link folded in for Sev-1/2)
 (Prescriptive, on demand)
   RA-007 correlate() → log evidence   →  PRS-008 analyze() → RCAVerdict + ranked fix steps (each requires_hitl)
      → PRS-001 recommend() → remediation → policy gate.check()==REQUIRED → chatops Approve/Deny → human
           → PRS-002 execute() / RA-004 runbook → ToolResult → audit (chatops.jsonl + state.db)
   PRS-007 synthesize() → postmortem + KB draft (publish HITL-gated)
   RA-008 command() coordinates the whole Sev-1/2 response via the orchestrator
```

Two persistence sinks: `data/state.db` (verdicts, classifications, tickets, notifications) and `demo/audit/chatops.jsonl` (every notification + approval lifecycle event — the audit-trail demo beat).

## 4. Agent inventory (shipped)

Every agent exposes a uniform `run(input: dict) -> dict` entry point (used by the orchestrator + eval harness) plus the domain functions below.

| Directory | Catalog ID | Phase | Key entry points | HITL |
|---|---|---|---|---|
| `alert_triage/` | RA-001 **+002** | Reactive | `triage(alert) -> (TriageVerdict, id)` · `triage_and_classify(alert) -> CombinedResult` | None |
| `auto_ticketing/` | RA-003 | Reactive | `ticket(verdict) -> TicketRecord` (ServiceNow) | Optional |
| `runbook_executor/` | RA-004 | Reactive | `select(incident)` · `run_plan(...)` · `execute_runbook(...)` — simulate-then-execute + audit log | Required (execute) |
| `notification_assembler/` | RA-005 **+006** | Reactive | `decide()` · `notify()` · `assemble_war_room()` · `route()` — one message, war-room link folded in | Optional |
| `log_correlation/` | RA-007 | Reactive | `correlate(CorrelationInput, force_synthetic=False) -> CorrelationResult` (Loki-backed) | None |
| `incident_commander/` | RA-008 (SRE) | Reactive | `command(...)` — coordinates Sev-1/2 via orchestrator + RCA; scribes timeline; **no destructive action** | None |
| `remediation_recommender/` | PRS-001 | Prescriptive | `recommend(RemediationInput) -> RemediationVerdict` — sits between RCA and Auto-Healer (folded into RCA console) | — |
| `auto_healer_lite/` | PRS-002 | Prescriptive | `recommend_restart(...)` · `execute(ExecutionRequest) -> ExecutionVerdict` — the runnable HITL demo | **Required** |
| `knowledge_synthesizer/` | PRS-007 | Prescriptive | `synthesize(bundle, scenario_id=None) -> SynthesisResult` — postmortem + KB draft; ticket-closed-gated 3-approval publish | Required (publish) |
| `rca_agent/` | PRS-008 ★ | Prescriptive | `analyze(...) -> RCAVerdict` — ranked fix steps w/ blast radius + rollback; **recommend-only** (does not execute); pinned to Anthropic | **Required** per step |

> **Catalog-ID drift:** the vision catalog lists RA-002, RA-006 as separate agents (merged here) and numbered Log Correlation differently in the old `DEMO_PLAN.md`. Trust the directory + git reality above; use the catalog for *contract intent*, not for a 1:1 dir map. `agents/README.md` is the authoritative shipped inventory.

## 5. API surface (`demo/ui/server.py` + routers, ~60 routes at :8765)

| Group | Routes |
|---|---|
| **Health/meta** | `GET /api/health` · `GET /metrics` · `GET /api/fixtures` |
| **Triage chain** | `POST /api/triage` · `POST /api/triage/fixture/{id}` · `POST /api/triage/live` · `POST /api/triage-full` |
| **Combined UI** | `GET /api/combined/fixtures` · `POST /api/combined/run` |
| **Classifier** | `GET /api/classifier/classifications` · `/metrics` · `POST /api/classifier/evaluate` |
| **RCA / remediation** | `POST /api/rca` · `POST /api/remediation` · `POST /api/execute` · `POST /api/demo/rca/apply-fix` |
| **Auto-heal** | `POST /api/demo/auto-heal/restart` · `/execute` · `GET /api/demo/auto-heal/outcome/{id}` |
| **Runbook executor** | `POST /api/demo/runbook-executor/run` · `GET /api/runbook-executor/runbooks` · `GET /api/runbooks/by-service/{svc}` |
| **Incident Commander** | `POST /api/incident-commander` |
| **HITL approvals** | `GET /api/approvals` · `/{id}` · `POST .../approve` · `.../deny` · `POST /api/approvals/slack/callback` |
| **Knowledge (router)** | `POST /api/synthesize` · `GET /api/kb` · `/{id}` · `POST /api/kb/{id}/publish` · `GET /api/kb/publish/outcome/{id}` |
| **War-room** | `POST /api/war-room/assemble` · `GET .../recent` · `/metrics` · `POST .../{id}/status` · `/attendee` |
| **Scenarios** | `GET /api/scenarios` · `POST /api/scenarios/{id}/inject` · `/reset` · `/reset-all` |
| **Observability** | `GET /api/topology` · `GET /api/system/pods` · `GET /api/live-alerts` · `GET /api/verdicts` · `GET /api/notifications` |
| **SPAs** | `/dashboard` · `/classifier` · `/combined` · `/hitl` |
| **WebSockets** | `/ws/alerts` (live alert push) · `/ws/chatops` (live chatops feed) |

## 6. Demo scenarios & evals

**15 failure scenarios** under `demo/scenarios/*.yaml`, each with a matching ground-truth file under `demo/truth_files/*.yaml` (+ `template.yaml`). A smoke test enforces the 1:1 pairing.

`ad_failure · ad_high_cpu · ad_manual_gc · cart_failure · currency-pod-kill · email_memory_leak · image_slow_load_10s · kafka-queue-buildup · kafka_backpressure · loadgen_homepage_flood · payment_failure · payment_unreachable · product_catalog_failure · recommendation_cache_failure · slow-product-catalog`

- **Inject/clear:** `uv run python -m demo.failure_injection.inject <name>` / `--clear` / `--list` (bare flagd flip — does NOT auto-trigger agents).
- **Eval harness:** `uv run python -m evals.harness` (per-agent `--agent <name>`; CI gate `--ci --min-pass-rate 0.85`). Golden cases live at `agents/<dir>/evals/golden.json`.

## 7. How to run (this machine)

Prereqs already present: `uv`, `kubectl`, `helm`, Rancher Desktop k3s. Python 3.12 is auto-installed by `uv`. Scripts require `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (one-time).

```powershell
cd "C:\Users\Admin\Documents\Adaptive AIOps\AIops"
uv sync --extra dev --extra ui        # deps (+ embeddings optional)
.\infra\bootstrap.ps1                 # deploy OTel demo + (now) Loki — ~10 min, idempotent
.\start.ps1                           # port-forwards + build SPAs + FastAPI UI at :8765  (run in the window you'll keep)
# exercise the full chain (real LLM round-trip):
Invoke-RestMethod -Method POST http://localhost:8765/api/triage/fixture/payment_cpu_spike -TimeoutSec 90 | ConvertTo-Json -Depth 4
.\reset.ps1 [-Hard]                   # clean slate before a rehearsal
.\stop.ps1                            # tear down port-forwards + UI (cluster stays up)
```

**Gotchas (learned the hard way — see SESSION_NOTES 2026-07-14):**
1. `start.ps1` background jobs die with their PowerShell session; run it in the window you'll actually use, or processes orphan on ports 8765/8080/9090/16686 and return HTTP 500. Kill orphans via `Get-NetTCPConnection -LocalPort 8765` → `Stop-Process -Force` (don't name a loop var `$pid` — reserved).
2. Dashboard **Inject** flips the flag but does **not** fire Prometheus alerts (`STATUS_CODE_UNSET`). Drive agents via `POST /api/triage/fixture/<name>`.
3. `bootstrap.ps1` "context deadline exceeded" is a false alarm; `otel-collector-agent` CrashLoop (can't reach central collector) is non-blocking.
4. `.env` holds **live** Azure OpenAI / ServiceNow / Slack / PagerDuty creds — treat this folder as sensitive.

## 8. Recent work & ownership

**You (Gaurav** — on Chinmay's account; roster key `vikram`, GH `Gaurav-Patil-1695`): the **Prescriptive/HITL side** — RCA console, Remediation Recommender (folded into RCA console, #205–207), Auto-Healer-Lite, RA-005/006 routing + war-room (IST business hours, CMDB-owner SMEs), Track C PagerDuty. Last landed **Jul 1**.

**Landed while you were away (the 3 commits ahead of your clone):**
- Jul 9 — RA-007 Log Correlation **live**: Loki + logs pipeline + dashboard page (Khushi #225) + live Loki provider (Varad #221)
- Jul 8 — Knowledge Synthesizer: ticket-closed gate + real 3-approval lifecycle (Khushi #224)

**Team:** Khushi (`riya`) — merges, RA-004 audit log, notifications, RA-007 deploy · Shravani (`arjun`) — agent mergers, RA-007 provider · Varad/"VP" (`meera`) — RA-003, Loki, IC timeline · Sharvari — docs/UX · Chinmay (`chinmay`) — original Phase-0 setup.

## 9. Pending / next steps

- [ ] **Sync:** discard build-artifact noise (`vite.config.*`, `*.tsbuildinfo`), keep the `CLAUDE.md` doc refresh, `git pull` the 3 commits.
- [ ] **Re-bootstrap for Loki:** this cluster predates RA-007 — re-run `.\infra\bootstrap.ps1` so the logs pipeline actually deploys.
- [ ] **Check `origin/backup/pre-format-local-work`** (yours, Jul 7) for stranded work.
- [ ] **Untrack** dashboard `*.tsbuildinfo` / built `vite.config.*` (branch `chore/untrack-dashboard-tsbuildinfo` exists for this) so they stop showing dirty after every build.
- [ ] **Set git identity** on this machine (`git config user.name/email`) so commits attribute to you, not unset/Chinmay.
- [ ] Forward-looking backlog: `origin/docs/post-poc-roadmap` branch.

## 10. Reference map

| Want | Read |
|---|---|
| Principles & guardrails | `CLAUDE.md` |
| Architecture prose + failure modes | `ARCHITECTURE.md` |
| Dated session log | `SESSION_NOTES.md` |
| Agent contracts (vision) | `docs/Adaptive_AIOps_Agent_Catalog.xlsx` + `agents/README.md` (shipped) |
| Product scope / non-goals | `PRD.md` |
| Daily run commands + sharp edges | `RUNNING.md` |
| Laptop setup from scratch | `ONBOARDING.md` |
| Design decisions | `docs/adr/` |
| Risks | `RISK_REGISTER.md` |
