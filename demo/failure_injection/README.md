# `demo/failure_injection/` — one-command failure runner

This module exposes a CLI that injects failures against the running OTel demo
cluster. The Python entry point lives here (`inject.py`); **the scenario
catalog does not** — it lives one folder up at `demo/scenarios/`, which is
the single source of truth shared with the dashboard.

See **[`demo/scenarios/README.md`](../scenarios/README.md)** for the YAML
schema, the full list of scenarios, and how to add new ones.

## Usage

```powershell
uv run --extra ui python -m demo.failure_injection.inject --list
uv run --extra ui python -m demo.failure_injection.inject slow-product-catalog
uv run --extra ui python -m demo.failure_injection.inject --clear
```

`--list` shows only the scenarios that declare a `mechanism` (the ones this
CLI can actually run). UI-only catalog entries are silently skipped.

`--clear` resets every flagd-driven scenario back to off. `kubectl`-driven
scenarios self-heal because Kubernetes restarts the pod.

## Mechanisms supported

| `mechanism:` value | What `inject.py` does |
|---|---|
| `flagd` | POST to flagd's HTTP endpoint inside the cluster (port-forwarded) |
| `kubectl` | Delete a pod by selector |
| `chaos-mesh` | *Phase 1+* — apply a Chaos Mesh experiment CRD |

## Why this exists separately from Chaos Mesh

Chaos Mesh is the right answer for Phase 1+ chaos experiments (PRS-007 Chaos
Orchestrator). Phase 0 needs *cheap* repeatable failures we can trigger from
a Python script during a demo, without depending on Chaos Mesh being
installed. Both end up coexisting — Chaos Mesh just becomes another value
in the `mechanism:` field of the shared schema.
