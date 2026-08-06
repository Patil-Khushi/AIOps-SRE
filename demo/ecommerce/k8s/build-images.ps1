# Build the five ecommerce images into the local Docker daemon.
#
# No registry and no push: Rancher Desktop's k3s uses dockerd as its container
# runtime, so images built here are immediately visible to the cluster. Verify
# with:  kubectl get node -o jsonpath='{..containerRuntimeVersion}'
# If that ever reports containerd:// instead of docker://, this approach stops
# working and the images need `nerdctl --namespace k8s.io build` or a registry.
#
# Usage:  .\k8s\build-images.ps1            (from demo/ecommerce)
#         .\k8s\build-images.ps1 -Tag v2    (custom tag; update the manifests)

param(
    [string]$Tag = 'dev'
)

$ErrorActionPreference = 'Stop'

# Resolve demo/ecommerce regardless of where this is invoked from.
$root = Split-Path -Parent $PSScriptRoot

$services = @(
    'user-service',
    'order-service',
    'payment-service',
    'mock-payment-gateway',
    'frontend'
)

foreach ($svc in $services) {
    $context = Join-Path $root $svc
    if (-not (Test-Path $context)) {
        Write-Error "Build context not found: $context"
    }
    Write-Host "==> Building ecommerce/${svc}:$Tag"
    docker build -t "ecommerce/${svc}:$Tag" $context
    if ($LASTEXITCODE -ne 0) { Write-Error "Build failed for $svc" }
}

Write-Host ''
Write-Host 'Built images:'
docker images --filter 'reference=ecommerce/*' --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
