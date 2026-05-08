# `infra/` — local cluster bootstrap

Phase 0 runs everything against a local Kubernetes cluster + the upstream OpenTelemetry Demo Helm chart. **No cloud, no real customer credentials.**

The cluster itself is owned by **Rancher Desktop** (which ships k3s). These scripts assume Rancher Desktop is already installed and Kubernetes is enabled — see [`../ONBOARDING.md`](../ONBOARDING.md) sections 1 and 2 for the one-time machine setup.

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| Rancher Desktop | Local k3s + container runtime + kubectl + helm | `winget install --id SUSE.RancherDesktop` (one-time, admin) |
| kubectl | Real Kubernetes CLI (winget version) | `winget install --scope user --id Kubernetes.kubectl` |
| uv | Python dep manager | `winget install --scope user --id astral-sh.uv` |
| k6 (optional) | Load generator | `winget install --scope user --id k6.k6` |

> **Why install kubectl twice?** Rancher Desktop ships its own `kubectl.exe` that is actually a *kuberlr* wrapper. The wrapper rejects standard kubectl args under Python `subprocess` invocations (it works fine when called from PowerShell directly). The repo's failure-injection module auto-detects a real kubectl, but installing the winget version puts a real kubectl on PATH for ad-hoc use. See `ONBOARDING.md` §7 troubleshooting.

## Up

```powershell
.\infra\bootstrap.ps1            # idempotent; takes ~5–10 minutes the first time
```

What this does:

1. Verifies Rancher Desktop's `rancher-desktop` kubectl context exists and is reachable.
2. Adds the `open-telemetry` Helm repo.
3. Installs (or upgrades) the OTel demo into namespace `otel-demo` using `demo/otel-demo/values.yaml`.
4. Waits for the frontend-proxy pod to be Ready, then prints the URLs and the port-forward command.

After it finishes, run the port-forward command it prints:

```powershell
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
```

Then in your browser:

| URL | What |
|---|---|
| <http://localhost:8080/> | OTel demo storefront |
| <http://localhost:8080/grafana/> | Grafana (admin / admin) |
| <http://localhost:8080/jaeger/ui/> | Jaeger UI |
| <http://localhost:8080/loadgen/> | Load generator UI |
| <http://localhost:8080/feature/> | flagd / feature-flag UI |

## Trigger failures

```powershell
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear
```

## Down

```powershell
.\infra\teardown.ps1
```

Uninstalls the Helm release and deletes the namespace. **Does not stop Rancher Desktop / k3s itself** — close those from the Rancher Desktop UI if you want to free the RAM.

## Re-running

`bootstrap.ps1` is idempotent. Safe to re-run after editing `demo/otel-demo/values.yaml` — `helm upgrade --install` does the right thing.

## What's NOT in Phase 0 bootstrap

- Chaos Mesh (Phase 1+; needed for harder failure scenarios)
- ServiceNow PDI / Jira / PagerDuty wiring (Phase 1+; runs against external dev tenants, not the cluster)
- An LLM gateway sidecar (Phase 0 reads `ANTHROPIC_API_KEY` directly from the host)
- OPA running as a service (Phase 2; Phase 0 evaluates rules in-process)
