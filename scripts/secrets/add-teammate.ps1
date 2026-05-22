# scripts/secrets/add-teammate.ps1
#
# Add a new teammate to git-crypt. Run from the repo root after they've sent
# you their armored public key (`their-pubkey.asc`).
#
# Usage:
#   .\scripts\secrets\add-teammate.ps1 -PubkeyFile .\their-pubkey.asc
#   .\scripts\secrets\add-teammate.ps1 -Email their.email@zensar.com   # if already imported
#
# What it does (idempotent):
#   1. Imports the public key (if -PubkeyFile is given)
#   2. Sets ultimate owner-trust on the key so git-crypt accepts it
#   3. Runs `git-crypt add-gpg-user <email>`, which creates a commit
#   4. Reminds you to `git push` and to tell the teammate to git pull + unlock
#
# See SECRETS.md for the human-readable version.

[CmdletBinding(DefaultParameterSetName = 'ByFile')]
param(
    [Parameter(ParameterSetName = 'ByFile', Mandatory = $true)]
    [string]$PubkeyFile,

    [Parameter(ParameterSetName = 'ByEmail', Mandatory = $true)]
    [string]$Email
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Error $msg; exit 1 }

if (-not (Test-Path '.\.gitattributes')) { Fail 'Run from the repo root (where .gitattributes lives).' }
if (-not (Get-Command gpg -ErrorAction SilentlyContinue)) { Fail 'gpg is not on PATH. See SECRETS.md.' }
if (-not (Get-Command git-crypt -ErrorAction SilentlyContinue)) { Fail 'git-crypt is not on PATH. See SECRETS.md.' }

if ($PSCmdlet.ParameterSetName -eq 'ByFile') {
    if (-not (Test-Path $PubkeyFile)) { Fail "Pubkey file not found: $PubkeyFile" }

    Write-Host "==> Importing $PubkeyFile" -ForegroundColor Cyan
    & gpg --import $PubkeyFile
    if ($LASTEXITCODE -ne 0) { Fail 'gpg --import failed.' }

    $importInfo = & gpg --import-options show-only --import $PubkeyFile 2>&1
    $uidLine = $importInfo | Where-Object { $_ -match '^uid' } | Select-Object -First 1
    if (-not $uidLine) { Fail "Couldn't parse a UID from $PubkeyFile." }
    if ($uidLine -match '<([^>]+)>') { $Email = $Matches[1] } else { Fail "Couldn't parse an email from UID line: $uidLine" }
    Write-Host "    Resolved email: $Email" -ForegroundColor Green
}

Write-Host "==> Setting ultimate trust on $Email" -ForegroundColor Cyan
$fpr = (& gpg --with-colons --list-keys $Email 2>$null |
        Where-Object { $_ -like 'fpr:*' } |
        ForEach-Object { ($_ -split ':')[9] } |
        Select-Object -First 1)
if (-not $fpr) { Fail "No key found in keyring for $Email." }

$trustEntry = "${fpr}:6:"
$trustEntry | & gpg --import-ownertrust
if ($LASTEXITCODE -ne 0) { Fail 'gpg --import-ownertrust failed.' }

Write-Host "==> Running git-crypt add-gpg-user $Email" -ForegroundColor Cyan
& git-crypt add-gpg-user $Email
if ($LASTEXITCODE -ne 0) { Fail 'git-crypt add-gpg-user failed.' }

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host 'Next steps:'
Write-Host '  1. git push'
Write-Host "  2. Tell $Email to: git pull; git-crypt unlock; Copy-Item .env.shared .env"
