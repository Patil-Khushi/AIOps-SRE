# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

The repo has the **Phase 0 platform seams** (`aiops/llm`, `aiops/tools`, `aiops/policy`, `aiops/state`, `aiops/runtime`), the demo bootstrap (`infra/`, `demo/ecommerce`, `demo/load`), the eval harness, OPA policy, and CI in place. **Phase 1 is shipped and Phase 2 is mostly shipped**: merged agents under `agents/` — `alert_triage/` (RA-001+002 combined: triage + classification), `auto_ticketing/` (RA-003), `notification_assembler/` (RA-005+006 combined: routing + war-room), plus `incident_commander/` (RA-008), `rca_agent/` (PRS-008), `knowledge_synthesizer/` (PRS-007), `log_correlation/` (RA-007), `auto_healer_lite/` (HITL demo), and post-POC stubs. The chatops seam lives under `aiops/tools/chatops/` (JSON-file + WebSocket + Slack + Teams adapters), the combined triage/classifier UI at `/combined`, and the React dashboard at `/dashboard` (which now carries the Incident Command Center with its blast-radius graph, and the RCA chat dock with live SSE pipeline progress — see "The demo web tier"). The `docs/` design files remain the authoritative source for agent contracts and architecture.

### The system under test is `demo/ecommerce/`, not the OpenTelemetry Demo

**The OTel Demo has been removed from the repo.** The SUT is now a purpose-built
e-commerce app in `demo/ecommerce/` (React frontend → order-service → user-service /
payment-service → MySQL / Postgres / Redis / mock-payment-gateway, all FastAPI + OTel
instrumented). `demo/otel-demo/`, `demo/scenarios/`, and `demo/failure_injection/` **no
longer exist** — much of the older documentation in this repo still refers to them.

What moved where:

| Old (gone) | Now |
|---|---|
| `demo/otel-demo/` (Helm values for the upstream chart) | `demo/ecommerce/k8s/*.yaml` (plain manifests) + `k8s/build-images.ps1` |
| `demo/failure_injection/inject.py` (flagd flag flipping) | `demo/ecommerce/failure_injection/` (env-var / scale / real-chaos toolkit) |
| `demo/scenarios/` | `demo/ecommerce/scenarios/*.yaml` |
| truth files as YAML in `demo/truth_files/` | `demo/ecommerce/truth_files/*.json` |
| `otel-demo` namespace for everything | `ecommerce` (app) + `observability` (Prom/Grafana/Jaeger) + `otel-demo` (**Loki only** — it did not move) |

Three consequences worth internalising before you touch anything:

- **flagd is gone.** There is no feature-flag daemon in the cluster. A fault is an env var
  on a Deployment, a StatefulSet scaled to zero, or real in-pod chaos. `aiops/tools/feature_flags/`
  survives as an unused seam, and `tests/test_no_kubectl_for_flagd.py` has been **deleted** —
  so ADR-001 and the "no kubectl patch for flagd" constraint below are now historical, not
  enforced. Clearing a fault goes through the new `automation.fault.clear` capability
  (`demo/ui/fault_clear.py`), which is what the RCA apply-fix loop executes after HITL approval.
- **`demo/truth_files/` still exists on disk but is the old OTel-demo corpus.** The evaluated
  truth files are the JSON ones under `demo/ecommerce/truth_files/`.
- **17 failure keys, but only 12 scenarios/truth files.** The registry
  (`failure_injection.FAILURES`) carries five infrastructure-only failures — `packet_loss`,
  `memory_exhaust`, `disk_full`, `dns_failure`, `pool_exhaustion` — that have no scenario YAML
  and therefore no truth file and no eval coverage. They are injectable by hand but invisible
  to the dashboard catalog and to `reset_all`, which iterates scenarios, not failure keys.
  Adding a scenario YAML for one of these means adding its truth file in the same change
  (`test_every_scenario_has_a_truth_file`).
- **`infra/bootstrap.ps1` is stale and will fail.** It still `helm upgrade`s the upstream
  OTel Demo chart from `demo/otel-demo/values.yaml`, which no longer exists. Bring the stack
  up with `infra/observability/install.ps1` (Prometheus/Grafana/Jaeger) plus the ecommerce
  manifests — see "Common commands". `start.ps1` and `reset.ps1` *are* current.

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
| 001 | flagd mutation goes through the `feature_flags.*` seam, never `kubectl patch` | Accepted, but **moot** — flagd left with the OTel Demo; its enforcing test is deleted |
| 002 | **No agent framework for the POC.** Agents are plain functions; `aiops/runtime/orchestrator.py` is the "framework". Do not add LangGraph/AutoGen/CrewAI. | Accepted |
| 003 | Anthropic (Azure AI Foundry Claude) is the default; OpenAI the swap-in; Ollama the offline fallback; `stub` for tests/CI | Accepted |
| 004 | One `ApprovalRequest` in `aiops/policy/approvals.py` fans out to chat + web surfaces; either can resolve it | Accepted |
| 005 | OPA is the chosen engine but **reference-only today** — see the note under "The seams to use, not bypass" | Accepted (direction) |
| 006 | No vector store until an agent needs persistent semantic retrieval; then pgvector | Proposed |
| 007 | Truth files are YAML in the repo, not DB rows | Accepted |

Changing an Accepted decision means writing a *new* ADR that supersedes the old one — not editing the old file.

### Which root document answers what

The repo root carries a lot of markdown. Rather than reading all of it: `README.md` (quick start; its status table is stale — trust this file's roadmap instead) · `ONBOARDING.md` (laptop setup, gotchas, tailing the audit log) · `RUNNING.md` (day-to-day run loop) · `CONTRIBUTING.md` (branching, PR gates) · `PRD.md` / `KPI.md` / `EVAL_METHODOLOGY.md` (product scope, metrics, how agents are scored) · `DEMO_PLAN.md` / `DEMO_SCRIPT.md` / `DEMO_SHOWCASE.md` (rehearsed demo narrative) · `PROJECT_STATE.md` / `SESSION_NOTES.md` (point-in-time status — may be stale, verify before relying on it) · `SECRETS.md` (git-crypt workflow) · `THREAT_MODEL.md` / `RISK_REGISTER.md` · `SOLUTION_BRIEF.md` (the long-form external-facing writeup).

**All of these root markdown files predate the OTel-Demo → `demo/ecommerce/` migration.** Wherever one names `otel-demo`, flagd, a feature flag, `demo/failure_injection/`, or `frontend-proxy:8080`, it is describing a stack that is no longer deployed. The current bring-up and fault workflow is the one in "Common commands" below; `demo/ecommerce/README.md` and `demo/ecommerce/k8s/README.md` are the accurate SUT-level docs.

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
- **Demo on synthetic / open-source / demo-app data**, not real customer data. Default demo target: **`demo/ecommerce/`**, the in-repo SUT, on Kubernetes (Rancher Desktop's bundled k3s locally; AKS/GKE deferred until post-POC). It replaced the OpenTelemetry Demo (Astronomy Shop) because half the scenarios needed real `OOMKilled` / `CrashLoopBackOff` pod states and kubectl-shaped remediation the agents could actually perform. Failure injection via `demo/ecommerce/failure_injection/`; load via k6 and the in-cluster `loadgen` Deployment.
- **Scope creep is the silent killer.** "While we're at it" lands on the post-POC backlog, not the current sprint.

## Reference POC stack (defaults from the onboarding guide)

When picking a tool for a new component, default to the choice in this table unless there's a reason not to. The stack is intentionally FOSS-heavy so the demo runs anywhere.

| Concern | Default | Notes |
|---|---|---|
| Demo app / SUT | **`demo/ecommerce/`** (in-repo FastAPI + React app) | Replaced the OpenTelemetry Demo. Own services, own datastores, own failure toolkit. |
| Cluster | **Rancher Desktop's bundled k3s** (Windows/macOS) | Org policy bans Docker on dev machines, so kind/k3d/Docker Desktop are out. Cloud (AKS/GKE) deferred. |
| Metrics / Logs / Traces | Prometheus / Loki / Tempo + Grafana | All FOSS, all integrate. |
| Instrumentation | OpenTelemetry SDKs + Collector | Vendor-neutral. |
| Tickets | ServiceNow PDI (primary) + Jira free tier (secondary) | Two integrations prove vendor-neutrality. |
| Alerting / on-call | PagerDuty developer account | Real on-call workflow. |
| Chat ops | Slack or Microsoft Teams | Match what the org uses. |
| Failure injection | `demo/ecommerce/failure_injection/` | Three modes (`FI_MODE`): `application` (env vars / scale-to-zero), `infrastructure` (tc, stress-ng, dd, DNS), `hybrid` (default, both). Two backends (`FI_BACKEND`): `k8s` (default) / `docker`. |
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
- **Phase 2 — RCA backbone (W6–8):** ✅ *Mostly shipped.* RCA Agent v1, HITL UI, Incident Commander v1, Knowledge Synthesizer v0 (postmortem drafting + HITL-gated KB publish), audit trail. *Out of scope: predictive, prescriptive autonomy, chaos.* The RCA Agent has since been rebuilt well past v1 into a deterministic evidence-ranking investigator with bounded historical memory and closed-loop learning — see "RCA Agent" below and `docs/rca_upgrade_checkpoint.md` for the full phase-by-phase record.
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
│   ├── feature_flags/     # DEAD: flagd adapter, kept after flagd left with the OTel Demo
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
└── rca_agent/             # PRS-008 ★: deterministic evidence-ranking investigation,
                          # bounded historical memory, ranked fix steps — see "RCA Agent" below
evals/                     # hand-rolled JSON test harness; CI gates pass-rate
demo/
├── ecommerce/             # ★ the system under test (replaced the OpenTelemetry Demo)
│   ├── frontend/          #   React storefront                      → NodePort 30080
│   ├── user-service/      #   FastAPI + MySQL                       → NodePort 30081
│   ├── order-service/     #   FastAPI + Postgres                    → NodePort 30082
│   ├── payment-service/   #   FastAPI + Redis (ClusterIP)
│   ├── mock-payment-gateway/ # simulated external processor (ClusterIP)
│   ├── k8s/               #   manifests + build-images.ps1 — the real deployment path
│   ├── failure_injection/ #   the fault toolkit: 17 failures, CLI, k8s/docker backends
│   ├── scenarios/         #   scenario YAMLs — schema-validated, one per failure key
│   ├── truth_files/       #   ★ ground truth (JSON) — the corpus the evals actually read
│   ├── observability/     #   NodePort bridge + promtail config
│   └── docker-compose.yml #   still fine for plain app dev; k8s is what AIOps work uses
├── truth_files/           # LEGACY OTel-demo truth files (YAML) — superseded by ecommerce/
├── load/                  # k6 baseline load script
├── audit/                 # chatops.jsonl — notification + approval audit log (gitignored)
├── providers.py           # register_demo_providers() — binds demo-side tool providers
├── ui/                    # FastAPI demo server (uv extra: ui) — serves :8765 and mounts the
│                          #   SPAs; ~52 routes + 7 sibling router/hub modules. See
│                          #   "The demo web tier" below before adding a route.
│   └── fault_clear.py     #   automation.fault.clear provider (the post-HITL fix executor)
├── dashboard/             # main React SPA         → /dashboard/ (18 pages; icc/ + chat/ trees)
├── combined-ui/           # RA-001+002 console     → /combined
├── classifier-ui/         # standalone classifier  → /classifier
└── hitl-ui/               # approval console       → /hitl
infra/
├── observability/         # ★ Prometheus + Grafana + Jaeger install (install.ps1) + dashboards
├── loki-values.yaml       # Loki — still installed into the `otel-demo` namespace
├── bootstrap.ps1/.sh      # STALE: still helm-installs the removed OTel Demo chart
└── port-forward.ps1 / teardown.ps1
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

## RCA Agent — deterministic investigation before the model (`agents/rca_agent/`)

PRS-008 is not a single LLM call over telemetry. `agent.py::analyze()` runs an entirely
deterministic pipeline first (`investigation/pipeline.py`, no LLM involved): gather
evidence, generate candidate failure classes from a fixed catalog
(`investigation/catalog.py` — generic SRE shapes like `dependency_unavailable`,
`resource_saturation_cpu`; never a hardcoded fault name), classify every observation as
supporting/contradicting/checked-absent/gap per hypothesis, and score each additively
(`investigation/scoring.py`). **The platform's score is the verdict's authoritative
confidence; the one LLM call that follows only explains the winning hypothesis** —
`_authoritative_confidence` downgrades the verdict to `UNCERTAIN` if the model's prose
doesn't actually describe the hypothesis that was scored. When the LLM is unavailable
(stub, timeout, unparseable JSON), `_fallback_verdict` builds a full verdict from the
investigation stages alone, no model involved at all.

Bounded historical memory (`investigation/memory.py`) can nudge a score, but only from
**verified outcomes** — never from the truth-file corpus, which is also the evaluation's
answer key. Only providers in `memory.OUTCOME_BACKED_PROVIDERS` may supply a prior
(`aiops/tools/incident_history/providers/outcomes.py` is the one shipped); a prior's
contribution is capped below every current-evidence scoring term and cancelled outright
when current evidence contradicts the hypothesis. Outcomes are written only after
`resolution_verifier` confirms recovery, via `agents/rca_agent/learning.py` — a module
restricted (by an AST check on its own source, not just a docstring) to writing outcome
rows and nothing else: no prompt edits, no source edits, no tool or policy registration.

`prompts.py` versions the system prompt by symbol (`SYSTEM_PROMPT_V1` … `V7`); each
version after V3 is built by explicit `.replace()` calls with import-time assertions, so a
half-applied edit fails the interpreter rather than shipping a prompt that silently still
describes the old behavior. The current version carries **no injection-mechanism detail**
(env var names, chaos implementation, alert→answer tables) — the executable action
vocabulary is resolved at request time from the platform's action registry
(`agent._action_vocabulary`), never hardcoded into the prompt.

Two eval tiers measure this agent differently. `evals/harness.py` (CI, no cluster, no real
LLM) checks contract properties against `agents/rca_agent/evals/golden.json` and
truth-file `exercises` blocks. `evals/rca_eval.py` (human-run, real LLM) measures accuracy
against simulated telemetry, with `--mode baseline/no-evidence/cold-start/learning/
poisoned-memory/ablation`. `evals/rca_synthetic.py` projects each truth file's declared
symptoms into a synthetic `IncidentContext` (Prometheus/Loki-shaped, matching the exact
PromQL keys `evidence.required_promql_queries()` asks for) so `rca_eval.py` runs
offline/reproducibly instead of against a live cluster; `evals/rca_metrics.py` then scores
one verdict across many independent axes (root-cause/category/service/remediation
accuracy, confidence calibration, fabricated-citation rate, evidence grounding, memory
influence, …) rather than a single pass/fail number, and reports `timeline_accuracy` /
`blast_radius_accuracy` as `not_measurable_yet` rather than as zero where no ground truth
exists yet. Truth files (`demo/ecommerce/truth_files/*.json`) must never
reach the agent directly — `evals/rca_truth.py::assert_blind` enforces this on the
production path. `docs/rca_upgrade_checkpoint.md` is the full phase-by-phase record of
this design (locked decisions, defects found and fixed, measured before/after deltas);
read it before changing anything under `agents/rca_agent/`.

### The chat layer is read-only over a frozen investigation

`agents/rca_agent/chat.py` is **not a second reasoning path**. It explains, cites, and
quantifies what the pipeline already computed; it cannot move a number, execute anything,
pull new memory, or start a fresh investigation — and that is structural, not prompted:
`ChatAnswer` has no field to put a new verdict in, and `tests/test_rca_chat_boundary.py`
AST-checks the chat surface for any `aiops.tools.*` import beyond one allowed read-only RAG
accessor. `answer()` grounds the model on a rendered snapshot of the `Investigation` (the
"grounding pack") and validates the reply against that same snapshot — unknown evidence ids
are dropped and counted as `fabricated_citations`, and a stated confidence that disagrees
with the platform's is flagged in `warnings`, never silently edited.
`_deterministic_answer()` is the no-LLM path (keyword-intent routing over a closed set,
rendering `Investigation` sections directly) — the chat analogue of `_fallback_verdict`.

That boundary is why sharing an answer to Teams lives in its own module
(`demo/ui/rca_share_routes.py`) rather than beside the chat routes: it needs the chatops
seam, which the AST check forbids inside the chat surface.

`agents/rca_agent/progress.py` is the progress seam — a pure module (no asyncio, no FastAPI,
no `aiops` import, so the agent stays individually runnable). `analyze()` emits `StageEvent`s
at the same boundaries it already records in `decision_trace`; omitting `progress` reproduces
`analyze()`'s output byte-for-byte. If a second agent ever wants the channel, move it to
`aiops/progress/` unchanged and leave `RcaStage` behind.

## The demo web tier (`demo/ui/` + `demo/dashboard/`)

`demo/ui/server.py` is a 3k-line FastAPI app with ~52 routes, plus seven sibling modules it
composes. **Add a new route family as a sibling module with its own `APIRouter` and a
one-line `app.include_router` in `server.py`** — never by importing `demo.ui.server` from the
sibling (circular import, and it couples that surface's failure mode to the core pipeline).
`knowledge_routes.py` set the pattern; `rca_chat_routes.py` and `rca_share_routes.py` follow
it. Shared state that both need (`rca_sessions.py`) goes in a third module both import.

| Module | Owns |
|---|---|
| `server.py` | triage / RCA / correlate / incident-commander / remediation / execute routes, fixtures + live alerts, approvals, classifier + combined consoles, war room, scenarios (inject/reset/reset-all), topology, pods, and the four SPA mounts |
| `knowledge_routes.py` | `/api/synthesize`, `/api/kb/*` (incl. HITL-gated publish + outcome poll), synthesizer/verifier status |
| `rca_chat_routes.py` | `/api/rca/chat` (POST turn, GET transcript, GET by-incident, DELETE) |
| `rca_share_routes.py` | `/api/rca/chat/share-teams` |
| `rca_sessions.py` | the in-memory RCA chat session store (LRU + TTL) |
| `rca_progress.py` | `/api/rca/stream/{run_id}` — the per-run SSE progress hub |
| `chatops_ws.py` / `_alert_hub.py` | `/ws/chatops` and `/ws/alerts` |
| `fault_clear.py` | the `automation.fault.clear` provider (post-HITL fix executor) |

Three real-time transports, deliberately different:

- **`/ws/chatops`** — one long-lived global feed, with a history ring.
- **`/ws/alerts`** — a single broadcaster task polls `live_alerts()` every
  `AIOPS_ALERT_BROADCAST_INTERVAL` and fans one payload out to all tabs, so cost stays flat
  in the number of open browsers. Don't convert it to per-client polling.
- **`/api/rca/stream/{run_id}`** — many short-lived per-run channels, **SSE not WebSocket**:
  the stream terminates naturally at the terminal stage (finite, timeout-free CI tests),
  needs no new dependency (a new `pyproject.toml` entry without a committed `uv.lock`
  reddens CI, #155), and `EventSource` gives reconnect + `Last-Event-ID` resume for free.
  Every method on the hub is keyed by `run_id` — two concurrent RCA runs must never
  cross-talk. Pass a `run_id` on the RCA request to get a sink; omit it and it costs nothing.

**In-memory over `aiops.state` is the established choice here, not an oversight.**
`_HITL_OUTCOMES` (`server.py`), `_PUBLISH_OUTCOMES` (`knowledge_routes.py`), the chatops
history ring, and the RCA session store are four bounded process-global stores; a fifth is
idiomatic, a new SQLModel table is POC scope creep. Chat transcripts are lost on restart —
grounding is not, because `rca_endpoint` calls `save_rca_result()` whenever a request carries
an `incident_id`, so `GET /api/rca/chat/by-incident/{id}` rehydrates the verdict.

The chat surface is unauthenticated like the rest of this POC, but it is the only
LLM-*cost* surface, so three caps apply regardless: per-session turn cap, message-length cap,
and one in-flight turn per `run_id`.

### Dashboard structure

`demo/dashboard/` is the main SPA (18 pages, React Router). Route groups: `/` landing,
`/agents/*` browse, and `/console/*` the operator console (`alerts`, `rca`, `incidents`,
`incidents/:incidentId`, `log-correlation`, `incident-commander`, `approvals`,
`notifications`, `reasoning`, `knowledge`, `topology`, `health`). Retired routes redirect
rather than 404 — `war-room` → `notifications`, `remediation-recommender` and `auto-healer`
→ `/console/rca`. Keep that pattern when a page merges away.

Two feature areas carry their own component trees rather than living in a page file:

- **Incident Command Center** (`components/icc/`) — `IccRoot` + incident table/toolbar and an
  `IncidentWorkspace` whose tabs (`icc/tabs/`) are Evidence, Hypotheses, Timeline, Changes,
  History, Verification, and **Blast Radius** (`BlastRadiusGraph.tsx`). The page files
  (`IncidentCommandCenter.tsx`, `RcaConsole.tsx`) are thin.
- **RCA chat dock** (`components/chat/`) — `ChatDockProvider` + `RcaChatDock`, with
  `ProgressStageList` rendering the SSE stage events above.

Each of `dashboard` / `combined-ui` / `classifier-ui` / `hitl-ui` is its own Vite app and the
server serves the built `dist/` — an unbuilt edit does not show up. `start.ps1` builds only
`dashboard`.

## When you write code

### Local environment constraints

- **No Docker, no cloud, ~16 GB laptops.** Org policy bans Docker on dev machines; AKS/GKE are post-POC. All cluster work is Rancher Desktop's bundled k3s. Allocate ≥6 GB to its VM (Settings → Virtual Machine); the ecommerce SUT plus the observability stack use most of it.
- **Rancher Desktop ships a `kuberlr`-wrapped `kubectl` that rejects standard flags from Python `subprocess` calls.** Install a standalone `kubectl` (`winget install --scope user Kubernetes.kubectl`) — `start.ps1` and `reset.ps1` prefer it via `$LOCALAPPDATA\Programs\kubectl`, while `demo/ecommerce/failure_injection/_kubectl.py` *probes* candidates instead (real kubectl emits `clientVersion` JSON; the kuberlr wrapper errors) — it is a deliberate copy of the old `inject.py` logic, kept duplicated so the package stays runnable standalone inside `demo/ecommerce/`. Keep the two in sync.
- **Two PowerShell windows.** `start.ps1` runs port-forwards as background jobs in the *current* session; closing that shell kills them. Use `stop.ps1` to tear them down cleanly.
- **Demo-side tool providers only exist if you register them.** The `@tool` decorator fires as an import side effect, so a process that never imports the provider module has no provider for that capability. Every demo entry point calls `demo.providers.register_demo_providers()` — a new one must too, or `automation.fault.clear` silently has no implementation.
- **Faults are k8s objects, not flags.** `aiops/tools/feature_flags/` and `docs/arch_1_feature_flags_seam_design.md` describe flagd, which is gone; treat both as historical. An app-layer fault is `kubectl set env` on a Deployment (recovery writes explicit defaults back rather than deleting the key, so the manifest's `configMapKeyRef` mapping survives an inject/recover cycle — see the `_FAULT_DEFAULTS` comment in `_k8s.py`, and keep those defaults matching `demo/ecommerce/k8s/01-config.yaml`). An infra-layer fault is real in-pod chaos and can report `unavailable` when the tool is missing from the image — `order_service.packet_loss` needs `tc`, which is not in the service Dockerfiles, so it neither injects nor recovers today.
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

# One-time cluster bring-up. NOT infra\bootstrap.ps1 — that still targets the
# removed OTel Demo chart and will fail.
.\infra\observability\install.ps1                  # Prometheus + Grafana + Jaeger
cd demo\ecommerce; .\k8s\build-images.ps1          # build the 5 SUT images
kubectl apply -f k8s\00-namespace.yaml
kubectl apply -f k8s\01-config.yaml
kubectl apply -f k8s\10-datastores.yaml
kubectl -n ecommerce rollout status statefulset/mysql --timeout=180s
kubectl apply -f k8s\20-app.yaml; kubectl apply -f k8s\30-frontend.yaml
# Datastores are applied and awaited FIRST: each service builds its SQLAlchemy
# engine at import time, so starting them together crashloops the app pods
# until the databases happen to win the race. See demo/ecommerce/k8s/README.md.

# Day-to-day bring-up — checks the cluster, port-forwards Prometheus :9090 /
# Jaeger :16686 / Grafana :3001 / Loki :3100, recovers any injected failure,
# builds the React dashboard if needed, starts the FastAPI UI on :8765.
# The SUT itself needs no port-forward: it is on NodePorts 30080-30083.
.\start.ps1                                        # tear down with: .\stop.ps1

# Run the FastAPI demo server on its own (start.ps1 does this plus port-forwards)
uv run uvicorn demo.ui.server:app --port 8765

# Failure injection (17 failures across the 3 services). Run from demo/ecommerce/,
# where the package is importable as a top-level module.
cd demo\ecommerce
python -m failure_injection list --show-layers
python -m failure_injection signals order_service.http_500   # L1 / L2 / expected RCA
python -m failure_injection inject order_service.http_500 --load 60
python -m failure_injection recover order_service.http_500
# FI_MODE=application|infrastructure|hybrid (default hybrid)
# FI_BACKEND=k8s (default)|docker · FI_NAMESPACE=ecommerce · FI_DRY_RUN=1

# Recover EVERY injected failure at once. The CLI has no `recover --all`, and
# reset.ps1's default makes no cluster contact — this is the one that does it.
uv run python -c "from demo.ui import scenario_provider as sp; print(sp.reset_all(sp.load()))"
.\start.ps1 -Fresh                                 # same thing, plus state.db + audit log

# Back to a clean baseline before a rehearsal / demo. Layered by cost:
.\reset.ps1              # agent INPUTS: audit log truncated. Seconds, no cluster contact
.\reset.ps1 -Hard        # + agent OUTPUTS: verdicts/classifications/tickets in state.db
.\reset.ps1 -Data        # + the SUT's own orders/users/payments (bounces the 3 services,
                         #   because Prometheus counters live in-process and a table
                         #   truncate alone does not move them)
.\reset.ps1 -Telemetry   # + metric/log/trace HISTORY (deletes + re-provisions PVCs, ~3 min).
                         #   This is the one to run before recording a demo.
.\reset.ps1 -All
# Note: -Data and -Telemetry DO touch the cluster and will drop port-forwards to
# any replaced pod — re-run start.ps1 if a UI stops responding afterwards.
# Prometheus alert rules use rolling [2m] windows, so an alert from a cleared
# fault keeps firing ~2 min. That is lag, not a failed reset.

# Fire one fixture through the running server and print the routing decision
.\scripts\demo\fire.ps1 -List
.\scripts\demo\fire.ps1 payment_cpu_spike
.\scripts\demo\fire-all.ps1

# Run one agent standalone (alert_triage, auto_healer_lite, knowledge_synthesizer,
# log_correlation, notification_assembler, runbook_executor have __main__.py)
uv run python -m agents.alert_triage

# Run the tests (no cluster needed). testpaths = tests/ aiops/ evals/, though
# aiops/ and evals/ carry no test files today — everything lives in tests/.
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

# Tear down (leaves Rancher Desktop's k3s running). Like bootstrap.ps1, this
# still targets the removed otel-demo release — check it before trusting it.
.\infra\teardown.ps1
kubectl delete namespace ecommerce                 # the SUT itself

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
| War-room meeting | `AIOPS_TEAMS_MEETING_WEBHOOK_URL`, `AIOPS_TEAMS_MEETING_FLOW_ID`, `AIOPS_POWER_AUTOMATE_ENV`, `AIOPS_WAR_ROOM_MAX_ATTENDEES`, `AIOPS_TEAMS_MEETING_MINUTES`, `AIOPS_TEAMS_MEETING_WAIT_SECONDS`, `AIOPS_TEAMS_MEETING_TIMEOUT`, `AIOPS_AZ_PATH` | Jitsi room (no calendar invite). **Gotcha:** even with all vars set, `teams_meeting.py` reads the real join URL back from the Power Automate flow's run history via an `az account get-access-token` call — if the `az` CLI isn't installed/logged in on the host, the meeting is still created (invites go out) but the code silently keeps the Jitsi link instead of surfacing the real `teams.microsoft.com/l/meetup-join/...` URL. Check `az account show` first when a war room shows Jitsi despite Teams config looking complete. |
| Runbook links | `AIOPS_RUNBOOK_PUBLISHER_URL`, `AIOPS_RUNBOOK_PUBLISHER_FLOW_ID`, `AIOPS_RUNBOOK_LINKS_PATH` | Runbook name as plain text, no link |
| HITL | `AIOPS_HITL_DEFAULT`, `AIOPS_HITL_APPROVAL_TIMEOUT` | Required-level actions deny without an approver |
| Context layer | `AIOPS_CONTEXT_LAYER` (`off`/`shadow`/`on`), `AIOPS_CONTEXT_WORKERS` | `off` — agents keep their pre-existing per-agent retrieval |
| Resilience (`aiops/tools/resilience.py`) | `AIOPS_RESILIENCE_TIMEOUT`, `_RETRIES`, `_BACKOFF`, `_BREAKER`, `_CACHE_TTL` | 3s timeout / 2 retries / 0.2s backoff / 30s breaker / 60s cache |
| Incident history | `AIOPS_INCIDENT_HISTORY_PROVIDERS` | `mock` provider only |
| RCA chat / RAG | `AIOPS_RCA_LLM_PROVIDER`, `AIOPS_RCA_LLM_MODEL`, `AIOPS_RCA_CHAT_MAX_SESSIONS`, `_MAX_TURNS`, `_HISTORY_TURNS`, `_TTL`, `AIOPS_RCA_RAG_LIMIT`, `_RAG_MIN_SIMILARITY`, `AIOPS_RCA_MEMORY_PROVIDERS`, `AIOPS_RCA_CHANGE_LOOKBACK_HOURS` | Anthropic regardless of `AIOPS_LLM_PROVIDER` (ADR-003); deterministic answers when unavailable |
| Real-time feeds | `AIOPS_RCA_STREAM_HEARTBEAT`, `AIOPS_RCA_STREAM_IDLE_TIMEOUT`, `AIOPS_ALERT_BROADCAST_INTERVAL` | 15 s heartbeat / 60 s idle close / 5 s alert poll |
| Failure injection (`demo/ecommerce/`) | `FI_MODE` (`application`/`infrastructure`/`hybrid`), `FI_BACKEND` (`k8s`/`docker`), `FI_NAMESPACE`, `FI_DRY_RUN` | `hybrid` mode, `k8s` backend, `ecommerce` namespace. Note these are **not** `AIOPS_`-prefixed — the SUT is a standalone app. |

The remote seams (Loki, Jaeger) have **circuit breakers** — `AIOPS_*_CIRCUIT_OPEN_SECONDS` — so a down backend degrades the agent rather than hanging the request. Preserve that when adding a new remote provider.

### Constraints code review will catch

Each of these has a test that fails CI, so they are worth knowing before you write the code:

- **No direct vendor SDK imports.** `import anthropic` / `import openai` outside `aiops/llm/` → `test_no_direct_llm_sdk_imports_outside_aiops_llm`.
- ~~No direct flagd mutation via kubectl~~ — **retired.** flagd left with the OTel Demo and `tests/test_no_kubectl_for_flagd.py` is deleted. Clearing a fault now goes through the `automation.fault.clear` capability (`tests/test_fault_clear_seam.py`), which refuses any `target` other than `off`: an approved "fix" that *injected* something would be the worst outcome of a HITL flow.
- **No `@app.on_event` in `demo/ui/`.** Use lifespan handlers → `test_no_fastapi_on_event_in_demo_ui` (DEMO-15, #67).
- **No HITL checks inside agent logic.** HITL is enforced at the *registry boundary* — just call `get_registry().call(capability, ...)` and a Required-level capability returns `ok=False` when no approver is wired. Agents never gate-check themselves.
- **Every RCA fix step must set `requires_hitl=True`** → `test_rca_fix_step_rejects_requires_hitl_false`.
- **Every new failure scenario ships with a truth file** → `test_every_scenario_has_a_truth_file`. `tests/test_scenarios_yaml.py` is strict about a lot more than that: the YAML `id` must equal the filename stem, ids must be unique, `failure_key` must resolve to a registered `Failure`, UI descriptors must match the server catalog, `needs_load` must agree with the registry's `LoadHint`, and every alertname the catalog references must exist as a real Prometheus rule. `tests/test_ecommerce_truth_files_evaluable.py` additionally requires each truth file to be *evaluable*, not merely present.
- **Every new agent ships with `agents/<dir>/evals/golden.json`.** The eval harness discovers it automatically.
- **`aiops/` never imports `agents/` (or `demo/`).** The dependency arrow is `demo/ → agents/ → aiops/`, checked by AST (not substring matching) → `tests/test_layering.py`. The one sanctioned exception is `aiops/runtime/orchestrator.py`, which by design sits above the agents it chains.
- **Context-layer parity while it migrates.** `tests/test_context_shadow.py` requires zero mismatches between shadow-mode context output and legacy retrieval; `test_rca_context_adapter.py` / `test_notification_assembler_context_adapter.py` gate on byte-identical output between the adapter and the pre-migration prompt strings; `test_retrieval_call_sites.py` is a ratchet that fails if the count of duplicated per-agent retrieval call sites grows instead of shrinks.
- **The RCA prompt cannot regain injection truth or a hardcoded action key.** `tests/test_rca_prompt_v7.py` is a two-sided ratchet on `SYSTEM_PROMPT_V7` — forbidden strings must stay out, required safety clauses must stay in — with a positive control proving the check isn't vacuous. Any edit to `agents/rca_agent/prompts.py` must keep it passing.
- **RCA's historical memory cannot recall the truth-file corpus.** Only providers in `investigation.memory.OUTCOME_BACKED_PROVIDERS` may supply a prior; registering a new `incident_history` provider without classifying it fails `tests/test_rca_memory_blindness.py::test_every_shipped_provider_is_either_allowed_or_deliberately_not`.
- **The RCA chat surface cannot reach the tool registry.** `tests/test_rca_chat_boundary.py` AST-checks `agents/rca_agent/chat.py` and `demo/ui/rca_chat_routes.py` for any `aiops.tools.*` import beyond the one allowed read-only RAG accessor — anything needing the chatops or automation seams goes in a sibling module (see `rca_share_routes.py`).
- **The RCA learning module cannot touch code.** `agents/rca_agent/learning.py` may only write outcome rows; `tests/test_rca_learning.py::TestLearningBoundary` parses its AST for any reference to a prompt symbol, file write, tool/policy registration, or dynamic execution.

### The seams to use, not bypass

| Want to... | Use... | Don't... |
|---|---|---|
| Call an LLM | `aiops.llm.complete` / `acomplete` | `import anthropic` / `import openai` |
| Call ServiceNow / Slack / Prom / Jaeger / Loki | `aiops.tools.get_registry().call(capability, ...)` | `httpx.post(...)` / `kubectl` |
| Clear an injected fault after HITL approval | `automation.fault.clear` via the registry | importing `demo.ecommerce.failure_injection` from `aiops/` (breaks the layering test) |
| Gate a destructive action | `aiops.policy.get_gate().enforce(action, ctx)` | `if user_confirmed:` inside the agent |
| Persist verdicts / classifications / state | `aiops.state.repository.save_*` / `load_*` | raw SQLAlchemy / SQL |
| Chain the Reactive-Active flow | `aiops.runtime.orchestrator.run_reactive_flow(alert)` | re-wiring the agent calls inline |
| Gather incident evidence for an agent | `aiops.context.build(request)` behind `AIOPS_CONTEXT_LAYER`, projected via an `agents/<name>/context_adapter.py` | each agent independently re-querying Prometheus/Loki/CMDB/on-call/Git |
| Add timeout/retry/breaker/cache to a new provider | wrap the call in `aiops.tools.resilience.guard(...)` | hand-rolling a subset of the four and forgetting one |

#### HITL levels live in two files that nothing forces to agree

`policies/hitl.rego` is **reference-only today**; the runtime authority is `DEFAULT_LEVELS` in `aiops/policy/gate.py` (ADR-005 — wiring OPA in as the runtime check is a Phase 2 step). No test compares them, so they silently drift. **When you change an action's autonomy level, edit both**, and match the catalog row.

#### The orchestrator seam

`run_reactive_flow(alert)` is the **single** entry point for the RA-001 → RA-002 → RA-003 → RA-005+006 chain. It triages, classifies, tickets, notifies, and persists each step with FK guards; notification failure is caught and non-fatal (`routing=None`). It returns a `ReactiveFlowResult`, and `.to_api_dict()` reproduces the legacy `POST /api/triage` response body verbatim — that shape is a public contract for the dashboard, the SPAs, and the tests, so don't change it casually.

Four callers share it: the `/api/triage` route, the live-alert sweep, the auto-triage loop, and RA-008 Incident Commander. If you need the chain, call it — don't re-wire the agents inline. Note the dependency direction: agents never import `aiops.runtime`.

#### Registry capability namespaces

Tools register under a dotted `capability` name and are dispatched by `get_registry().call(capability, ...)`; multiple providers can serve one capability (e.g. `mock.*` vs `snow.*`, selected by whether real credentials are present). Namespaces currently in use:

`itsm.incident.*` · `itsm.cmdb.*` · `itsm.ticket.close` · `observability.metrics.*` · `observability.logs.query` · `observability.traces.*` · `observability.events.query` · `automation.fault.clear` · `oncall.schedule.lookup` · `incident.resolvers.lookup` · `notify.send` · `chatops.war_room.create` · `knowledge.publish` · `rca.fix_step.execute` · `automation.runbook.{execute,simulate,apply}` · `scm.file.read` · `scm.repo.tree` · `scm.commit.history` · `scm.diff` · `scm.pr.list`

Register a new one with the `@tool(name=, capability=, provider=)` decorator in `aiops/tools/`. Several subpackages are deliberately *not* registries: `aiops/tools/alerts/` holds pure webhook-payload → canonical `Alert` adapters (Alertmanager, CloudWatch, Datadog, Prometheus), most of `aiops/tools/chatops/` is the client/adapter seam rather than a registered capability, and `topology/` / `incident_history/` / `change_context/` are provider-chain seams (own `register_provider`, consumed by `aiops/context/collectors/`) rather than dispatch-by-capability.
