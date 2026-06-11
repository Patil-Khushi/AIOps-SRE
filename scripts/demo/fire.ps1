# Fire one fixture through the triage API and pretty-print the routing decision.
#
# Usage:
#   .\scripts\demo\fire.ps1 severity_hint_critical_direct
#   .\scripts\demo\fire.ps1 payment_cpu_spike
#   .\scripts\demo\fire.ps1 -List
#
# Requires: uvicorn server running on http://127.0.0.1:8765

[CmdletBinding(DefaultParameterSetName = "Fire")]
param(
    [Parameter(ParameterSetName = "Fire", Position = 0, Mandatory = $true)]
    [string]$Fixture,

    [Parameter(ParameterSetName = "List")]
    [switch]$List,

    [string]$BaseUrl = "http://127.0.0.1:8765"
)

$ErrorActionPreference = "Stop"

if ($List) {
    try {
        $data = Invoke-RestMethod -Uri "$BaseUrl/api/fixtures" -TimeoutSec 5
    } catch {
        Write-Host "ERROR: cannot reach $BaseUrl/api/fixtures" -ForegroundColor Red
        Write-Host "Is the server running? .\start.ps1" -ForegroundColor Yellow
        exit 1
    }
    Write-Host ""
    Write-Host "Available fixtures:" -ForegroundColor Cyan
    foreach ($c in $data.cases) {
        Write-Host ("  {0,-42} {1}" -f $c.id, $c.description)
    }
    Write-Host ""
    Write-Host "Fire one with:  .\scripts\demo\fire.ps1 <fixture_id>"
    exit 0
}

Write-Host ""
Write-Host ">>> Firing: $Fixture" -ForegroundColor Cyan
try {
    $result = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/triage/fixture/$Fixture" -TimeoutSec 30
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Hint: list fixtures with .\scripts\demo\fire.ps1 -List" -ForegroundColor Yellow
    exit 1
}

# The endpoint returns {verdict, ticket, classification, ...} now. Pull the
# verdict out so the printout looks the same as the older flat-verdict
# version.
$verdict = if ($result.PSObject.Properties.Match('verdict').Count) { $result.verdict } else { $result }

Write-Host ""
Write-Host "Triage verdict:" -ForegroundColor Green
Write-Host "  severity         : $($verdict.severity)"
Write-Host "  affected_service : $($verdict.affected_service)"
Write-Host "  assigned_team    : $($verdict.assigned_team)"
Write-Host "  assigned_engineer: $($verdict.assigned_engineer)"
Write-Host "  alert_summary    : $($verdict.alert_summary)"
Write-Host ""
Write-Host "RA-005 will have routed this through the chatops seam (Slack + dashboard + audit log)."
Write-Host "  Check: #aiops-test on Slack, AND http://localhost:8765/dashboard/notifications"
Write-Host ""
