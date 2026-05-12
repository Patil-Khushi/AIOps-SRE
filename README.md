# Adaptive AIOps + SRE Ops

Vendor-neutral, multi-agent platform that automates IT operations across four maturity phases (Reactive-Active → Proactive → Predictive → Prescriptive-Adaptive). 30 modular agents, a dedicated **RCA Agent** that produces executable fix steps with rollback, and SRE discipline woven into every phase.

This repository is the **Proof-of-Concept (POC) build** — not production. The goal is a credible end-to-end demo on synthetic / open-source data, anchored on the OpenTelemetry Demo running in a local Rancher Desktop k3s cluster.

> **If you are new here, read [`docs/poc_aiops_onboarding_guide.docx`](docs/poc_aiops_onboarding_guide.docx) first** — it is the canonical onboarding text. [`CLAUDE.md`](CLAUDE.md) summarises the architecture and design principles.

---

## Status

| Phase | Window | What's done | What's open |
|---|---|---|---|
| **Phase 0 — Setup** | W0–2 | Repo skeleton, platform seams, demo bootstrap, 3 failure scenarios, truth-file template, eval harness skeleton, OPA policy starter | First end-to-end agent run |
| Phase 1 — Reactive backbone | W3–5 | — | Alert Triage, Auto-Ticketing, Notification Router, Log Correlation |
| Phase 2 — RCA backbone | W6–8 | — | RCA Agent, HITL UI, Incident Commander |
| Phase 3 — Proactive + first prediction | W9–10 | — | Anomaly Detector, Dependency Mapper, Early Warning, SLO Breach Predictor |
| Phase 4 — Polish + demo | W11–12 | — | Recorded demo, post-POC backlog |

See [`docs/poc_aiops_onboarding_guide.docx`](docs/poc_aiops_onboarding_guide.docx) §8 for the full roadmap with entry/exit criteria.

---

## Quick start

**Prerequisites:** [Rancher Desktop](https://rancherdesktop.io/) (k3s enabled), `kubectl`, `helm`, Python 3.12+, [`uv`](https://github.com/astral-sh/uv) (or `pip`). See [`infra/README.md`](infra/README.md) for install commands and why we use Rancher Desktop instead of Docker / kind.

### Windows / PowerShell (primary)

```powershell
# 1. Make sure Rancher Desktop is running (tray icon shows 'Kubernetes: running').

# 2. Install the OTel demo into the local k3s cluster
.\infra\bootstrap.ps1

# 3. Install Python deps
uv sync

# 4. Port-forward the demo's reverse proxy (leave this running in its own window)
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080

# 5. Trigger a failure scenario
uv run python -m demo.failure_injection.inject slow-product-catalog

# 6. Open Grafana
# http://localhost:8080/grafana/
```

### macOS / Linux

```bash
./infra/bootstrap.sh
uv sync
uv run python -m demo.failure_injection.inject slow-product-catalog
```

### Tear down

```powershell
.\infra\teardown.ps1     # or  ./infra/teardown.sh
```

---

## Repository layout

```
.
├── CLAUDE.md                    # Guidance for Claude Code (architecture + principles)
├── README.md                    # This file
├── CONTRIBUTING.md              # Branching, commits, code style
├── pyproject.toml               # Python project + tool config
├── docs/                        # Source-of-truth design documents (binary Office files)
├── aiops/                       # Cross-cutting platform code — the day-one seams
│   ├── llm/                     # Provider-agnostic LLM gateway (Anthropic / OpenAI / Ollama)
│   ├── tools/                   # Tool registry — wraps every external integration
│   └── policy/                  # Platform-enforced HITL gate (Required/Optional/None)
├── agents/                      # Individual agents (one directory per agent). Empty in Phase 0.
├── evals/                       # Eval harness — hand-rolled JSON test cases
├── demo/                        # The "pretend customer" environment
│   ├── otel-demo/               # Helm values for the OpenTelemetry Demo
│   ├── failure_injection/       # One-command failure scenario runner
│   ├── truth_files/             # Ground-truth per scenario (cause + expected fix)
│   └── load/                    # k6 load scripts for steady-state traffic
├── infra/                       # Local cluster bootstrap (Rancher Desktop k3s + Helm)
├── policies/                    # OPA policies — HITL + guardrails as code
├── tests/                       # Repo-level smoke tests
├── scripts/                     # Convenience scripts
└── .github/workflows/           # CI: lint + tests + eval gate
```

The agent catalog (30 agents, primary tool mappings, KPIs, HITL levels) lives in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`. **Treat the catalog as authoritative** when implementing an agent.

---

## Design principles (non-negotiable)

These come from `docs/Adaptive_AIOps_Solution_Design.pptx` and shape every code decision:

1. **Vendor-neutral by default** — every external dependency goes through `aiops/llm/` or `aiops/tools/`. Never call vendor SDKs from agent code directly.
2. **Modular, individually sellable agents** — each agent has a stable contract. License-one and license-all must both work.
3. **HITL is platform-enforced** — gates live in `aiops/policy/` and `policies/*.rego`, not inside agent logic. Required-HITL actions cannot be bypassed by a buggy or compromised agent.
4. **Policy-as-code** — versioned in Git, reviewed like code.
5. **Safe-autonomy primitives** — dry-run, simulation, blast-radius caps, rollback are first-class.
6. **Closed-loop learning** — every model/prompt/policy is versioned and shadow-evaluated before promotion.
7. **Eval harness from day one** — when you build an agent, build its eval set in the same week. A prompt change is a model change.
8. **Truth files for every demo scenario** — see `demo/truth_files/template.yaml`.

---

## Where things live (cheat sheet)

| Want to... | Look in / read |
|---|---|
| Understand an agent's contract | `docs/Adaptive_AIOps_Agent_Catalog.xlsx` |
| Add a new agent | `agents/README.md` |
| Add a new failure scenario | `demo/failure_injection/README.md` + write a truth file in `demo/truth_files/` |
| Call an LLM | `aiops/llm/__init__.py` (never call SDKs directly) |
| Call ServiceNow / Splunk / etc. | `aiops/tools/registry.py` |
| Add a HITL-gated action | `aiops/policy/gate.py` + a rule in `policies/hitl.rego` |
| Score an agent | `evals/harness.py` |
| Bring up the demo | `infra/bootstrap.ps1` (or `.sh`) |
| Configure LLM access | `docs/llm-access.md` |

---

## License

Internal — IT Services Practice. Not for external distribution without sign-off.
