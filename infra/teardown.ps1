# infra/teardown.ps1 — delete the kind cluster.

[CmdletBinding()]
param([string]$ClusterName = 'aiops-poc')

$ErrorActionPreference = 'Stop'

$existing = kind get clusters 2>$null
if ($existing -split "`n" -contains $ClusterName) {
    Write-Host "Deleting kind cluster '$ClusterName'..."
    kind delete cluster --name $ClusterName
    Write-Host "Done."
} else {
    Write-Host "No cluster named '$ClusterName' to delete."
}
