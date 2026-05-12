# infra/teardown.ps1 — uninstall the OTel demo from Rancher Desktop k3s.
# Leaves the Rancher Desktop cluster itself running (we don't own it).

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [switch]$KeepNamespace
)

$ErrorActionPreference = 'Stop'

Write-Host "==> Stopping any port-forward jobs started by infra/port-forward.ps1"
Get-Job -Name 'pf-*' -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job | Out-Null

$release = helm list -n $Namespace -q 2>$null
if ($release -split "`n" -contains 'otel-demo') {
    Write-Host "==> Uninstalling helm release 'otel-demo' from namespace '$Namespace'"
    helm uninstall otel-demo -n $Namespace
} else {
    Write-Host "    no helm release 'otel-demo' to uninstall"
}

if (-not $KeepNamespace) {
    $nsExists = $null
    $nsExists = kubectl get ns $Namespace -o name 2>$null
    if ($nsExists) {
        Write-Host "==> Deleting namespace '$Namespace'"
        kubectl delete ns $Namespace --wait=false
    }
}

Write-Host "Done."
