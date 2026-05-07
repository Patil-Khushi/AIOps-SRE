# `demo/failure_injection/` — one-command failure runner

POC guide §8.1 requires "≥3 failure scenarios that can be triggered with one command." This module is that. Phase 0 ships three:

| Scenario id | Mechanism | What breaks |
|---|---|---|
| `slow-product-catalog` | flagd | Product catalog responds slowly; checkout latency degrades |
| `kafka-queue-buildup` | flagd | Kafka consumer falls behind; downstream order processing delays |
| `currency-pod-kill` | kubectl | Currency pod terminated mid-request; cart and frontend see 5xx |

Two more (`network-partition`, `database-pool-exhaustion`) land in Phase 1 once Chaos Mesh is in the bootstrap.

## Usage

```powershell
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear
```

`--clear` resets every flagd-driven failure. `currency-pod-kill` self-heals because Kubernetes restarts the pod.

## Adding a new scenario

1. Drop a YAML file in `scenarios/` matching the schema below.
2. Add a truth file in `demo/truth_files/<id>.yaml` (use `template.yaml`).
3. If the new scenario uses a new flagd key, append it to `_flagd_clear()` in `inject.py` so `--clear` resets it.

### YAML schema

```yaml
id: <kebab-case-id>          # matches truth-file filename
title: <one-line summary>
description: |
  Multi-line. Why this scenario, what it tests, what NOT to claim from it.

mechanism: flagd | kubectl | chaos-mesh
flagd:                       # if mechanism: flagd
  flag_key: <flagd flag>
  variant: "on"
kubectl:                     # if mechanism: kubectl
  namespace: otel-demo
  selector: app.kubernetes.io/component=<service>
  action: delete-pod

expected_signals:            # informational; for the truth file to elaborate
  - prometheus_metric: <PromQL>
  - log_pattern: <regex>
  - trace_pattern: <description>

duration_seconds: 600
clears_on: <how it ends>
```

## Why this exists separately from Chaos Mesh

Chaos Mesh is the right answer for Phase 1+ chaos experiments (PRS-007 Chaos Orchestrator). Phase 0 needs *cheap* repeatable failures we can trigger from a Python script during a demo, without depending on Chaos Mesh being installed. Both end up coexisting.
