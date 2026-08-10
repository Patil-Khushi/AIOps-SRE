# Install the standalone observability stack into the `observability` namespace.
#
# Replaces the Prometheus / Grafana / Jaeger that used to arrive as subcharts of
# the opentelemetry-demo umbrella chart. That coupling is the whole reason the
# OTel Demo could not just be uninstalled — `helm uninstall otel-demo` would
# have removed the observability stack along with it.
#
# The OpenTelemetry Collector is NOT installed here any more. Its entire config
# was one otlp receiver forwarding to jaeger:4317, and Jaeger's allInOne accepts
# OTLP natively — so the app now exports straight to Jaeger and the hop is gone.
# This script uninstalls a leftover collector release if it finds one.
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
    helm repo update | Out-Null
}

kubectl create namespace $Namespace --dry-run=client -o yaml | kubectl apply -f - | Out-Null

Write-Host "==> Installing Jaeger (allInOne, in-memory, native OTLP on 4317/4318)"
helm upgrade --install jaeger jaegertracing/jaeger `
    --namespace $Namespace `
    --values (Join-Path $here 'jaeger-values.yaml') `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { Write-Error 'Jaeger install failed.' }

# The collector used to sit between the app and Jaeger. Removing it from this
# script is not enough on a cluster that already has it: the release would stay
# up, keep its Service resolvable, and quietly keep receiving spans from any pod
# whose ConfigMap had not been rolled yet — a split trace view that looks like
# random span loss. Uninstall it explicitly instead.
$otelRelease = helm list --namespace $Namespace --filter '^otel-collector$' --short 2>$null
if ($otelRelease) {
    Write-Host "==> Removing the OpenTelemetry Collector (superseded by direct OTLP to Jaeger)"
    helm uninstall otel-collector --namespace $Namespace | Out-Null
}

Write-Host "==> Installing Prometheus + Alertmanager"
helm upgrade --install prometheus prometheus-community/prometheus `
    --namespace $Namespace `
    --values (Join-Path $here 'prometheus-values.yaml') `
    --wait --timeout 8m
if ($LASTEXITCODE -ne 0) { Write-Error 'Prometheus install failed.' }

# Dashboards ship as a ConfigMap that grafana-values.yaml mounts and Grafana's
# file provisioner loads. This has to happen BEFORE the Grafana install so the
# mount exists on first boot — otherwise the pod starts with no dashboards and
# only picks them up on the next roll.
#
# It also has to happen on every run, not just the first: Grafana's storage is
# an emptyDir (persistence is disabled), so a restart wipes grafana.db and the
# provisioner rebuilds every dashboard from this ConfigMap. It is the only
# durable copy — a dashboard edited in the UI and not written back here is gone
# the next time the pod moves.
$dashboardDir = Join-Path $here 'dashboards'
if (Test-Path $dashboardDir) {
    $dashboards = Get-ChildItem -Path $dashboardDir -Filter '*.json' -File
    if ($dashboards) {
        Write-Host "==> Applying dashboards ConfigMap ($($dashboards.Count) dashboard(s))"
        $fromFileArgs = $dashboards | ForEach-Object { '--from-file'; "$($_.Name)=$($_.FullName)" }
        kubectl create configmap grafana-dashboard-ecommerce-db `
            --namespace $Namespace @fromFileArgs `
            --dry-run=client -o yaml | kubectl apply -f - | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Error 'Dashboard ConfigMap apply failed.' }
    }
    else {
        Write-Host "==> No dashboards in $dashboardDir - skipping ConfigMap"
    }
}

Write-Host "==> Installing Grafana"
helm upgrade --install grafana grafana/grafana `
    --namespace $Namespace `
    --values (Join-Path $here 'grafana-values.yaml') `
    --wait --timeout 5m
if ($LASTEXITCODE -ne 0) { Write-Error 'Grafana install failed.' }

# helm does not roll the Deployment when only a mounted ConfigMap changed, so an
# edited dashboard would sit in the ConfigMap unread until something else
# restarted the pod. The kubelet does sync the mounted file within ~60s and the
# provider re-reads every 30s, so this is belt-and-braces for the first install
# path rather than a correctness requirement on re-runs.
kubectl rollout status deploy/grafana --namespace $Namespace --timeout=3m | Out-Null

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
