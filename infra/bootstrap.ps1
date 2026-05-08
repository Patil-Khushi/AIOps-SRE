# infra/bootstrap.ps1
# One-shot Phase-0 bootstrap for Rancher Desktop. Idempotent — re-run any time.
#
# Prerequisites (one-time, see ONBOARDING.md sections 1 and 2):
#   - WSL2 enabled
#   - Rancher Desktop installed and running with Kubernetes enabled (k3s)
#   - kubectl context 'rancher-desktop' present and Ready
#   - helm on PATH (Rancher Desktop ships it)
#
# What this script does:
#   1. Verifies prerequisites and the running cluster.
#   2. Adds the OpenTelemetry Helm repo.
#   3. Installs (or upgrades) the OTel demo into the otel-demo namespace
#      using demo/otel-demo/values.yaml.
#   4. Prints the single port-forward command + the URL list.
#
# Common gotcha: Rancher Desktop ships a kubectl wrapper (kuberlr) that
# breaks under Python subprocess. The failure-injection script auto-detects
# a real kubectl, so this is mostly a problem for ad-hoc Python scripts.
# `winget install --scope user --id Kubernetes.kubectl` puts a real kubectl
# on user PATH alongside the wrapper.

[CmdletBinding()]
param(
    [string]$Context      = 'rancher-desktop',
    [string]$Namespace    = 'otel-demo',
    [string]$ChartVersion = '',   # empty = latest
    [switch]$Force                # re-run helm upgrade even if already healthy
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

Write-Host "==> Verifying kubectl context '$Context'"
$contexts = kubectl config get-contexts -o name 2>$null
if (-not ($contexts -split "`n" -contains $Context)) {
    Write-Error "kubectl context '$Context' not found. Is Rancher Desktop running with Kubernetes enabled?"
}
kubectl config use-context $Context | Out-Null

Write-Host "==> Verifying cluster is reachable"
kubectl cluster-info --request-timeout=10s | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cannot reach the cluster. Open Rancher Desktop and wait for the green status indicator."
}
$nodeStatus = (kubectl get nodes -o jsonpath='{.items[0].status.conditions[?(@.type==\"Ready\")].status}')
if ($nodeStatus -ne 'True') {
    Write-Error "k3s node is not Ready (status=$nodeStatus). Wait a minute and re-run."
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

    Write-Host "==> Waiting for frontend-proxy pod to be Ready"
    kubectl -n $Namespace wait --for=condition=Ready pod -l app.kubernetes.io/component=frontend-proxy --timeout=300s
}

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host ""
Write-Host "The OTel demo exposes everything through one port-forward. Run this in a"
Write-Host "separate window (it will hold the foreground):"
Write-Host ""
Write-Host "  kubectl -n $Namespace port-forward svc/frontend-proxy 8080:8080" -ForegroundColor Yellow
Write-Host ""
Write-Host "Then open in your browser:"
Write-Host "  Webstore:        http://localhost:8080/"
Write-Host "  Grafana:         http://localhost:8080/grafana/    (admin / admin)"
Write-Host "  Jaeger UI:       http://localhost:8080/jaeger/ui/"
Write-Host "  Load generator:  http://localhost:8080/loadgen/"
Write-Host "  Feature flags:   http://localhost:8080/feature/"
Write-Host ""
Write-Host "Trigger a failure:"
Write-Host "  uv run python -m demo.failure_injection.inject --list"
Write-Host "  uv run python -m demo.failure_injection.inject slow-product-catalog"
Write-Host ""
Write-Host "Tear down:  ./infra/teardown.ps1"
