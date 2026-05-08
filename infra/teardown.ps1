# infra/teardown.ps1
# Uninstalls the OTel demo and namespace. Leaves Rancher Desktop / k3s alone —
# stopping the cluster itself is done from the Rancher Desktop UI.

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [switch]$KeepNamespace
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
    Write-Error "helm not on PATH."
}

$release = helm -n $Namespace ls -q 2>$null
if ($release -split "`n" -contains 'otel-demo') {
    Write-Host "Uninstalling otel-demo Helm release..."
    helm -n $Namespace uninstall otel-demo
} else {
    Write-Host "No otel-demo release in namespace '$Namespace' to uninstall."
}

if (-not $KeepNamespace) {
    Write-Host "Deleting namespace '$Namespace'..."
    kubectl delete namespace $Namespace --ignore-not-found=true
}

Write-Host ""
Write-Host "Done. Rancher Desktop / k3s itself is still running — stop it from"
Write-Host "the Rancher Desktop UI if you want to free the RAM."
