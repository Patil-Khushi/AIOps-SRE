# Verify AIOPS_SERVICENOW_* credentials in .env can authenticate against the PDI.
#
# Run this after setting the aiops_agent password via the SN UI's
# "Set Password" related link (see aiops/tools/itsm/README.md §2 and
# issue #43). Confirms .env points at a working least-privilege user
# before flipping the rest of the stack off the admin account.
#
# Exit codes:
#   0  basic-auth returned 200 + a result array
#   1  missing/invalid .env values or non-200 response

[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\.env')
)

$ErrorActionPreference = 'Stop'

function Get-EnvFileValue($path, $key) {
    if (-not (Test-Path $path)) { return $null }
    foreach ($line in Get-Content $path -Encoding UTF8) {
        if ($line -match "^\s*$([Regex]::Escape($key))\s*=\s*(.+?)\s*(#.*)?$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

$envPath = (Resolve-Path $EnvFile -ErrorAction SilentlyContinue)
if (-not $envPath) {
    Write-Host "FAIL: .env not found at $EnvFile" -ForegroundColor Red
    exit 1
}

$url  = Get-EnvFileValue $envPath 'AIOPS_SERVICENOW_INSTANCE_URL'
$user = Get-EnvFileValue $envPath 'AIOPS_SERVICENOW_USER'
$pw   = Get-EnvFileValue $envPath 'AIOPS_SERVICENOW_PASSWORD'

if (-not $url -or -not $user -or -not $pw) {
    Write-Host "FAIL: AIOPS_SERVICENOW_{INSTANCE_URL,USER,PASSWORD} not all set in $envPath" -ForegroundColor Red
    exit 1
}

$url = $url.TrimEnd('/')
Write-Host "Probing $url as $user ..." -ForegroundColor Cyan

$pair = "$($user):$($pw)"
$b64  = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$probe = "$url/api/now/table/incident?sysparm_limit=1"

try {
    $resp = Invoke-WebRequest -Uri $probe `
        -Headers @{ Authorization = "Basic $b64"; Accept = 'application/json' } `
        -UseBasicParsing -TimeoutSec 15
} catch {
    $status = $_.Exception.Response.StatusCode.value__ 2>$null
    if ($status -eq 401) {
        Write-Host "FAIL: 401 Unauthorized." -ForegroundColor Red
        Write-Host "  Re-set the password via the SN UI's 'Set Password' related link" -ForegroundColor Yellow
        Write-Host "  (not the form's Password field). See aiops/tools/itsm/README.md." -ForegroundColor Yellow
    } else {
        Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red
    }
    exit 1
}

if ($resp.StatusCode -eq 200 -and $resp.Content -match '"result"') {
    Write-Host "OK: $user authenticates and can read /api/now/table/incident." -ForegroundColor Green
    Write-Host "Next: uv run pytest tests/test_smoke.py" -ForegroundColor DarkGray
    exit 0
}

Write-Host "FAIL: unexpected response (status $($resp.StatusCode))." -ForegroundColor Red
exit 1
