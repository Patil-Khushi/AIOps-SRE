# `infra/` — local cluster bootstrap

Phase 0 runs everything against a local kind cluster + the upstream OpenTelemetry Demo Helm chart. No cloud, no real customer credentials.

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| Docker Desktop | Container runtime kind sits on top of | <https://docs.docker.com/desktop/> |
| kind | Local Kubernetes cluster in Docker | `winget install Kubernetes.kind` |
| kubectl | Kubernetes CLI | `winget install Kubernetes.kubectl` |
| helm | Kubernetes package manager | `winget install Helm.Helm` |
| uv (optional) | Python dep mgr the repo uses | `winget install astral-sh.uv` |
| k6 (optional) | Load generator | `winget install k6.k6` |

PowerShell-first; the bash variants (`bootstrap.sh`, `teardown.sh`) match the PowerShell ones step-for-step.

## Up

```powershell
.\infra\bootstrap.ps1            # idempotent; takes ~10 minutes the first time
```

What this does:

1. Verifies Docker / kind / kubectl / helm are present and Docker is running.
2. Creates a 3-node kind cluster named `aiops-poc` (control-plane + 2 workers) with port mappings for 8080 / 3000 / 16686.
3. Adds the `open-telemetry` Helm repo and installs the OTel demo chart with our values.
4. Patches the frontend, Grafana, and Jaeger services to NodePort so the kind port mappings take effect.

After it finishes:

| URL | What |
|---|---|
| <http://localhost:8080> | OTel demo frontend (the "store") |
| <http://localhost:3000> | Grafana (admin / admin by default) |
| <http://localhost:16686> | Jaeger UI |

## Down

```powershell
.\infra\teardown.ps1
```

Deletes the kind cluster. Docker images and volumes can be cleaned with `docker system prune` afterwards if disk space matters.

## Re-running

`bootstrap.ps1` is idempotent: re-run after editing `demo/otel-demo/values.yaml` to apply changes. `helm upgrade --install` does the right thing.

## What's NOT in Phase 0 bootstrap

- Chaos Mesh (Phase 1+; needed for harder failure scenarios)
- ServiceNow PDI / Jira / PagerDuty wiring (Phase 1+; runs against external dev tenants, not the cluster)
- An LLM gateway sidecar (Phase 0 reads `ANTHROPIC_API_KEY` directly from the host)
- OPA running as a service (Phase 2; Phase 0 evaluates rules in-process)
