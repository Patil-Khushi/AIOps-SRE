# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

As of 2026-05-12 the repo has the **Phase 0 platform seams** (`aiops/llm`, `aiops/tools`, `aiops/policy`), the demo bootstrap (`infra/`, `demo/otel-demo`, `demo/failure_injection`, `demo/truth_files`, `demo/load`), the eval harness skeleton, the OPA policy starter, smoke tests, and CI in place. Phase 1 has started: `agents/alert_triage/` is the first agent in flight, and the demo UIs (`demo/ui/` FastAPI + `demo/dashboard/` React/Vite) are wired up. The `docs/` design files remain the authoritative source for agent contracts and architecture.

Source-of-truth documents (binary Office files):

- `docs/Adaptive_AIOps_Unified_Architecture.pptx` — the one-slide architecture diagram (the master picture).
- `docs/Adaptive_AIOps_Solution_Design.pptx` — phase decomposition, integration matrix, HITL policy, rollout plan, KPIs, risks.
- `docs/Adaptive_AIOps_Agent_Catalog.xlsx` — every agent with description, key features, primary tool mapping, secondary integrations, inputs/outputs, HITL level, sellable-standalone flag, KPI. **Authoritative agent reference.** Sheets: README, Master, Reactive-Active, Proactive, Predictive, Prescriptive-Adaptive, Tool-Mapping-Matrix, Phase-Summary.
- `docs/aiops_onboarding_guide.docx` — concept primer (AIOps, SRE, RCA, agentic AI vocabulary).
- `docs/poc_aiops_onboarding_guide.docx` — POC playbook: data problem, what to use instead, reference stack, 12-week roadmap, pitfalls.

When the design intent is unclear, extract text from the docx/pptx/xlsx (they are zip archives of XML — `xl/sharedStrings.xml` for Excel strings, `ppt/slides/slideN.xml` for slides, `word/document.xml` for Word) and consult the catalog before guessing. Do not invent agent behavior or integrations the catalog does not specify.

## What is being built

**Adaptive AIOps + SRE Ops** — a vendor-neutral, multi-agent platform that automates IT operations across four maturity phases. The product is **30 modular agents**, each individually sellable, with a dedicated **RCA Agent** as the headline differentiator (it produces executable fix steps with rollback, not just a likely-cause list).

The four phases (each with one SRE-specific agent):

| Phase | Count | Question | Representative agents |
|---|---|---|---|
| Reactive-Active | 8 | "What just broke?" | Alert Triage, Incident Classifier, Auto-Ticketing, Runbook Executor, Notification Router, War-Room Assembler, Log Correlation, **Incident Commander (SRE)** |
| Proactive | 7 | "What is starting to look wrong?" | Anomaly Detector, Drift Monitor, Dependency Mapper, Noise Reducer, Early Warning, Topology Discovery, **Toil Detector (SRE)** |
| Predictive | 7 | "What will break, and when?" | Failure Forecaster, Capacity Planner, SLO Breach Predictor, Seasonality Learner, Root-Cause Predictor, Change Impact Predictor, **Reliability Forecaster (SRE)** |
| Prescriptive-Adaptive | 8 | "What should we do — and can the system do it?" | Remediation Recommender, Auto-Healer, Policy Optimizer, Feedback Learner, Cost-Aware Scaler, Knowledge Synthesizer, **Chaos Orchestrator (SRE)**, **RCA Agent ★** |

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

- **Do not build all 30 agents.** A reasonable POC scope is **6–10 agents end-to-end** on one full Reactive→Prescriptive flow (typical: Alert Triage → Incident Classifier → Auto-Ticketing → Log Correlation → RCA Agent → Remediation Recommender, plus one or two SRE agents and one Predictive agent for the "wow" moment). The rest may be stubbed for narrative continuity.
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
| Agent framework | LangGraph (or AutoGen / in-house) | Pick one and stick to it. |
| LLM | Anthropic Claude or OpenAI hosted; Ollama for local fallback | Decide per agent based on data sensitivity. Pin model versions; never use "latest". |
| Vector store | pgvector or Qdrant | Both free and well-documented. |
| Topology graph | Neo4j Community or in-process JSON | Start simple. |
| Policy / governance | Open Policy Agent (OPA) | Industry-standard policy-as-code. |
| Evals | Hand-rolled JSON test cases first | Add Ragas/DeepEval/LangSmith only when count gets unwieldy. |
| Source control / CI | GitHub + GitHub Actions | |

## 12-week POC roadmap (target shape)

The onboarding guide specifies five phases. Use this to judge what's in scope at any given moment:

- **Phase 0 — Setup (W0–2):** repo skeleton, demo app deployed with OTel→Prom/Loki/Tempo flowing, Grafana dashboards, failure-injection library v0 (≥3 scenarios), truth-file template, LLM API access. *Out of scope: any agent.*
- **Phase 1 — Reactive backbone (W3–5):** Alert Triage v1, Auto-Ticketing v1 (ServiceNow PDI), Notification Router v1 (Slack), Log Correlation v1, eval harness, demo UI v0. First internal demo. *Out of scope: predictive, RCA, autonomous-destructive, polished UI.*
- **Phase 2 — RCA backbone (W6–8):** RCA Agent v1, HITL UI v1, Incident Commander v1, Knowledge Synthesizer v0 (postmortem skeleton only), audit trail. *Out of scope: predictive, prescriptive autonomy, chaos, multi-tenant.*
- **Phase 3 — Proactive + first prediction (W9–10):** Anomaly Detector v1, Dependency Mapper v1 (live OTel service map), Early Warning v1, SLO Breach Predictor v1 on synthetic SLOs, Reliability Forecaster v0. *Out of scope: full predictive suite, chaos.*
- **Phase 4 — Polish + demo (W11–12):** rehearsed scenarios, narrative, recorded demo, post-POC backlog written.

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

## Repository layout (Phase 0)

```
aiops/                     # platform seams — never call vendor SDKs outside this package
├── llm/                   # provider-agnostic LLM gateway (anthropic / openai / ollama / stub)
├── tools/                 # tool registry — every external integration registers here
└── policy/                # platform-enforced HITL gate (None / Optional / Required)
agents/                    # one directory per agent (alert_triage in flight; more land in Phase 1)
evals/                     # hand-rolled JSON test harness; CI gates pass-rate
demo/
├── otel-demo/             # Helm values for the upstream OpenTelemetry Demo chart
├── failure_injection/     # one-command failure scenario runner + starter scenarios
├── truth_files/           # ground truth per scenario (cause + expected fix)
├── load/                  # k6 baseline load script
├── ui/                    # FastAPI demo server (uv extra: ui) — served at :8765
└── dashboard/             # React + Vite + Tailwind dashboard (built into dist/, mounted at /dashboard/)
infra/                     # Rancher Desktop k3s bootstrap (PowerShell + bash) + Prometheus rules
policies/                  # OPA policies (hitl.rego); reference-only in Phase 0
tests/                     # repo-level smoke tests
start.ps1 / stop.ps1       # one-command bring-up / tear-down of cluster port-forwards + UI
.github/workflows/         # CI: ruff + pytest + eval gate + opa check
```

> **Why `aiops/` and not `platform/`:** Python's stdlib has a module called `platform`. Using that name as a top-level package shadows it and breaks pytest, uv, and most libraries that introspect the runtime. Don't change it back.

## When you write code

### Local environment constraints

- **No Docker, no cloud, ~16 GB laptops.** Org policy bans Docker on dev machines; AKS/GKE are post-POC. All cluster work is Rancher Desktop's bundled k3s. Allocate ≥6 GB to its VM (Settings → Virtual Machine); the OTel demo uses ~3.5 GB inside.
- **Rancher Desktop ships a `kuberlr`-wrapped `kubectl` that rejects standard flags from Python `subprocess` calls.** Install a standalone `kubectl` (`winget install --scope user Kubernetes.kubectl`) — `start.ps1` and `demo/failure_injection/inject.py` prefer it via `$LOCALAPPDATA\Programs\kubectl`.
- **Two PowerShell windows.** `start.ps1` runs port-forwards as background jobs in the *current* session; closing that shell kills them. Use `stop.ps1` to tear them down cleanly.

### Common commands

```powershell
# Install deps (one-time)
uv sync --extra dev
uv sync --extra ui          # FastAPI demo server (demo/ui/) — required by start.ps1
uv sync --extra embeddings  # sentence-transformers for Alert Triage dedup (optional)

# Bring up the OTel demo into Rancher Desktop k3s (one-time, ~10 min)
.\infra\bootstrap.ps1                              # bash equivalent: ./infra/bootstrap.sh

# Then either: leave a single port-forward open for the OTel demo proxy...
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080

# ...or: one-command bring-up — checks the cluster, port-forwards
# Prometheus :9090 / Jaeger :16686 / frontend-proxy :8080, builds the
# React dashboard if needed, starts the FastAPI UI on :8765, opens the browser.
.\start.ps1                                        # tear down with: .\stop.ps1

# Trigger a failure scenario
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear

# Run the smoke tests (no cluster needed)
uv run pytest

# Run a single test
uv run pytest tests/test_smoke.py::test_hitl_gate_blocks_required_without_approver

# Run the eval harness for all agents
uv run python -m evals.harness

# Run the eval harness for one agent
uv run python -m evals.harness --agent alert_triage

# CI gate — fails if pass rate drops below threshold
uv run python -m evals.harness --ci --min-pass-rate 0.85

# Lint / format
uv run ruff check .
uv run ruff format .

# Tear down the OTel demo (leaves Rancher Desktop's k3s running)
.\infra\teardown.ps1
```

### Constraints code review will catch

- **No direct vendor SDK imports.** `import anthropic` / `import openai` outside `aiops/llm/` fails the smoke test (`test_no_direct_llm_sdk_imports_outside_aiops_llm`).
- **No HITL checks inside agent logic.** Use `aiops.policy.get_gate().enforce(...)` at the action boundary.
- **Every new failure scenario ships with a truth file.** The smoke test `test_every_scenario_has_a_truth_file` enforces it.
- **Every new agent ships with `evals/golden.json`.** The eval harness will discover it automatically.

### The seams to use, not bypass

| Want to... | Use... | Don't... |
|---|---|---|
| Call an LLM | `aiops.llm.complete` / `acomplete` | `import anthropic` |
| Call ServiceNow / Splunk / Slack / kubectl | `aiops.tools.get_registry().call(capability, ...)` | `httpx.post("https://servicenow...")` |
| Gate a destructive action | `aiops.policy.get_gate().enforce(action, ctx)` | `if user_confirmed:` inside the agent |
