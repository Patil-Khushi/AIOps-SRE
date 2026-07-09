# Backend port-forwards for the OTel demo (Prometheus 9090, Jaeger 16686).
# Run once per dev session. Stops any previous forwards started by this script first.
# Leave the frontend-proxy port-forward (8080) running separately.

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo'
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    $alt = "$env:LOCALAPPDATA\Programs\kubectl"
    if (Test-Path "$alt\kubectl.exe") {
        $env:Path = "$alt;$env:Path"
    } else {
        throw 'kubectl not found on PATH and not at $env:LOCALAPPDATA\Programs\kubectl\kubectl.exe'
    }
}

$forwards = @(
    @{ Name = 'prometheus'; Svc = 'svc/prometheus'; LocalPort = 9090;  RemotePort = 9090  }
    @{ Name = 'jaeger';     Svc = 'svc/jaeger';     LocalPort = 16686; RemotePort = 16686 }
    @{ Name = 'loki';       Svc = 'svc/loki';       LocalPort = 3100;  RemotePort = 3100  }
)

Get-Job -Name 'pf-*' -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job | Out-Null

foreach ($fwd in $forwards) {
    $job = Start-Job -Name "pf-$($fwd.Name)" -ScriptBlock {
        param($ns, $svc, $local, $remote)
        kubectl port-forward -n $ns $svc "${local}:${remote}"
    } -ArgumentList $Namespace, $fwd.Svc, $fwd.LocalPort, $fwd.RemotePort

    Write-Host ("Started {0,-12} -> http://localhost:{1}  (job {2})" -f $fwd.Name, $fwd.LocalPort, $job.Id)
}

Write-Host ''
Write-Host 'Backends:'
Write-Host '  Prometheus  http://localhost:9090'
Write-Host '  Jaeger      http://localhost:16686'
Write-Host '  Loki        http://localhost:3100'
Write-Host ''
Write-Host "Manage: Get-Job -Name 'pf-*'           # see status"
Write-Host "        Get-Job -Name 'pf-*' | Receive-Job -Keep   # tail logs"
Write-Host "        Get-Job -Name 'pf-*' | Stop-Job -PassThru | Remove-Job   # stop"
