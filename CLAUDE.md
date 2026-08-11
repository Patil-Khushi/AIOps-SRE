# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

The repo has the **Phase 0 platform seams** (`aiops/llm`, `aiops/tools`, `aiops/policy`, `aiops/state`, `aiops/runtime`), the demo bootstrap (`infra/`, `demo/otel-demo`, `demo/failure_injection`, `demo/truth_files`, `demo/load`), the eval harness, OPA policy, and CI in place. **Phase 1 is shipped and Phase 2 is mostly shipped**: merged agents under `agents/` — `alert_triage/` (RA-001+002 combined: triage + classification), `auto_ticketing/` (RA-003), `notification_assembler/` (RA-005+006 combined: routing + war-room), plus `incident_commander/` (RA-008), `rca_agent/` (PRS-008), `knowledge_synthesizer/` (PRS-007), `log_correlation/` (RA-007), `auto_healer_lite/` (HITL demo), and post-POC stubs. The chatops seam lives under `aiops/tools/chatops/` (JSON-file + WebSocket + Slack + Teams adapters), the combined triage/classifier UI at `/combined`, and the React dashboard at `/dashboard`. The `docs/` design files remain the authoritative source for agent contracts and architecture.

### Agent mergers: the catalog says 30, the product ships 19

The **vision catalog** (`docs/Adaptive_AIOps_Agent_Catalog.xlsx`) still lists 30 agents as separate rows. The **shipped catalog** (`demo/dashboard/src/data/agentCatalog.ts`, commit `1ede493`) consolidated those into **19** product-named agents:

| Phase | Shipped count | Merges applied |
|---|---|---|
| Reactive-Active | 6 | Alert Triage + Incident Classifier → **Alert Triage Agent**; Notification Router + War-Room Assembler → **Notification Router** |
| Proactive | 3 | Anomaly Detector + Drift Monitor + Noise Reducer + Early Warning → **Proactive Sensing**; Topology Discovery + Dependency Mapper → **Service Graph** |
| Predictive | 5 | Failure Forecaster + SLO Breach Predictor + Reliability Forecaster → **Reliability Prediction** |
| Prescriptive-Adaptive | 5 | Feedback Learner + Policy Optimizer → **Closed-Loop Learning**; RCA absorbs Remediation Recommender + Auto-Healer |

Two of these merges are real code merges, not just catalog relabelling:

- **RA-001 + RA-002** → `agents/alert_triage/` owns both triage and incident classification. `triage(alert)` returns the verdict only, `classify(payload)` the classification only, `triage_and_classify(alert)` both as a `CombinedResult`. Classification code lives in `alert_triage/classifier*.py`. The former `incident_classifier/` package was deleted.
- **RA-005 + RA-006** → `agents/notification_assembler/` owns both routing and war-room assembly, and emits **one** message per incident (war-room join link folded in for Sev-1/2). The former `notification_router/` and `war_room_assembler/` wrappers were deleted.

The `agentCatalog.ts` `agent()` factory takes an optional `id` override so renamed agents keep stable routes (`alert-triage`, `notification-assembler`, `topology-discovery`) — renaming an agent's display label does not require touching `Sidebar` or `consoleScope`.

Catalog rows and directory names do not match 1:1. `agents/README.md` documents the merge decisions in prose but has fallen behind disk — it currently omits `log_correlation/`, `rca_agent/`, `remediation_recommender/`, `resolution_verifier/`, `runbook_executor/`, and `auto_healer_lite/`. Treat the **Repository layout** tree below, not `agents/README.md`, as the current shipped inventory; the eleven directories under `agents/` there are all shipped.

Source-of-truth documents (binary Office files):

- `docs/Adaptive_AIOps_Unified_Architecture.pptx` — the one-slide architecture diagram (the master picture).
- `docs/Adaptive_AIOps_Solution_Design.pptx` — phase decomposition, integration matrix, HITL policy, rollout plan, KPIs, risks.
- `docs/Adaptive_AIOps_Agent_Catalog.xlsx` — every agent with description, key features, primary tool mapping, secondary integrations, inputs/outputs, HITL level, sellable-standalone flag, KPI. **Authoritative agent reference.** Sheets: README, Master, Reactive-Active, Proactive, Predictive, Prescriptive-Adaptive, Tool-Mapping-Matrix, Phase-Summary.
- `docs/aiops_onboarding_guide.docx` — concept primer (AIOps, SRE, RCA, agentic AI vocabulary).
- `docs/poc_aiops_onboarding_guide.docx` — POC playbook: data problem, what to use instead, reference stack, 12-week roadmap, pitfalls.

When the design intent is unclear, extract text from the docx/pptx/xlsx (they are zip archives of XML — `xl/sharedStrings.xml` for Excel strings, `ppt/slides/slideN.xml` for slides, `word/document.xml` for Word) and consult the catalog before guessing. Do not invent agent behavior or integrations the catalog does not specify.

### `docs/adr/` overrides this file's stack table

Architectural decisions are recorded as ADRs (`docs/adr/NNNN-*.md`, Nygard format, immutable once Accepted). They back-fill decisions that previously lived only in this file, and where an Accepted ADR is narrower than the stack table below, **the ADR wins**:

| ADR | Decision | Status |
|---|---|---|
| 001 | flagd mutation goes through the `feature_flags.*` seam, never `kubectl patch` | Accepted |
| 002 | **No agent framework for the POC.** Agents are plain functions; `aiops/runtime/orchestrator.py` is the "framework". Do not add LangGraph/AutoGen/CrewAI. | Accepted |
| 003 | Anthropic (Azure AI Foundry Claude) is the default; OpenAI the swap-in; Ollama the offline fallback; `stub` for tests/CI | Accepted |
| 004 | One `ApprovalRequest` in `aiops/policy/approvals.py` fans out to chat + web surfaces; either can resolve it | Accepted |
| 005 | OPA is the chosen engine but **reference-only today** — see the note under "The seams to use, not bypass" | Accepted (direction) |
| 006 | No vector store until an agent needs persistent semantic retrieval; then pgvector | Proposed |
| 007 | Truth files are YAML in the repo, not DB rows | Accepted |

Changing an Accepted decision means writing a *new* ADR that supersedes the old one — not editing the old file.

### Which root document answers what

The repo root carries a lot of markdown. Rather than reading all of it: `README.md` (quick start; its status table is stale — trust this file's roadmap instead) · `ONBOARDING.md` (laptop setup, gotchas, tailing the audit log) · `RUNNING.md` (day-to-day run loop) · `CONTRIBUTING.md` (branching, PR gates) · `PRD.md` / `KPI.md` / `EVAL_METHODOLOGY.md` (product scope, metrics, how agents are scored) · `DEMO_PLAN.md` / `DEMO_SCRIPT.md` / `DEMO_SHOWCASE.md` (rehearsed demo narrative) · `PROJECT_STATE.md` / `SESSION_NOTES.md` (point-in-time status — may be stale, verify before relying on it) · `SECRETS.md` (git-crypt workflow) · `THREAT_MODEL.md` / `RISK_REGISTER.md` · `SOLUTION_BRIEF.md` (the long-form external-facing writeup).

## What is being built

**Adaptive AIOps + SRE Ops** — a vendor-neutral, multi-agent platform that automates IT operations across four maturity phases. The product is **19 modular agents**, each individually sellable, with a dedicated **RCA Agent** as the headline differentiator (it produces executable fix steps with rollback, not just a likely-cause list).

The four phases (each with one SRE-specific agent):

| Phase | Count | Question | Representative agents |
|---|---|---|---|
| Reactive-Active | 6 | "What just broke?" | Alert Triage Agent, Auto-Ticketing, Runbook Executor, Notification Router, Log Correlation, **Incident Commander (SRE)** |
| Proactive | 3 | "What is starting to look wrong?" | Proactive Sensing, Service Graph, **Toil Detector (SRE)** |
| Predictive | 5 | "What will break, and when?" | Reliability Prediction, Capacity Planner, Seasonality Learner, Root-Cause Predictor, Change Impact Predictor |
| Prescriptive-Adaptive | 5 | "What should we do — and can the system do it?" | Closed-Loop Learning, Cost-Aware Scaler, Knowledge Synthesizer, **Chaos Orchestrator (SRE)**, **RCA Agent ★** |

Agents map onto a shared **Agentic AI Runtime** with six components: Planner, Router, Orchestrator, Memory, Tool Registry, Eval Harness. Third-party agents are first-class via **MCP** (tool/data access), **A2A** (agent-to-agent delegation), and **OpenAPI** (REST integrations).

## Non-negotiable design principles

These come from the Solution Design and must shape every code decision. Treat them as hard constraints, not aspirations.

1. **Vendor-neutral by default.** Every integration point has at least two documented alternatives. **Wrap every external dependency (LLM, ITSM, observability, automation) behind a thin internal interface from day one.** Never let `anthropic.messages.create()` or `ServiceNowClient` calls leak into agent code directly. The cost of doing this later is enormous.
2. **Modular and individually sellable.** Each agent is a standalone unit with a stable contract — inputs, outputs, KPI, HITL level. License-one or license-all must both work. Avoid abstractions that couple agents to each other beyond their declared input/output schemas.
3. **HITL is platform-enforced, not agent-enforced.** Every agent has one of three autonomy levels — **None / Optional / Required** (see catalog and Solution Design slide 10). Required-HITL actions (Runbook Executor, Capacity Planner, SLO Breach Predictor, Change Impact Predictor, Remediation Recommender, Policy Optimizer, Feedback Learner, Knowledge Synthesizer, Chaos Orchestrator, **every RCA Agent fix step**) must be gated by the platform layer so a buggy or compromised agent physically cannot bypass the gate. Don't put HITL checks inside agent logic.
4. **Policy-as-code governance.** Every action passes through a declarative policy layer (target: OPA) before execution. Policy lives in Git, reviewed like code.
5. **Safe autonomy as primitives.** Dry-run, simulation, blast-radius caps, circuit breakers, and rollback are first-class — not bolted on. Every action the agents take must be reversible, and the reverse must have been tested at least once.
6. **Closed-loop learning.** Every model, prompt, and policy is versioned and runs through a shadow eval harness before promotion. Champion/challenger by default; auto-rollback on regression.
7. **Evaluation harness from day one.** When you build an agent, build its eval set in the same week. A prompt change is a model change — re-run evals. "Looks good in the demo" is not a metric.
8. **Truth files for every demo scenario.** Each failure scenario the team injects must have a written truth file (what is broken, what the real cause is, what the correct fix is) so the eval harness has ground truth. Without this the team grades itself on vibes.

## POC scope discipline (READ THIS BEFORE BUILDING)

The owner is at POC stage and the explicit guidance is:

- **Do not build all 19 agents.** A reasonable POC scope is **6–10 agents end-to-end** on one full Reactive→Prescriptive flow (typical: Alert Triage → Incident Classifier → Auto-Ticketing → Log Correlation → RCA Agent → Remediation Recommender, plus one or two SRE agents and one Predictive agent for the "wow" moment). The rest may be stubbed for narrative continuity.
- **End-to-end ugly first, refactor second.** Get one full path working with tape-and-glue before designing shared abstractions. Working demo first; the architecture is for the production phase.
- **Demo on synthetic / open-source / demo-app data**, not real customer data. Default demo target: the **OpenTelemetry Demo (Astronomy Shop)** on Kubernetes (Rancher Desktop's bundled k3s locally; AKS/GKE deferred until post-POC). Failure injection via OTel demo feature flags + Chaos Mesh; load via k6.
- **Scope creep is the silent killer.** "While we're at it" lands on the post-POC backlog, not the current sprint.

## Reference POC stack (defaults from the onboarding guide)

When picking a tool for a new component, default to the choice in this table unless there's a reason not to. The stack is intentionally FOSS-heavy so the demo runs anywhere.

| Concern | Default | Notes |
|---|---|---|
| Demo app | OpenTelemetry Demo | Already instrumented; has feature flags for failures. |
| Cluster | **Rancher Desktop's bundled k3s** (Windows/macOS) | Org policy bans Docker on dev machines, so kind/k3d/Docker Desktop are out. Cloud (AKS/GKE) deferred. |
| Metrics / Logs / Traces | Prometheus / Loki / Tempo + Grafana | All FOSS, all integrate. |
| Instrumentation | OpenTelemetry SDKs + Collector | Vendor-neutral. |
| Tickets | ServiceNow PDI (primary) + Jira free tier (secondary) | Two integrations prove vendor-neutrality. |
| Alerting / on-call | PagerDuty developer account | Real on-call workflow. |
| Chat ops | Slack or Microsoft Teams | Match what the org uses. |
| Failure injection | OTel demo flags + Chaos Mesh | Flags for easy, Chaos Mesh for advanced. |
| Load | k6 | Modern, scriptable. |
| Agent framework | **None** — plain functions + `aiops/runtime/orchestrator.py` | Superseded by ADR-002. Do not add LangGraph/AutoGen/CrewAI to `pyproject.toml`. |
| LLM | Anthropic (Azure Foundry Claude) default; OpenAI swap-in; Ollama offline; `stub` in CI | ADR-003. Pin model versions; never use "latest". RCA Agent is pinned to Anthropic regardless of `AIOPS_LLM_PROVIDER`. |
| Vector store | Deferred; **pgvector** when first needed | ADR-006. Alert Triage dedup embeds in memory today — no store is deployed. |
| Topology graph | Neo4j Community or in-process JSON | Start simple. |
| Policy / governance | Open Policy Agent (OPA) | Industry-standard policy-as-code. |
| Evals | Hand-rolled JSON test cases first | Add Ragas/DeepEval/LangSmith only when count gets unwieldy. |
| Source control / CI | GitHub + GitHub Actions | |

## 12-week POC roadmap (target shape)

The onboarding guide specifies five phases. Use this to judge what's in scope at any given moment:

- **Phase 0 — Setup (W0–2):** ✅ *Shipped.* Repo skeleton, demo app deployed with OTel→Prom/Loki/Tempo flowing, failure-injection library, truth-file template, LLM API access.
- **Phase 1 — Reactive backbone (W3–5):** ✅ *Shipped.* Alert Triage v1 (RA-001+002 merged), Auto-Ticketing v1 (ServiceNow PDI), Notification Assembler v1 (RA-005+006 merged), Log Correlation v1, eval harness, demo UI, dashboard. *Out of scope: predictive, full RCA, prescriptive autonomy.*
- **Phase 2 — RCA backbone (W6–8):** ✅ *Mostly shipped.* RCA Agent v1, HITL UI, Incident Commander v1, Knowledge Synthesizer v0 (postmortem drafting + HITL-gated KB publish), audit trail. *Out of scope: predictive, prescriptive autonomy, chaos.*
- **Phase 3 — Proactive + first prediction (W9–10):** *In progress / planned.* Anomaly Detector, Dependency Mapper (live OTel service map), Early Warning, SLO Breach Predictor, Reliability Forecaster. *Out of scope: full predictive suite, chaos.*
- **Phase 4 — Polish + demo (W11–12):** *Planned.* Rehearsed scenarios, recorded demo, postmortems, post-POC backlog.

## Concept cheat sheet (so you can read the docs without rereading them)

- **MTTA / MTTR / MTTD / MTBF** — Mean Time To Acknowledge / Resolve / Detect / Between Failures.
- **SLI / SLO / SLA** — measurable indicator / internal target / customer contract.
- **Error budget** — `1 − SLO` over a window; spent on changes, chaos, outages.
- **Toil** — repetitive manual automatable work that scales linearly with system size; SRE's named enemy. Toil Detector mines for it.
- **Blast radius** — how much damage one action can do if it goes wrong. Auto-Healer and Chaos Orchestrator both enforce blast-radius caps.
- **Runbook** — step-by-step procedure. Runbook Executor automates safe ones.
- **CMDB / CI** — Configuration Management Database / Configuration Item.
- **HITL** — Human-In-The-Loop gating. Three levels: None, Optional, Required.
- **RAG** — Retrieval-Augmented Generation. Most agents that "know about the system" use it.
- **MCP / A2A / OpenAPI** — the three open contracts third-party tools and agents plug in through.
- **PDI** — ServiceNow Personal Developer Instance (free full ServiceNow tenant for dev).

## Repository layout

```
aiops/                     # platform seams — never call vendor SDKs outside this package
├── llm/                   # provider-agnostic LLM gateway (anthropic / openai / ollama / stub)
├── context/               # Context Engineering Layer — shared evidence pipeline (opt-in, see below)
│   └── collectors/        # thin translators from the tool registry into Observation objects
├── tools/                 # tool registry — every external integration registers here
│   ├── chatops/           # ChatOpsClient + JSON-file + WebSocket + Slack + Teams adapters
│   ├── feature_flags/     # flagd adapter (replaces the kubectl-patch shell-out, ARCH-1)
│   ├── itsm/              # ServiceNow PDI client (incident.create/update, cmdb.lookup)
│   ├── observability/     # read-only Prometheus + Jaeger + K8s events queries (autonomy NONE)
│   ├── scm/               # GitHub read-only source seam (scm.* capabilities, autonomy NONE)
│   ├── topology/          # service dependency resolution (CMDB-backed, own provider chain)
│   ├── incident_history/  # similar-incident retrieval (own provider chain, not a registry capability)
│   ├── change_context/    # union of recent-change signals: GitHub/GitLab/ArgoCD/Jenkins/flags/K8s
│   ├── resilience.py      # shared timeout+retry+breaker+cache wrapper (`guard`) for provider seams
│   └── alerts/            # alert normalization (Prometheus → canonical Alert)
├── policy/                # platform-enforced HITL gate (None / Optional / Required)
├── state/                 # SQLModel persistence (sqlite default; Postgres via URL swap post-POC)
├── runtime/               # orchestrator seam — run_reactive_flow() chains RA-001→003→005+006
└── runbooks/              # runbook definitions used by auto_healer_lite / runbook_executor
agents/                    # Shipped agents (this tree is current; agents/README.md has fallen behind, see above)
├── alert_triage/          # RA-001+002 combined: triage + incident classification
├── auto_ticketing/        # RA-003: ServiceNow ticketing
├── runbook_executor/      # RA-004: runbook execution (REQUIRED HITL)
├── notification_assembler/# RA-005+006 combined: notification routing + war-room assembly
├── log_correlation/       # RA-007: cross-service log correlation (Loki-backed)
├── incident_commander/    # RA-008 (SRE): coordinates Sev-1/2, chains the flow + RCA
├── remediation_recommender/ # PRS-001: candidate fixes
├── auto_healer_lite/      # PRS-002: requests automation.runbook.execute (REQUIRED HITL)
├── knowledge_synthesizer/ # PRS-007: postmortem + KB draft (publish is HITL-gated)
├── resolution_verifier/   # PRS-007 companion: confirms the incident actually resolved
└── rca_agent/             # PRS-008 ★: ranked fix steps + blast radius + rollback
evals/                     # hand-rolled JSON test harness; CI gates pass-rate
demo/
├── otel-demo/             # Helm values for the upstream OpenTelemetry Demo chart (+ Prom rules)
├── scenarios/             # scenario YAMLs — the source of truth for injectable failures
├── failure_injection/     # inject.py — the CLI that flips scenario flags via the seam
├── truth_files/           # ground truth per scenario (cause + expected fix)
├── load/                  # k6 baseline load script
├── audit/                 # chatops.jsonl — notification + approval audit log (gitignored)
├── ui/                    # FastAPI demo server (uv extra: ui) — serves :8765 and mounts the SPAs
├── dashboard/             # main React SPA         → /dashboard/
├── combined-ui/           # RA-001+002 console     → /combined
├── classifier-ui/         # standalone classifier  → /classifier
└── hitl-ui/               # approval console       → /hitl
infra/                     # Rancher Desktop k3s bootstrap (PowerShell + bash) + Prometheus rules
policies/                  # OPA policies (hitl.rego) — enforces Required-HITL actions
scripts/                   # ops helpers (github_bulk runner, seed_oncall, verify_snow_creds.ps1)
tests/                     # repo-level smoke + integration tests
start.ps1 / stop.ps1       # one-command bring-up / tear-down of cluster port-forwards + UI
.github/workflows/         # CI: ruff + pytest + eval gate + opa check
```

> **Why `aiops/` and not `platform/`:** Python's stdlib has a module called `platform`. Using that name as a top-level package shadows it and breaks pytest, uv, and most libraries that introspect the runtime. Don't change it back.

## Context Engineering Layer (in progress, opt-in)

`aiops/context/` fixes a real bug: four agents each independently queried Prometheus/Loki/Jaeger/CMDB/on-call/Git for the same incident, so `oncall.schedule.lookup` fired 4x, `itsm.cmdb.lookup` 3x, and RCA reasoned from a different evidence set than Log Correlation looking at the same failure — no shared ranking, redaction, or token budgeting anywhere. `ContextBuilder.build(request)` runs eight stages — Collect (the only impure one, fanning out over `aiops/context/collectors/`) → Normalize → Correlate → Rank → Enrich → Redact → Budget → Assemble — into a frozen `IncidentContext`. It never raises on the incident path; a failed source degrades to an `UNAVAILABLE` section rather than costing a verdict.

- **Rollout gate:** `AIOPS_CONTEXT_LAYER` (`off` / `shadow` / `on`, default `off`), read **per call**, not at import — this is a deliberate fix for an earlier RA-007 bug where an import-time env read broke `monkeypatch` in tests. While `off`, every agent keeps its existing retrieval untouched.
- **Adapter pattern:** agent-specific projection lives in `agents/<name>/context_adapter.py` (e.g. `rca_agent/context_adapter.py`, `notification_assembler/context_adapter.py`), never in `aiops/context/` itself — reproducing one agent's exact prompt shape is that agent's concern, not the platform's. `tests/test_layering.py` statically (AST-based) enforces that `aiops/context/` never imports `agents/`.
- **Don't bypass it once a capability is behind it.** `aiops/tools/topology/`, `incident_history/`, and `change_context/` are provider-chain seams (their own `register_provider`, not registry capabilities) that the context layer's collectors wrap — call through the collector/context layer for new agent code rather than hitting these providers directly, so evidence stays deduplicated and consistently ranked across agents.

**`aiops/tools/resilience.py`** is the shared `guard()` wrapper (timeout, retry, circuit breaker, cache) that `topology`, `incident_history`, and `change_context` are all built on, because before it existed each provider seam reimplemented its own subset of these four and silently dropped others. Retries happen *before* the breaker trips (a breaker with no retry over-reacts to one dropped packet). Wrap any new remote provider in `guard` rather than hand-rolling timeout/retry/breaker logic — env vars `AIOPS_RESILIENCE_TIMEOUT` / `_RETRIES` / `_BACKOFF` / `_BREAKER` / `_CACHE_TTL` control it.

## When you write code

### Local environment constraints

- **No Docker, no cloud, ~16 GB laptops.** Org policy bans Docker on dev machines; AKS/GKE are post-POC. All cluster work is Rancher Desktop's bundled k3s. Allocate ≥6 GB to its VM (Settings → Virtual Machine); the OTel demo uses ~3.5 GB inside.
- **Rancher Desktop ships a `kuberlr`-wrapped `kubectl` that rejects standard flags from Python `subprocess` calls.** Install a standalone `kubectl` (`winget install --scope user Kubernetes.kubectl`) — `start.ps1` and `demo/failure_injection/inject.py` prefer it via `$LOCALAPPDATA\Programs\kubectl`.
- **Two PowerShell windows.** `start.ps1` runs port-forwards as background jobs in the *current* session; closing that shell kills them. Use `stop.ps1` to tear them down cleanly.
- **flagd flag mutation goes through the seam.** Use `aiops.tools.get_registry().call("feature_flags.set_variant", flag=..., variant=...)` (or `feature_flags.get_variant` / `list_variants` / `reset_all`). Direct `kubectl patch flagd-config` is forbidden — `tests/test_no_kubectl_for_flagd.py` will fail CI for any new caller. Background: ARCH-1 (issue #70, `docs/arch_1_feature_flags_seam_design.md`).
- **PowerShell 5.1's `Get-Content` default encoding is CP1252, not UTF-8.** Tailing `demo/audit/chatops.jsonl` without `-Encoding UTF8` turns em-dashes into `â€"` mojibake. See ONBOARDING.md §11 "Tailing the chatops audit log".
- **`.env.shared` is git-crypt-encrypted** (`.gitattributes`). On a locked clone it reads as binary — that's expected, not corruption. Unlock with `scripts/secrets/unlock.ps1`, then copy it to `.env` for local overrides (`.env` is gitignored). `scripts/secrets/add-teammate.ps1` adds a GPG key. Full workflow in SECRETS.md.
- **`.env` is not auto-loaded.** `uv run` doesn't read it, and neither does `import aiops`. Only entry points that call `aiops._dotenv.load_dotenv()` explicitly get it: `demo/ui/server.py`, `evals/harness.py`, and a couple of `scripts/`. Real env vars win — the file fills in defaults, it never overrides. A new entry point that needs config must call it itself.
- **Importing `demo.ui.server` in a test pollutes `os.environ` for the whole session** (it calls `load_dotenv()` at import). Tests that assert on unset vars must pin them — `tests/conftest.py` has fixtures for this, and `test_slack_user_map_isolation.py` / `test_pagerduty_adapter.py` document the failure mode. Don't add a bare `monkeypatch.delenv` and assume it holds.

### Common commands

```powershell
# Install deps (one-time)
uv sync --extra dev
uv sync --extra ui          # FastAPI demo server (demo/ui/) — required by start.ps1
uv sync --extra embeddings  # sentence-transformers for Alert Triage dedup (optional)
# Other extras: llm-anthropic / llm-openai / llm-ollama / all-llm (pull in that provider's
# SDK — only imported inside aiops/llm/), itsm (ServiceNow client libs)

# Bring up the OTel demo into Rancher Desktop k3s (one-time, ~10 min)
.\infra\bootstrap.ps1                              # bash equivalent: ./infra/bootstrap.sh

# Then either: leave a single port-forward open for the OTel demo proxy...
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080

# ...or: one-command bring-up — checks the cluster, port-forwards
# Prometheus :9090 / Jaeger :16686 / frontend-proxy :8080, builds the
# React dashboard if needed, starts the FastAPI UI on :8765, opens the browser.
.\start.ps1                                        # tear down with: .\stop.ps1

# Run the FastAPI demo server on its own (start.ps1 does this plus port-forwards)
uv run uvicorn demo.ui.server:app --port 8765

# Trigger a failure scenario
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear

# Back to a clean baseline before a rehearsal / demo (flags off, audit log
# truncated). -Hard also wipes verdicts/classifications/tickets from state.db.
# Touches neither the cluster nor start.ps1's port-forwards.
.\reset.ps1
.\reset.ps1 -Hard

# Fire one fixture through the running server and print the routing decision
.\scripts\demo\fire.ps1 -List
.\scripts\demo\fire.ps1 payment_cpu_spike
.\scripts\demo\fire-all.ps1

# Run one agent standalone (alert_triage, auto_healer_lite, knowledge_synthesizer,
# log_correlation, notification_assembler, runbook_executor have __main__.py)
uv run python -m agents.alert_triage

# Run the tests (no cluster needed). testpaths = tests/ aiops/ evals/ — note that
# aiops/ and evals/ carry their own tests; `uv run pytest tests/` is NOT the full suite.
uv run pytest

# Run a single test
uv run pytest tests/test_smoke.py::test_hitl_gate_blocks_required_without_approver

# Skip tests that need a live cluster or a real LLM (markers: integration, llm)
uv run pytest -m "not integration and not llm"

# Run the eval harness for all agents
uv run python -m evals.harness

# Run the eval harness for one agent
uv run python -m evals.harness --agent alert_triage

# CI gate — fails if pass rate drops below threshold
uv run python -m evals.harness --ci --min-pass-rate 0.85

# Lint / format / typecheck. CI runs `ruff format --check`, so a formatted-but-
# uncommitted file reddens the build — run `ruff format .` before pushing.
uv run ruff check .
uv run ruff format .
uv run mypy aiops agents

# Rego gate (its own CI job — an unformatted .rego fails the build)
opa fmt --diff policies/
opa check policies/

# Optional local commit guard mirroring the CI lint gate (once per clone)
uv run pre-commit install

# Rebuild a frontend SPA after editing it (start.ps1 builds demo/dashboard only).
# Each of dashboard / combined-ui / classifier-ui / hitl-ui is its own Vite app;
# the FastAPI server serves the built dist/, so an unbuilt edit will not show up.
cd demo\dashboard; npm install; npm run build    # then: cd ..\..
cd demo\dashboard; npm run dev                   # or hot-reload on Vite's own port

# Tear down the OTel demo (leaves Rancher Desktop's k3s running)
.\infra\teardown.ps1

# Bulk-create GitHub Issues + add to the project board (idempotent — safe to
# re-run; see scripts/github_bulk/README.md for the manifest format).
.\scripts\github_bulk\run.ps1
```

### What CI actually runs

`.github/workflows/ci.yml`, two jobs, on every PR:

1. `uv sync --locked --extra dev --extra ui` → `ruff check .` → `ruff format --check .` → `pytest` → `evals.harness --ci --min-pass-rate 0.85`, all with `AIOPS_LLM_PROVIDER=stub`.
2. `opa fmt --diff policies/` → `opa check policies/`.

Consequences worth knowing before you push:

- **`--locked` means a `pyproject.toml` dependency change without a committed `uv lock` fails CI.** Run `uv lock` and commit `uv.lock` in the same change (#155).
- **CI installs only `dev` + `ui`** — not `embeddings`, not `all-llm`. Anything importing `sentence_transformers` at module scope breaks CI; keep those imports lazy and rule-based fallbacks working.
- **No test may hit a real LLM or a cluster.** `stub` is the CI provider; mark anything else `@pytest.mark.integration` or `@pytest.mark.llm`. Markers are `--strict-markers`, so a typo is an error, not a skip.
- **Every test has a 60 s wall-clock cap** (`timeout_method="thread"`, for Windows). A test that waits on HITL approval or an asyncio event must set its own `@pytest.mark.timeout(N)` rather than blocking the suite (#113).
- `asyncio_mode = "auto"` — async tests need no `@pytest.mark.asyncio`.
- CONTRIBUTING.md adds a human gate CI can't check: eval pass rate may not drop more than 2% vs `main`.

### Configuration surface

Everything is env-var driven and read at the seam, never in agent code. Read from `.env` (loaded explicitly — `uv run` does *not* auto-load it). Every seam degrades to a mock/stub when its vars are absent, so the whole demo runs unconfigured.

| Area | Vars | Default behaviour when unset |
|---|---|---|
| LLM | `AIOPS_LLM_PROVIDER` (`anthropic`/`openai`/`ollama`/`stub`), `AIOPS_LLM_MODEL`, `AIOPS_LLM_MAX_TOKENS_PER_CALL` | stub provider |
| State | `AIOPS_STATE_DB_URL` | `sqlite:///./data/state.db` |
| Runbooks | `AIOPS_RUNBOOKS_DIR` | `data/runbooks` |
| ITSM | `AIOPS_SERVICENOW_INSTANCE_URL` / `_USER` / `_PASSWORD`, `AIOPS_USE_MOCK_ITSM` | mock ITSM provider |
| Observability | `AIOPS_PROMETHEUS_URL`, `AIOPS_LOKI_URL`, `AIOPS_JAEGER_URL`, `AIOPS_GRAFANA_URL` / `_API_KEY` | provider registered but calls fail soft |
| ChatOps | `AIOPS_SLACK_WEBHOOK_URL`, `AIOPS_SLACK_BOT_TOKEN`, `AIOPS_SLACK_USER_MAP_JSON`, `AIOPS_TEAMS_WEBHOOK_URL`, `AIOPS_TEAMS_DM_WEBHOOK_URL`, `AIOPS_PAGERDUTY_INTEGRATION_KEY`, `AIOPS_JITSI_BASE` | JSON-file + WebSocket sinks only |
| War-room meeting | `AIOPS_TEAMS_MEETING_WEBHOOK_URL`, `AIOPS_TEAMS_MEETING_FLOW_ID`, `AIOPS_WAR_ROOM_MAX_ATTENDEES`, `AIOPS_TEAMS_MEETING_MINUTES` | Jitsi room (no calendar invite) |
| Runbook links | `AIOPS_RUNBOOK_PUBLISHER_URL`, `AIOPS_RUNBOOK_PUBLISHER_FLOW_ID`, `AIOPS_RUNBOOK_LINKS_PATH` | Runbook name as plain text, no link |
| HITL | `AIOPS_HITL_DEFAULT`, `AIOPS_HITL_APPROVAL_TIMEOUT` | Required-level actions deny without an approver |
| Context layer | `AIOPS_CONTEXT_LAYER` (`off`/`shadow`/`on`), `AIOPS_CONTEXT_WORKERS` | `off` — agents keep their pre-existing per-agent retrieval |
| Resilience (`aiops/tools/resilience.py`) | `AIOPS_RESILIENCE_TIMEOUT`, `_RETRIES`, `_BACKOFF`, `_BREAKER`, `_CACHE_TTL` | 3s timeout / 2 retries / 0.2s backoff / 30s breaker / 60s cache |
| Incident history | `AIOPS_INCIDENT_HISTORY_PROVIDERS` | `mock` provider only |

The remote seams (Loki, Jaeger) have **circuit breakers** — `AIOPS_*_CIRCUIT_OPEN_SECONDS` — so a down backend degrades the agent rather than hanging the request. Preserve that when adding a new remote provider.

### Constraints code review will catch

Each of these has a test that fails CI, so they are worth knowing before you write the code:

- **No direct vendor SDK imports.** `import anthropic` / `import openai` outside `aiops/llm/` → `test_no_direct_llm_sdk_imports_outside_aiops_llm`.
- **No direct flagd mutation via kubectl.** Use the `feature_flags.set_variant` capability → `test_no_kubectl_for_flagd_outside_seam` (ARCH-1, #70).
- **No `@app.on_event` in `demo/ui/`.** Use lifespan handlers → `test_no_fastapi_on_event_in_demo_ui` (DEMO-15, #67).
- **No HITL checks inside agent logic.** HITL is enforced at the *registry boundary* — just call `get_registry().call(capability, ...)` and a Required-level capability returns `ok=False` when no approver is wired. Agents never gate-check themselves.
- **Every RCA fix step must set `requires_hitl=True`** → `test_rca_fix_step_rejects_requires_hitl_false`.
- **Every new failure scenario ships with a truth file** → `test_every_scenario_has_a_truth_file`. Scenario YAMLs are also schema-validated and must have unique ids, and their UI descriptors must match the server's (`tests/test_scenarios_yaml.py`).
- **Every new agent ships with `agents/<dir>/evals/golden.json`.** The eval harness discovers it automatically.
- **`aiops/` never imports `agents/` (or `demo/`).** The dependency arrow is `demo/ → agents/ → aiops/`, checked by AST (not substring matching) → `tests/test_layering.py`. The one sanctioned exception is `aiops/runtime/orchestrator.py`, which by design sits above the agents it chains.
- **Context-layer parity while it migrates.** `tests/test_context_shadow.py` requires zero mismatches between shadow-mode context output and legacy retrieval; `test_rca_context_adapter.py` / `test_notification_assembler_context_adapter.py` gate on byte-identical output between the adapter and the pre-migration prompt strings; `test_retrieval_call_sites.py` is a ratchet that fails if the count of duplicated per-agent retrieval call sites grows instead of shrinks.

### The seams to use, not bypass

| Want to... | Use... | Don't... |
|---|---|---|
| Call an LLM | `aiops.llm.complete` / `acomplete` | `import anthropic` / `import openai` |
| Call ServiceNow / Slack / flagd / Prom / Jaeger | `aiops.tools.get_registry().call(capability, ...)` | `httpx.post(...)` / `kubectl patch` |
| Gate a destructive action | `aiops.policy.get_gate().enforce(action, ctx)` | `if user_confirmed:` inside the agent |
| Persist verdicts / classifications / state | `aiops.state.repository.save_*` / `load_*` | raw SQLAlchemy / SQL |
| Chain the Reactive-Active flow | `aiops.runtime.orchestrator.run_reactive_flow(alert)` | re-wiring the agent calls inline |
| Gather incident evidence for an agent | `aiops.context.build(request)` behind `AIOPS_CONTEXT_LAYER`, projected via an `agents/<name>/context_adapter.py` | each agent independently re-querying Prometheus/Loki/CMDB/on-call/Git |
| Add timeout/retry/breaker/cache to a new provider | wrap the call in `aiops.tools.resilience.guard(...)` | hand-rolling a subset of the four and forgetting one |

#### HITL levels live in two files that nothing forces to agree

`policies/hitl.rego` is **reference-only today**; the runtime authority is `DEFAULT_LEVELS` in `aiops/policy/gate.py` (ADR-005 — wiring OPA in as the runtime check is a Phase 2 step). No test compares them, so they silently drift. **When you change an action's autonomy level, edit both**, and match the catalog row.

#### HITL levels live in two files that nothing forces to agree

`policies/hitl.rego` is **reference-only today**; the runtime authority is `DEFAULT_LEVELS` in `aiops/policy/gate.py` (ADR-005 — wiring OPA in as the runtime check is a Phase 2 step). No test compares them, so they silently drift. **When you change an action's autonomy level, edit both**, and match the catalog row.

#### HITL levels live in two files that nothing forces to agree

`policies/hitl.rego` is **reference-only today**; the runtime authority is `DEFAULT_LEVELS` in `aiops/policy/gate.py` (ADR-005 — wiring OPA in as the runtime check is a Phase 2 step). No test compares them, so they silently drift. **When you change an action's autonomy level, edit both**, and match the catalog row.

#### The orchestrator seam

`run_reactive_flow(alert)` is the **single** entry point for the RA-001 → RA-002 → RA-003 → RA-005+006 chain. It triages, classifies, tickets, notifies, and persists each step with FK guards; notification failure is caught and non-fatal (`routing=None`). It returns a `ReactiveFlowResult`, and `.to_api_dict()` reproduces the legacy `POST /api/triage` response body verbatim — that shape is a public contract for the dashboard, the SPAs, and the tests, so don't change it casually.

Four callers share it: the `/api/triage` route, the live-alert sweep, the auto-triage loop, and RA-008 Incident Commander. If you need the chain, call it — don't re-wire the agents inline. Note the dependency direction: agents never import `aiops.runtime`.

#### Registry capability namespaces

Tools register under a dotted `capability` name and are dispatched by `get_registry().call(capability, ...)`; multiple providers can serve one capability (e.g. `mock.*` vs `snow.*`, selected by whether real credentials are present). Namespaces currently in use:

`itsm.incident.*` · `itsm.cmdb.*` · `itsm.ticket.close` · `observability.metrics.*` · `observability.logs.query` · `observability.traces.*` · `observability.events.query` · `feature_flags.*` · `oncall.schedule.lookup` · `incident.resolvers.lookup` · `notify.send` · `chatops.war_room.create` · `knowledge.publish` · `rca.fix_step.execute` · `automation.runbook.{execute,simulate,apply}` · `scm.file.read` · `scm.repo.tree` · `scm.commit.history` · `scm.diff` · `scm.pr.list`

Register a new one with the `@tool(name=, capability=, provider=)` decorator in `aiops/tools/`. Several subpackages are deliberately *not* registries: `aiops/tools/alerts/` holds pure webhook-payload → canonical `Alert` adapters (Alertmanager, CloudWatch, Datadog, Prometheus), most of `aiops/tools/chatops/` is the client/adapter seam rather than a registered capability, and `topology/` / `incident_history/` / `change_context/` are provider-chain seams (own `register_provider`, consumed by `aiops/context/collectors/`) rather than dispatch-by-capability.
