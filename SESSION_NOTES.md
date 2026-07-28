# Session Notes

Running log of what's been done across working sessions on this repo. **Append the newest session at the top.** Each session entry has: date, who, what shipped, current state, decisions, and what's queued next.

The goal is that the next person (or the next Claude session) can read the top entry, then `CLAUDE.md` and `ONBOARDING.md`, and be productive in 10 minutes.

---

## 2026-05-08 — Phase 0 setup complete on reference laptop

**Driver:** Chinmay. **Pair:** Claude.

### What shipped

Phase-0 environment is verified end-to-end on the reference laptop:

- WSL2 + **Rancher Desktop** (k3s) running. Context: `rancher-desktop`.
- OpenTelemetry Demo deployed via Helm chart `opentelemetry-demo 0.40.7` (appVersion 2.2.0) into namespace `otel-demo`.
- All 26 demo pods Running; Helm release status `deployed`, revision 5.
- Smoke tests: **11/11 pytest passed**, eval harness emits `phase0=true, pass_rate=1.0`.
- Failure injection (`uv run python -m demo.failure_injection.inject slow-product-catalog`) works end-to-end with `--clear` reset.
- Webstore at `http://localhost:8080/`, Grafana at `/grafana/`, Jaeger at `/jaeger/ui/`, Loadgen at `/loadgen/`, Feature flags at `/feature/` — all behind one `kubectl port-forward svc/frontend-proxy 8080:8080`.

### Files created or significantly changed in this session

- `ONBOARDING.md` (new) — laptop setup walkthrough, IT ticket template, free-tier account list, TODO list, agent-assignments table, verification checklist, troubleshooting.
- `infra/bootstrap.ps1` + `bootstrap.sh` — rewritten for Rancher Desktop k3s. Idempotent (skips Helm if `otel-demo` is already healthy). `-Force` switch to override.
- `infra/teardown.ps1` + `teardown.sh` — uninstall Helm release + drop namespace; do **not** stop Rancher Desktop / k3s.
- `infra/README.md` — replaced kind references with Rancher Desktop reality.
- `infra/kind-config.yaml` — **deleted** (no longer needed).
- `demo/otel-demo/values.yaml` — slimmed to chart 0.40.x reality (top-level `flagd`, `observability`, per-component overrides removed; only `default.envOverrides` and `opensearch.enabled: false` kept).
- `demo/failure_injection/inject.py` — added `_require_kubectl()` resolver that picks a real kubectl over the kuberlr wrapper, plus `--field-manager=helm` on patches.
- `demo/failure_injection/scenarios/currency-pod-kill.yaml` — selector updated for the chart-0.40 label rename (`currencyservice` → `currency`).
- `CLAUDE.md` — common-commands block updated; new "Local-environment quirks" subsection.
- `README.md` — quick-start dropped Docker/kind, points at ONBOARDING.md.

### Decisions worth remembering

- **Runtime: Rancher Desktop k3s** (not Docker Desktop, not kind). Picked because no Docker is allowed in this org, and Rancher Desktop bundles k3s + kubectl + helm + nerdctl in one install.
- **LLM: Anthropic / OpenAI hosted, NOT Ollama.** 16 GB RAM laptops can't host both the OTel demo (~3.5 GB) and a local LLM (~6+ GB resident). Use the hosted APIs.
- **Skip the OpenSearch sub-chart in the OTel demo Helm values.** Saves ~1.5 GB; we don't use it for Phase 0 (Loki / Tempo via the OTel Collector cover us).
- **Idempotent bootstrap.** `infra/bootstrap.ps1` detects an already-healthy release and skips Helm — avoids the field-manager conflict that bites on re-runs.

### Two gotchas the team should know about (also in `ONBOARDING.md` §7)

1. **Rancher Desktop's `kubectl.exe` is a kuberlr wrapper.** It rejects standard kubectl args (`-n`, `--client`, etc.) when invoked under Python `subprocess` — works fine from PowerShell directly. Symptom: `Error: unknown shorthand flag: 'n' in -n`. Fix: `winget install --scope user --id Kubernetes.kubectl` for a real kubectl alongside it. `inject.py` auto-resolves.
2. **`flagd-config` field-manager poisoning.** Plain `kubectl patch` registers `kubectl-patch` as the field manager; subsequent `helm upgrade` then conflicts. Already fixed in `inject.py` (uses `--field-manager=helm`). Recovery for a poisoned cluster: `kubectl delete configmap flagd-config && helm rollback otel-demo`.

### Where Phase 1 starts (next session)

Tracked in `ONBOARDING.md` §4 and §5. Short version:

- [ ] Other 3 team members work through `ONBOARDING.md` end-to-end on their laptops. Each completes the §5 checklist.
- [ ] Team agrees on Phase-1 agent ownership in `ONBOARDING.md` §4.2:
  - RA-001 Alert Triage — _assign_
  - RA-003 Auto-Ticketing — _assign_
  - RA-005 Notification Router — _assign_
  - RA-007 Log Correlation — _assign_
- [ ] Each owner: read their catalog row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`, write `agents/<phase>-<id>-<slug>/README.md` + 5 hand-written `evals/golden.json` cases — **before** writing prompt code.
- [ ] Provision Anthropic API keys per developer; verify with the `aiops.llm.complete` smoke command in `ONBOARDING.md` §5.
- [ ] Set up free-tier accounts: ServiceNow PDI, PagerDuty developer, GitHub.

### Useful commands cheat sheet (for the next session)

```powershell
# Bring up the demo (idempotent)
.\infra\bootstrap.ps1

# Port-forward (separate window)
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080

# Smoke test the platform
$env:AIOPS_LLM_PROVIDER='stub'; uv run pytest

# Trigger / clear failures
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear

# Eval harness
uv run python -m evals.harness --ci --min-pass-rate 0.85

# Real LLM smoke (with key set)
$env:AIOPS_LLM_PROVIDER='anthropic'; uv run python -c "from aiops.llm import Message, complete; print(complete([Message('user','say hi')]).text)"

# Tear down (keeps Rancher / k3s running)
.\infra\teardown.ps1
```

---

## How to keep this file useful

- **One section per working session.** Date the heading. Newest at top.
- **Cover four things:** what shipped, decisions, gotchas, what's next.
- **Be specific about state.** "Helm release at revision 5, status deployed" is useful; "things are working" isn't.
- If a gotcha bit you and is now fixed, write it up so the next person (or Claude) doesn't re-debug it.
- This file is committed to the repo. Keep it free of secrets.
