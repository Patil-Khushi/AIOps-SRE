# `demo/scenarios/` — failure-scenario catalog (single source of truth)

One YAML file per failure scenario. **This is the only place scenarios live.**
Both consumers read this folder:

| Consumer | What it does |
|---|---|
| `demo/ui/server.py` | Reads every file at startup; exposes the catalog via `/api/scenarios`; the React dashboard's Overview page renders the Inject/Reset buttons. |
| `demo/failure_injection/inject.py` | Reads every file at startup; the CLI `--list` and runnable scenarios are filtered to files that declare a `mechanism`. |

POC guide §8.1: ≥3 scenarios triggerable with one command. **CLAUDE.md non-negotiable #8:** every scenario ships with a paired truth file at `demo/truth_files/<id>.yaml`. Enforced by `tests/test_smoke.py::test_every_scenario_has_a_truth_file`.

## Two flavours — pick the one that fits

Both flavours coexist in this folder. The dashboard reads both; the CLI only runs files with a `mechanism` block.

### Minimal (UI-only descriptor)

Use for scenarios driven by simple flag toggles where the UI just needs metadata. The CLI silently skips these — they don't define how to inject the failure programmatically.

```yaml
id: payment_failure              # snake_case; must equal filename stem
category: errors                 # errors | latency | capacity | infra (drives UI grouping)
flag: paymentFailure             # flagd flag name (must exist in demo/otel-demo/values.yaml)
variant_on: "100%"               # optional — defaults to "on"
alert: PaymentErrorRateHigh      # Prometheus alert rule expected to fire
service: payment                 # OTel demo service the alert reads
title: Payment failure (HTTP 500s)
description: Payment service rejects every charge with a 5xx error.
eta_seconds: 90                  # how long before the alert fires after injection
```

### Extended (CLI-runnable)

Use for scenarios you also want to run from `inject.py`. Adds a `mechanism` block plus optional signal expectations.

```yaml
id: slow-product-catalog         # kebab- or snake_case both OK; must equal filename stem
title: Product catalog service responds slowly under load
description: |
  Multi-line. Why this scenario, what it tests, what NOT to claim from it.

mechanism: flagd                 # flagd | kubectl | chaos-mesh (Phase 1+)
flagd:                           # required when mechanism: flagd
  flag_key: productCatalogFailure
  variant: "on"
kubectl:                         # required when mechanism: kubectl
  namespace: otel-demo
  selector: app.kubernetes.io/component=<service>
  action: delete-pod

expected_signals:                # informational, for the truth file to elaborate
  - prometheus_metric: <PromQL>
  - log_pattern: <regex>
  - trace_pattern: <description>

duration_seconds: 600
clears_on: failure_injection.inject --clear
```

## Adding a new scenario

1. Pick a unique `id`. Use `snake_case` for new UI-only scenarios; kebab-case is OK for CLI-runnable scenarios that keep their legacy ids.
2. Confirm the matching flagd flag exists in `demo/otel-demo/values.yaml` (or add it).
3. If the scenario needs an alert, add or reuse a rule under `prometheus.serverFiles.alerting_rules.yml` in the same file.
4. Drop a YAML in this folder using one of the schemas above.
5. **Required:** add a paired truth file at `demo/truth_files/<id>.yaml` — copy `demo/truth_files/template.yaml` as a starting point.
6. Restart the demo server (`.\stop.ps1; .\start.ps1`). The new scenario auto-appears in the Overview page; `inject.py --list` picks it up if it has a `mechanism` block.

## Using the CLI runner

```powershell
uv run --extra ui python -m demo.failure_injection.inject --list
uv run --extra ui python -m demo.failure_injection.inject slow-product-catalog
uv run --extra ui python -m demo.failure_injection.inject --clear
```

`--clear` resets every flagd-driven failure. `kubectl`-driven scenarios (e.g. pod-kill) self-heal because Kubernetes restarts the pod.

## Why two flavours?

The dashboard needs lightweight catalog rows (12 of those today). The CLI needs the same scenarios plus explicit "how to inject" wiring (3 of those today). Combining them in one folder with optional fields keeps the catalog single-sourced without forcing every UI row to grow a full chaos-injection spec.

## Why not just Chaos Mesh?

Chaos Mesh is the right answer for Phase 1+ chaos experiments (PRS-007 Chaos Orchestrator). Phase 0 needs *cheap* repeatable failures we can trigger from a Python script during a demo, without depending on Chaos Mesh being installed. Both end up coexisting — Chaos Mesh becomes one of the `mechanism:` values in the schema above.
