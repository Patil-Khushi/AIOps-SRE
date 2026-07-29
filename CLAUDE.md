# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

The repo has the **Phase 0 platform seams** (`aiops/llm`, `aiops/tools`, `aiops/policy`, `aiops/state`, `aiops/runtime`), the demo bootstrap (`infra/`, `demo/otel-demo`, `demo/failure_injection`, `demo/truth_files`, `demo/load`), the eval harness, OPA policy, and CI in place. **Phase 1 is shipped and Phase 2 is mostly shipped**: merged agents under `agents/` — `alert_triage/` (RA-001+002 combined: triage + classification), `auto_ticketing/` (RA-003), `notification_assembler/` (RA-005+006 combined: routing + war-room), plus `incident_commander/` (RA-008), `rca_agent/` (PRS-008), `knowledge_synthesizer/` (PRS-007), `log_correlation/` (RA-007), `auto_healer_lite/` (HITL demo), and post-POC stubs. The chatops seam lives under `aiops/tools/chatops/` (JSON-file + WebSocket + Slack adapters), the combined triage/classifier UI at `/combined`, and the React dashboard at `/dashboard`. The `docs/` design files remain the authoritative source for agent contracts and architecture.

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

Catalog rows and directory names do not match 1:1. **`agents/README.md` is the authoritative shipped inventory.**

Source-of-truth documents (binary Office files):

- `docs/Adaptive_AIOps_Unified_Architecture.pptx` — the one-slide architecture diagram (the master picture).
- `docs/Adaptive_AIOps_Solution_Design.pptx` — phase decomposition, integration matrix, HITL policy, rollout plan, KPIs, risks.
- `docs/Adaptive_AIOps_Agent_Catalog.xlsx` — every agent with description, key features, primary tool mapping, secondary integrations, inputs/outputs, HITL level, sellable-standalone flag, KPI. **Authoritative agent reference.** Sheets: README, Master, Reactive-Active, Proactive, Predictive, Prescriptive-Adaptive, Tool-Mapping-Matrix, Phase-Summary.
- `docs/aiops_onboarding_guide.docx` — concept primer (AIOps, SRE, RCA, agentic AI vocabulary).
- `docs/poc_aiops_onboarding_guide.docx` — POC playbook: data problem, what to use instead, reference stack, 12-week roadmap, pitfalls.

When the design intent is unclear, extract text from the docx/pptx/xlsx (they are zip archives of XML — `xl/sharedStrings.xml` for Excel strings, `ppt/slides/slideN.xml` for slides, `word/document.xml` for Word) and consult the catalog before guessing. Do not invent agent behavior or integrations the catalog does not specify.

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
| Agent framework | LangGraph (or AutoGen / in-house) | Pick one and stick to it. |
| LLM | Anthropic Claude or OpenAI hosted; Ollama for local fallback | Decide per agent based on data sensitivity. Pin model versions; never use "latest". |
| Vector store | pgvector or Qdrant | Both free and well-documented. |
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
├── tools/                 # tool registry — every external integration registers here
│   ├── chatops/           # ChatOpsClient + JSON-file + WebSocket + Slack adapters
│   ├── feature_flags/     # flagd adapter (replaces the kubectl-patch shell-out, ARCH-1)
│   ├── itsm/              # ServiceNow PDI client (incident.create/update, cmdb.lookup)
│   ├── observability/     # read-only Prometheus + Jaeger queries (autonomy NONE)
│   └── alerts/            # alert normalization (Prometheus → canonical Alert)
├── policy/                # platform-enforced HITL gate (None / Optional / Required)
├── state/                 # SQLModel persistence (sqlite default; Postgres via URL swap post-POC)
├── runtime/               # orchestrator seam — run_reactive_flow() chains RA-001→003→005+006
└── runbooks/              # runbook definitions used by auto_healer_lite / runbook_executor
agents/                    # Shipped agents (agents/README.md is the authoritative inventory)
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

## When you write code

### Local environment constraints

- **No Docker, no cloud, ~16 GB laptops.** Org policy bans Docker on dev machines; AKS/GKE are post-POC. All cluster work is Rancher Desktop's bundled k3s. Allocate ≥6 GB to its VM (Settings → Virtual Machine); the OTel demo uses ~3.5 GB inside.
- **Rancher Desktop ships a `kuberlr`-wrapped `kubectl` that rejects standard flags from Python `subprocess` calls.** Install a standalone `kubectl` (`winget install --scope user Kubernetes.kubectl`) — `start.ps1` and `demo/failure_injection/inject.py` prefer it via `$LOCALAPPDATA\Programs\kubectl`.
- **Two PowerShell windows.** `start.ps1` runs port-forwards as background jobs in the *current* session; closing that shell kills them. Use `stop.ps1` to tear them down cleanly.
- **flagd flag mutation goes through the seam.** Use `aiops.tools.get_registry().call("feature_flags.set_variant", flag=..., variant=...)` (or `feature_flags.get_variant` / `list_variants` / `reset_all`). Direct `kubectl patch flagd-config` is forbidden — `tests/test_no_kubectl_for_flagd.py` will fail CI for any new caller. Background: ARCH-1 (issue #70, `docs/arch_1_feature_flags_seam_design.md`).
- **PowerShell 5.1's `Get-Content` default encoding is CP1252, not UTF-8.** Tailing `demo/audit/chatops.jsonl` without `-Encoding UTF8` turns em-dashes into `â€"` mojibake. See ONBOARDING.md §11 "Tailing the chatops audit log".

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

# Lint / format / typecheck
uv run ruff check .
uv run ruff format .
uv run mypy aiops agents

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

### Configuration surface

Everything is env-var driven and read at the seam, never in agent code. Read from `.env` (loaded explicitly — `uv run` does *not* auto-load it). Every seam degrades to a mock/stub when its vars are absent, so the whole demo runs unconfigured.

| Area | Vars | Default behaviour when unset |
|---|---|---|
| LLM | `AIOPS_LLM_PROVIDER` (`anthropic`/`openai`/`ollama`/`stub`), `AIOPS_LLM_MODEL`, `AIOPS_LLM_MAX_TOKENS_PER_CALL` | stub provider |
| State | `AIOPS_STATE_DB_URL` | `sqlite:///./data/state.db` |
| Runbooks | `AIOPS_RUNBOOKS_DIR` | `data/runbooks` |
| ITSM | `AIOPS_SERVICENOW_INSTANCE_URL` / `_USER` / `_PASSWORD`, `AIOPS_USE_MOCK_ITSM` | mock ITSM provider |
| Observability | `AIOPS_PROMETHEUS_URL`, `AIOPS_LOKI_URL`, `AIOPS_JAEGER_URL`, `AIOPS_GRAFANA_URL` / `_API_KEY` | provider registered but calls fail soft |
| ChatOps | `AIOPS_SLACK_WEBHOOK_URL`, `AIOPS_SLACK_BOT_TOKEN`, `AIOPS_SLACK_USER_MAP_JSON`, `AIOPS_PAGERDUTY_INTEGRATION_KEY`, `AIOPS_JITSI_BASE` | JSON-file + WebSocket sinks only |
| HITL | `AIOPS_HITL_DEFAULT`, `AIOPS_HITL_APPROVAL_TIMEOUT` | Required-level actions deny without an approver |

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

### The seams to use, not bypass

| Want to... | Use... | Don't... |
|---|---|---|
| Call an LLM | `aiops.llm.complete` / `acomplete` | `import anthropic` / `import openai` |
| Call ServiceNow / Slack / flagd / Prom / Jaeger | `aiops.tools.get_registry().call(capability, ...)` | `httpx.post(...)` / `kubectl patch` |
| Gate a destructive action | `aiops.policy.get_gate().enforce(action, ctx)` | `if user_confirmed:` inside the agent |
| Persist verdicts / classifications / state | `aiops.state.repository.save_*` / `load_*` | raw SQLAlchemy / SQL |
| Chain the Reactive-Active flow | `aiops.runtime.orchestrator.run_reactive_flow(alert)` | re-wiring the agent calls inline |

#### The orchestrator seam

`run_reactive_flow(alert)` is the **single** entry point for the RA-001 → RA-002 → RA-003 → RA-005+006 chain. It triages, classifies, tickets, notifies, and persists each step with FK guards; notification failure is caught and non-fatal (`routing=None`). It returns a `ReactiveFlowResult`, and `.to_api_dict()` reproduces the legacy `POST /api/triage` response body verbatim — that shape is a public contract for the dashboard, the SPAs, and the tests, so don't change it casually.

Four callers share it: the `/api/triage` route, the live-alert sweep, the auto-triage loop, and RA-008 Incident Commander. If you need the chain, call it — don't re-wire the agents inline. Note the dependency direction: agents never import `aiops.runtime`.

#### Registry capability namespaces

Tools register under a dotted `capability` name and are dispatched by `get_registry().call(capability, ...)`; multiple providers can serve one capability (e.g. `mock.*` vs `snow.*`, selected by whether real credentials are present). Namespaces currently in use:

`itsm.incident.*` · `itsm.cmdb.*` · `itsm.ticket.close` · `observability.metrics.*` · `observability.logs.query` · `observability.traces.*` · `feature_flags.*` · `oncall.schedule.lookup` · `incident.resolvers.lookup` · `notify.send` · `chatops.war_room.create` · `knowledge.publish` · `rca.fix_step.execute` · `automation.runbook.{execute,simulate,apply}`

Register a new one with the `@tool(name=, capability=, provider=)` decorator in `aiops/tools/`. Two subpackages are deliberately *not* registries: `aiops/tools/alerts/` holds pure webhook-payload → canonical `Alert` adapters (Alertmanager, CloudWatch, Datadog, Prometheus), and most of `aiops/tools/chatops/` is the client/adapter seam rather than a registered capability.
