# infra/bootstrap.ps1
# One-shot Phase-0 bootstrap. Idempotent — re-run any time.
#
# What it does:
#   1. Verifies prerequisites (Docker, kind, kubectl, helm).
#   2. Creates a kind cluster named aiops-poc (skip if already exists).
#   3. Adds the OpenTelemetry Helm repo.
#   4. Installs the OTel demo into the otel-demo namespace using demo/otel-demo/values.yaml.
#   5. Waits for the frontend pod to be Ready.
#   6. Prints the URLs to open.

[CmdletBinding()]
param(
    [string]$ClusterName = 'aiops-poc',
    [string]$Namespace = 'otel-demo',
    [string]$ChartVersion = ''   # empty = latest
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "Required tool '$name' not on PATH. Install it before running bootstrap."
    }
}

Write-Host "==> Checking prerequisites" -ForegroundColor Cyan
Require-Tool docker
Require-Tool kind
Require-Tool kubectl
Require-Tool helm

Write-Host "==> Verifying Docker is running"
docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker is not running. Start Docker Desktop and re-run.' }

Write-Host "==> Ensuring kind cluster '$ClusterName'"
$existing = kind get clusters 2>$null
if ($existing -split "`n" -contains $ClusterName) {
    Write-Host "    cluster already exists; skipping create"
} else {
    kind create cluster --name $ClusterName --config (Join-Path $repoRoot 'infra/kind-config.yaml')
    if ($LASTEXITCODE -ne 0) { Write-Error 'kind create cluster failed.' }
}

kubectl cluster-info --context "kind-$ClusterName" | Out-Null

Write-Host "==> Adding OpenTelemetry Helm repo"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>$null | Out-Null
helm repo update | Out-Null

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

# Patch frontend / grafana / jaeger to NodePort so the kind extraPortMappings work.
function Set-NodePort($svc, $nodePort, $targetPort) {
    Write-Host "==> Exposing $svc as NodePort $nodePort"
    kubectl -n $Namespace patch service $svc --type='json' -p `
        "[{`"op`":`"replace`",`"path`":`"/spec/type`",`"value`":`"NodePort`"},
          {`"op`":`"replace`",`"path`":`"/spec/ports/0/nodePort`",`"value`":$nodePort},
          {`"op`":`"replace`",`"path`":`"/spec/ports/0/targetPort`",`"value`":$targetPort}]" 2>$null
}

# Names vary slightly across chart versions — try both layouts and ignore failures.
Set-NodePort 'otel-demo-frontendproxy' 30080 8080
Set-NodePort 'otel-demo-grafana'        30300 3000
Set-NodePort 'otel-demo-jaeger-query'   30168 16686

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host "    Frontend:  http://localhost:8080"
Write-Host "    Grafana:   http://localhost:3000   (admin / admin by default)"
Write-Host "    Jaeger:    http://localhost:16686"
Write-Host ""
Write-Host "Trigger a failure:"
Write-Host "    uv run python -m demo.failure_injection.inject --list"
Write-Host "    uv run python -m demo.failure_injection.inject slow-product-catalog"
Write-Host ""
Write-Host "Tear down:  ./infra/teardown.ps1"
