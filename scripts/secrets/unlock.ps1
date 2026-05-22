# scripts/secrets/unlock.ps1
#
# Decrypt the repo's encrypted files (.env.shared) on this machine.
#
# Usage:
#   .\scripts\secrets\unlock.ps1                        # use your GPG key
#   .\scripts\secrets\unlock.ps1 -KeyFile .\backup.key  # use the symmetric backup key
#   .\scripts\secrets\unlock.ps1 -CopyToEnv             # also Copy-Item .env.shared .env afterwards
#
# See SECRETS.md for the full onboarding flow.

param(
    [string]$KeyFile,
    [switch]$CopyToEnv
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command git-crypt -ErrorAction SilentlyContinue)) {
    Write-Error 'git-crypt is not on PATH. See SECRETS.md.'; exit 1
}
if (-not (Test-Path '.\.gitattributes')) {
    Write-Error 'Run from the repo root (where .gitattributes lives).'; exit 1
}

if ($KeyFile) {
    if (-not (Test-Path $KeyFile)) { Write-Error "Key file not found: $KeyFile"; exit 1 }
    Write-Host "==> Unlocking with symmetric key $KeyFile" -ForegroundColor Cyan
    & git-crypt unlock $KeyFile
} else {
    Write-Host '==> Unlocking with your GPG key' -ForegroundColor Cyan
    & git-crypt unlock
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host 'Unlock failed. Common causes:' -ForegroundColor Yellow
    Write-Host '  - Your GPG key has not been added by the repo owner yet (you need git-crypt add-gpg-user).'
    Write-Host '  - You generated a new GPG key after being added; ask to be added again.'
    Write-Host '  - The keyfile path is wrong.'
    exit 1
}

Write-Host ''
Write-Host 'Unlocked. .env.shared is now plaintext on disk.' -ForegroundColor Green
& git-crypt status -e | Select-Object -First 10

if ($CopyToEnv) {
    if (-not (Test-Path '.\.env.shared')) {
        Write-Warning '.env.shared not found in repo root; nothing to copy.'
    } elseif (Test-Path '.\.env') {
        Write-Warning '.env already exists on disk. Refusing to overwrite. Diff yourself: git diff --no-index .env .env.shared'
    } else {
        Copy-Item '.env.shared' '.env'
        Write-Host ''
        Write-Host 'Copied .env.shared -> .env. Add personal overrides (KUBECONFIG, etc.) to .env.' -ForegroundColor Green
    }
}
