# Bring the demo back to a known-clean baseline.
#
#   .\reset.ps1            # flagd flags off, chatops outbox truncated
#   .\reset.ps1 -Hard      # also wipe verdicts / classifications / tickets from state.db
#
# Safe to run before every rehearsal or live demo. Does NOT touch the cluster
# (no pod bouncing, no helm upgrade) and does NOT kill start.ps1's port-forwards.
#
# Note on the post-reset summary's "prom active alerts" line: Prometheus alert
# rules use rolling [2m] windows. An alert that was firing from a prior fault
# stays firing for ~2 min after you flip the flag off — that's lag, not a reset
# failure. The "scenarios" / "verdicts" lines are the source of truth for
# whether reset actually worked.

[CmdletBinding()]
param(
    [switch]$Hard,
    [string]$UiBase = 'http://localhost:8765'
)

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
$problems = @()

function _step($label, [scriptblock]$action) {
    Write-Host ("[reset] {0,-40} " -f $label) -NoNewline
    try { & $action; Write-Host "ok" -ForegroundColor Green }
    catch { Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red; $script:problems += $label }
}

# 1. Flip every UI-known scenario back to off (one atomic kubectl patch).
_step "POST /api/scenarios/reset-all" {
    $r = Invoke-RestMethod -Method POST "$UiBase/api/scenarios/reset-all" -TimeoutSec 15
    Write-Host -NoNewline ("(reset_count={0}) " -f $r.reset_count)
}

# 2. Belt-and-suspenders: clear any flag the UI doesn't know about.
_step "inject.py --clear" {
    Push-Location $repo
    try { uv run --quiet python -m demo.failure_injection.inject --clear 2>&1 | Out-Null }
    finally { Pop-Location }
}

# 3. Truncate the chatops audit log.
_step "truncate demo/audit/chatops.jsonl" {
    $p = Join-Path $repo 'demo\audit\chatops.jsonl'
    if (Test-Path $p) { Set-Content -Path $p -Value '' -Encoding utf8 -NoNewline }
}

# 4. Clear scratch eval-harness output files.
_step "remove .tmp_eval*.txt" {
    Get-ChildItem -Path $repo -Filter '.tmp_eval*.txt' -ErrorAction SilentlyContinue | Remove-Item -Force
}

# 5. Hard reset: wipe persisted agent state (verdicts, classifications, tickets,
#    notifications). Dashboard's "AI Reasoning" / history views become empty.
if ($Hard) {
    _step "DELETE FROM verdicts/* (state.db)" {
        $db = Join-Path $repo 'data\state.db'
        if (-not (Test-Path $db)) { throw "no DB at $db" }
        $py = @"
import sqlite3
con = sqlite3.connect(r'$db')
cur = con.cursor()
# delete child rows first to satisfy foreign keys
for t in ('notifications','tickets','classifications','verdicts'):
    try:
        cur.execute(f'DELETE FROM {t}')
    except sqlite3.OperationalError:
        pass
con.commit()
con.close()
print('hard-reset ok')
"@
        $py | uv run --quiet python - | Out-Null
    }
}

# 6. Final state summary.
Write-Host ''
Write-Host '=== post-reset state ==='
try {
    $on = Invoke-RestMethod "$UiBase/api/scenarios" -TimeoutSec 5 | Where-Object { $_.current_variant -ne 'off' }
    if ($on) { Write-Host "  scenarios still on: $($on.scenario_id -join ', ')" -ForegroundColor Yellow }
    else     { Write-Host "  scenarios:        all off" -ForegroundColor Green }
} catch { Write-Host "  scenarios:        UNKNOWN ($($_.Exception.Message))" -ForegroundColor Yellow }

try {
    $alerts = (Invoke-RestMethod 'http://localhost:9090/api/v1/alerts' -TimeoutSec 5).data.alerts
    if ($alerts.Count -eq 0) {
        Write-Host "  prom active alerts: 0" -ForegroundColor Green
    } else {
        $names = ($alerts | ForEach-Object { $_.labels.alertname }) -join ', '
        Write-Host ("  prom active alerts: {0} ({1}) -- lag from prior fault, clears in ~2 min" -f $alerts.Count, $names) -ForegroundColor DarkGray
    }
} catch { Write-Host "  prom alerts:      UNKNOWN" -ForegroundColor Yellow }

try {
    $v = Invoke-RestMethod "$UiBase/api/verdicts?limit=1" -TimeoutSec 5
    Write-Host ("  persisted verdicts: {0}" -f $v.count)
} catch { Write-Host "  verdicts:         UNKNOWN" -ForegroundColor Yellow }

Write-Host ''
if ($problems.Count -eq 0) { Write-Host 'CLEAN' -ForegroundColor Green }
else { Write-Host ('DIRTY  ({0} failed)' -f $problems.Count) -ForegroundColor Red; exit 1 }
