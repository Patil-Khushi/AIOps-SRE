# Tear down the demo's background jobs (port-forwards + UI server).
# Leaves Rancher Desktop's k3s running. To stop the cluster too, quit Rancher Desktop
# (or System tray -> Rancher Desktop -> Quit).

$jobs = Get-Job -Name 'pf-*' -ErrorAction SilentlyContinue
if (-not $jobs) {
    Write-Host "no pf-* jobs running."
} else {
    foreach ($j in $jobs) { Write-Host "  stopping $($j.Name) (id $($j.Id))" }
    $jobs | Stop-Job -PassThru | Remove-Job | Out-Null
    Write-Host "stopped." -ForegroundColor Green
}
Write-Host ''
Write-Host 'Cluster is still running. To stop k3s, quit Rancher Desktop from the tray.'
