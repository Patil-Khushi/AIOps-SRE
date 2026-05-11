# Tear down the demo's background jobs (port-forwards + UI server).
# Leaves the k3d cluster running. To stop the cluster too: k3d cluster stop aiops

$jobs = Get-Job -Name 'pf-*' -ErrorAction SilentlyContinue
if (-not $jobs) {
    Write-Host "no pf-* jobs running."
} else {
    foreach ($j in $jobs) { Write-Host "  stopping $($j.Name) (id $($j.Id))" }
    $jobs | Stop-Job -PassThru | Remove-Job | Out-Null
    Write-Host "stopped." -ForegroundColor Green
}
Write-Host ''
Write-Host 'Cluster is still running. To stop it too:  k3d cluster stop aiops'
