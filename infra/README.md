# `infra/` — local cluster bootstrap

Phase 0 runs everything against **Rancher Desktop's bundled k3s cluster** + the upstream OpenTelemetry Demo Helm chart. No Docker, no cloud, no real customer credentials.

> **Why Rancher Desktop and not kind / Docker Desktop?** Org policy bans Docker on dev laptops. Rancher Desktop ships its own container runtime (containerd or dockerd-mode) and a single-node k3s cluster that's plenty for the POC. Install once with admin rights, then everything is no-admin.

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| Rancher Desktop | Local k3s cluster + container runtime | <https://rancherdesktop.io/> (one-time admin install) |
| kubectl | Kubernetes CLI | `winget install --scope user --id Kubernetes.kubectl` |
| helm | Kubernetes package manager | `winget install --scope user Helm.Helm` |
| uv (optional) | Python dep mgr the repo uses | `winget install --scope user astral-sh.uv` |
| k6 (optional) | Load generator | `winget install --scope user k6.k6` |

After installing, open Rancher Desktop, go to **Settings → Kubernetes**, and make sure Kubernetes is enabled (it is by default). Allocate at least **6 GB RAM** to the VM (Settings → Virtual Machine) — the OTel demo uses ~3.5 GB inside that.

**Why install winget kubectl even though Rancher Desktop ships one:** Rancher Desktop's kubectl is a `kuberlr` wrapper that rejects standard flags (`-n`, `--client`) when invoked from Python `subprocess`. The repo's failure-injection script resolves to the winget kubectl automatically when both are present.

PowerShell-first; the bash variants (`bootstrap.sh`, `teardown.sh`) match the PowerShell ones step-for-step.

## Up

```powershell
.\infra\bootstrap.ps1            # idempotent; takes ~10 minutes the first time
```

What this does:

1. Verifies `kubectl` and `helm` are on PATH and Rancher Desktop's k3s API is reachable.
2. Pins the kube context to `rancher-desktop` (so a stray context doesn't catch the install).
3. Adds the `open-telemetry` Helm repo and installs the OTel demo chart with our values.
4. Waits for the demo frontend pod to be Ready.

Then open a second PowerShell window and start the port-forward (leave it running for the dev session):

```powershell
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
```

That single forward exposes the frontend, Grafana, and Jaeger UI through the demo's built-in reverse proxy:

| URL | What |
|---|---|
| <http://localhost:8080/> | OTel demo frontend (the "store") |
| <http://localhost:8080/grafana/> | Grafana (admin / admin by default) |
| <http://localhost:8080/jaeger/ui/> | Jaeger UI |

If you'd rather port-forward Prometheus and Jaeger directly (e.g. to wire a Prometheus client lib to `:9090`), `infra/port-forward.ps1` starts both as background jobs.

## Down

```powershell
.\infra\teardown.ps1
```

This uninstalls the `otel-demo` helm release and deletes the `otel-demo` namespace. **It does not stop or reset the Rancher Desktop cluster itself** — that's the user's machine, not ours to delete. Use `-KeepNamespace` to keep the namespace if you want to re-install in place.

## Re-running

`bootstrap.ps1` is idempotent: re-run after editing `demo/otel-demo/values.yaml` to apply changes. `helm upgrade --install` does the right thing.

## Troubleshooting

**`Cannot reach the Kubernetes API`** — Rancher Desktop isn't running, or k3s is still starting. Open Rancher Desktop, wait for the tray icon to show *Kubernetes: running*, then re-run.

**`flagd-config conflict with "kubectl-patch"`** during a re-install — a previous failure-injection patch poisoned the field-manager. Easiest fix: `.\infra\teardown.ps1; .\infra\bootstrap.ps1`. Or manually `kubectl delete configmap flagd-config -n otel-demo && helm rollback otel-demo -n otel-demo`.

**Existing PowerShell window doesn't see newly installed `kubectl`/`helm`** — winget updates user PATH but already-open shells don't pick it up. Either reopen PowerShell or refresh in-session:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
```

## What's NOT in Phase 0 bootstrap

- Chaos Mesh (Phase 1+; needed for harder failure scenarios)
- ServiceNow PDI / Jira / PagerDuty wiring (Phase 1+; runs against external dev tenants, not the cluster)
- An LLM gateway sidecar (Phase 0 reads `ANTHROPIC_API_KEY` directly from the host)
- OPA running as a service (Phase 2; Phase 0 evaluates rules in-process)
