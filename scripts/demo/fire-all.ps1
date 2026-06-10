# Fire one fixture per severity (Sev-1/2/3/4) through the triage API.
#
# Usage (from the AIops/ folder):
#   .\scripts\demo\fire-all.ps1
#
# Requires: uvicorn server running on http://127.0.0.1:8765

param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [int]$DelaySeconds = 1
)

$ErrorActionPreference = "Stop"

$Fixtures = @(
    @{ id = "severity_hint_critical_direct";  caption = "CRITICAL - Sev-1, pages on-call regardless of hour" }
    @{ id = "severity_hint_p2_high";          caption = "IMPORTANT - Sev-2, chats team in business hours / pages after-hours" }
    @{ id = "ad_low_traffic_early_warning";   caption = "MINOR - Sev-3, daytime triage channel, no mention" }
    @{ id = "sev_4_below_threshold_boundary"; caption = "NOISE - Sev-4, alerts-noise bucket, log only" }
)

Write-Host ""
Write-Host "RA-005 demo - firing 4 fixtures (one per severity)" -ForegroundColor Cyan
Write-Host "Watch:  #aiops-test on Slack  AND  http://localhost:8765/dashboard/notifications" -ForegroundColor Cyan
Write-Host ("-" * 70)

try {
    $null = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 3 -ErrorAction Stop
} catch {
    Write-Host ""
    Write-Host "ERROR: cannot reach $BaseUrl/api/health" -ForegroundColor Red
    Write-Host "Start the server first:  .\start.ps1" -ForegroundColor Yellow
    exit 1
}

foreach ($f in $Fixtures) {
    Write-Host ""
    Write-Host ">>> Firing: $($f.id)" -ForegroundColor Cyan
    Write-Host "    $($f.caption)" -ForegroundColor DarkGray
    try {
        $result = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/triage/fixture/$($f.id)" -TimeoutSec 30
        $verdict = if ($result.PSObject.Properties.Match('verdict').Count) { $result.verdict } else { $result }
        Write-Host ("    Triage: severity={0}, service={1}, team={2}, engineer={3}" -f `
            $verdict.severity, $verdict.affected_service, $verdict.assigned_team, $verdict.assigned_engineer) -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
        continue
    }
    Start-Sleep -Seconds $DelaySeconds
}

Write-Host ""
Write-Host ("-" * 70)
Write-Host "Done. Four cards should now be on the dashboard + four messages in #aiops-test." -ForegroundColor Green
Write-Host "Audit log:  Get-Content demo\audit\chatops.jsonl"
Write-Host ""
