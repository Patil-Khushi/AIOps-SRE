# Install the standalone observability stack into the `observability` namespace.
#
# Replaces the Prometheus / Grafana / Jaeger / Collector that used to arrive as
# subcharts of the opentelemetry-demo umbrella chart. That coupling is the whole
# reason the OTel Demo could not just be uninstalled — `helm uninstall otel-demo`
# would have removed the observability stack along with it.
#
# Loki is NOT installed here. It is already its own release and stays put:
# reinstalling would drop its PVC and every log line collected so far.
#
# Idempotent — `helm upgrade --install` throughout, safe to re-run.
#
#   .\infra\observability\install.ps1
#   .\infra\observability\install.ps1 -Namespace obs      # non-default namespace

param(
    [string]$Namespace = 'observability',
    [switch]$SkipRepoUpdate
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "$name not found on PATH."
    }
}
Require-Tool kubectl
Require-Tool helm

Write-Host "==> Verifying cluster is reachable"
kubectl cluster-info --request-timeout=10s | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error 'Cannot reach the Kubernetes API. Is Rancher Desktop running?' }

if (-not $SkipRepoUpdate) {
    Write-Host "==> Adding/updating helm repos"
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>$null | Out-Null
    helm repo add grafana              https://grafana.github.io/helm-charts            2>$null | Out-Null
    helm repo add jaegertracing        https://jaegertracing.github.io/helm-charts      2>$null | Out-Null
    helm repo add open-telemetry       https://open-telemetry.github.io/opentelemetry-helm-charts 2>$null | Out-Null
    helm repo update | Out-Null
}

kubectl create namespace $Namespace --dry-run=client -o yaml | kubectl apply -f - | Out-Null

# Order matters: Jaeger first, because the Collector's traces pipeline exports
# to jaeger-collector and logs export failures until that Service resolves.
Write-Host "==> Installing Jaeger (allInOne, in-memory)"
helm upgrade --install jaeger jaegertracing/jaeger `
    --namespace $Namespace `
    --values (Join-Path $here 'jaeger-values.yaml') `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { Write-Error 'Jaeger install failed.' }

Write-Host "==> Installing OpenTelemetry Collector"
helm upgrade --install otel-collector open-telemetry/opentelemetry-collector `
    --namespace $Namespace `
    --values (Join-Path $here 'otel-collector-values.yaml') `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { Write-Error 'Collector install failed.' }

Write-Host "==> Installing Prometheus + Alertmanager"
helm upgrade --install prometheus prometheus-community/prometheus `
    --namespace $Namespace `
    --values (Join-Path $here 'prometheus-values.yaml') `
    --wait --timeout 8m
if ($LASTEXITCODE -ne 0) { Write-Error 'Prometheus install failed.' }

Write-Host "==> Installing Grafana"
helm upgrade --install grafana grafana/grafana `
    --namespace $Namespace `
    --values (Join-Path $here 'grafana-values.yaml') `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { Write-Error 'Grafana install failed.' }

Write-Host ''
Write-Host 'Installed. Port-forward to reach the UIs:'
Write-Host "    kubectl -n $Namespace port-forward svc/prometheus-server 9090:80"
Write-Host "    kubectl -n $Namespace port-forward svc/grafana 3001:80"
Write-Host "    kubectl -n $Namespace port-forward svc/jaeger 16686:16686"
Write-Host ''
Write-Host 'Then point the agents at them (.env):'
Write-Host '    AIOPS_PROMETHEUS_URL=http://localhost:9090'
Write-Host '    AIOPS_JAEGER_URL=http://localhost:16686'
Write-Host '    AIOPS_JAEGER_API_PREFIX=            # empty! this Jaeger serves /api at the root'
Write-Host '    AIOPS_GRAFANA_URL=http://localhost:3001'
