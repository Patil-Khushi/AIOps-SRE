# infra/bootstrap.ps1
# One-shot Phase-0 bootstrap for Rancher Desktop. Idempotent — re-run any time.
#
# What it does:
#   1. Verifies prerequisites (kubectl, helm) and that Rancher Desktop's k3s is reachable.
#   2. Adds the OpenTelemetry Helm repo.
#   3. Installs the OTel demo into the otel-demo namespace using demo/otel-demo/values.yaml.
#   4. Waits for the frontend pod to be Ready.
#   5. Prints the single port-forward command that exposes frontend / grafana / jaeger.

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [string]$ChartVersion = '',                # empty = latest
    [string]$Context     = 'rancher-desktop'   # set to '' to use current context
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "Required tool '$name' not on PATH. See ONBOARDING.md section 2."
    }
}

Write-Host "==> Checking prerequisites" -ForegroundColor Cyan
Require-Tool kubectl
Require-Tool helm

# Pin the kube context so we don't accidentally install into a stray cluster.
if ($Context) {
    $current = (kubectl config current-context 2>$null).Trim()
    if ($current -ne $Context) {
        Write-Host "    switching kube context: $current -> $Context"
        kubectl config use-context $Context | Out-Null
    }
}

Write-Host "==> Verifying Rancher Desktop k3s is reachable"
# Probe the API with a short timeout. We intentionally swap ErrorActionPreference around the call
# because PowerShell 5.1 turns a native exe's stderr into a NativeCommandError under 'Stop',
# which would terminate before our friendly message below.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$null = & kubectl version --request-timeout=5s 2>&1
$probeExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

if ($probeExit -ne 0) {
    Write-Host ""
    Write-Host "Cannot reach the Kubernetes API." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Start Rancher Desktop from the Start menu and wait for the tray icon"
    Write-Host "  to show 'Kubernetes: running' (usually 30-60 seconds). Then re-run"
    Write-Host "  this script."
    Write-Host ""
    Write-Host "  If Rancher Desktop is already running, check that"
    Write-Host "  'kubectl config current-context' returns 'rancher-desktop' and that"
    Write-Host "  k3s is enabled (Settings -> Kubernetes)."
    exit 1
}

Write-Host "==> Adding OpenTelemetry Helm repo"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>$null | Out-Null
helm repo update | Out-Null

# Skip the install if the demo is already healthy — running helm upgrade after
# inject.py has patched flagd-config triggers a server-side-apply conflict
# (kubectl-patch owns .data.demo.flagd.json). Idempotency wins; pass -Force to
# override (you'll need to clear flagd patches first or run teardown.ps1).
$alreadyHealthy = $false
if (-not $Force) {
    $existing = helm -n $Namespace ls --filter '^otel-demo$' -o json 2>$null
    if ($existing -and $existing.Trim() -ne '[]') {
        $status = ($existing | ConvertFrom-Json)[0].status
        if ($status -eq 'deployed') {
            $frontendReady = (kubectl -n $Namespace get pod -l app.kubernetes.io/component=frontend-proxy `
                -o "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}" 2>$null)
            if ($frontendReady -eq 'True') {
                $alreadyHealthy = $true
            }
        }
    }
}

if ($alreadyHealthy) {
    Write-Host "==> otel-demo already deployed and frontend-proxy is Ready; skipping helm install."
    Write-Host "    To force a re-install: .\infra\bootstrap.ps1 -Force"
    Write-Host "    Or wipe and reinstall:  .\infra\teardown.ps1; .\infra\bootstrap.ps1"
} else {
    Write-Host "==> Installing OTel demo into namespace '$Namespace'"
    $valuesPath = Join-Path $repoRoot 'demo/otel-demo/values.yaml'
    $helmArgs = @(
        'upgrade', '--install', 'otel-demo', 'open-telemetry/opentelemetry-demo',
        '--namespace', $Namespace, '--create-namespace',
        '--values', $valuesPath,
        '--wait', '--timeout', '15m'
    )
    if ($ChartVersion) { $helmArgs += @('--version', $ChartVersion) }
    helm @helmArgs
    if ($LASTEXITCODE -ne 0) { Write-Error 'helm install failed.' }

Write-Host "==> Waiting for frontend pod to be Ready"
kubectl -n $Namespace wait --for=condition=Ready pod -l app.kubernetes.io/component=frontend --timeout=300s

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host ""
Write-Host "Open a separate PowerShell window and run:"
Write-Host "    kubectl -n $Namespace port-forward svc/frontend-proxy 8080:8080"
Write-Host ""
Write-Host "Then browse:"
Write-Host "    Frontend:  http://localhost:8080/"
Write-Host "    Grafana:   http://localhost:8080/grafana/   (admin / admin by default)"
Write-Host "    Jaeger:    http://localhost:8080/jaeger/ui/"
Write-Host ""
Write-Host "Trigger a failure:"
Write-Host "  uv run python -m demo.failure_injection.inject --list"
Write-Host "  uv run python -m demo.failure_injection.inject slow-product-catalog"
Write-Host ""
Write-Host "Tear down:  .\infra\teardown.ps1"
