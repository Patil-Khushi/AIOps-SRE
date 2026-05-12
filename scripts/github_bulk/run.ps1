# Bulk-create GitHub issues from issues.json and add them to a Project v2.
#
# Idempotent: skips labels that already exist (gh returns non-zero, we swallow),
# and skips issues whose exact title already exists in the repo.
#
# Usage:
#   .\scripts\github_bulk\run.ps1
#   .\scripts\github_bulk\run.ps1 -DryRun          # print what would happen, do nothing
#   .\scripts\github_bulk\run.ps1 -SkipLabels      # assume labels already created
#   .\scripts\github_bulk\run.ps1 -SkipProject     # only create issues, don't add to project

[CmdletBinding()]
param(
    [string]$ManifestPath = "$PSScriptRoot\issues.json",
    [switch]$DryRun,
    [switch]$SkipLabels,
    [switch]$SkipProject
)

$ErrorActionPreference = 'Stop'

# Make winget-installed gh resolvable in this session.
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh CLI not found on PATH. Install with: winget install --scope user GitHub.cli"
}

$manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
$repo = $manifest.repo
$projectId = $manifest.project_id
Write-Host "Manifest: $($manifest.issues.Count) issues for repo '$repo' -> project '$projectId'" -ForegroundColor Cyan

# --- 1. Labels (idempotent) ------------------------------------------------
if (-not $SkipLabels) {
    Write-Host "`n[1/3] Creating labels..." -ForegroundColor Cyan
    foreach ($label in $manifest.labels) {
        if ($DryRun) {
            Write-Host "  dry-run: gh label create '$($label.name)'"
            continue
        }
        # `gh label create` returns non-zero if the label exists. Swallow.
        $null = & gh label create $label.name --color $label.color --description $label.description --repo $repo 2>&1
        $verb = if ($LASTEXITCODE -eq 0) { "created" } else { "exists" }
        Write-Host "  $verb`t$($label.name)"
    }
}

# --- 2. Existing issue titles (to make this re-runnable) -------------------
Write-Host "`n[2/3] Loading existing open issue titles..." -ForegroundColor Cyan
$existingTitles = @()
if (-not $DryRun) {
    $existing = & gh issue list --repo $repo --state all --limit 200 --json title | ConvertFrom-Json
    $existingTitles = $existing | ForEach-Object { $_.title }
    Write-Host "  found $($existingTitles.Count) existing issues"
}

# --- 3. Create issues + add to project ------------------------------------
Write-Host "`n[3/3] Creating issues..." -ForegroundColor Cyan
$created = 0
$skipped = 0
$projectAdded = 0
$bodyTmp = [System.IO.Path]::GetTempFileName()

foreach ($issue in $manifest.issues) {
    if ($existingTitles -contains $issue.title) {
        Write-Host "  skip   `t$($issue.title)" -ForegroundColor DarkGray
        $skipped++
        continue
    }

    if ($DryRun) {
        Write-Host "  dry-run`tcreate $($issue.title)"
        continue
    }

    # gh issue create's --body has poor multiline handling; use --body-file.
    Set-Content -Path $bodyTmp -Value $issue.body -Encoding utf8 -NoNewline
    $labelArgs = @()
    foreach ($lbl in $issue.labels) { $labelArgs += @('--label', $lbl) }

    $url = & gh issue create --repo $repo --title $issue.title --body-file $bodyTmp @labelArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "  FAILED `t$($issue.title)`n           $url"
        continue
    }
    Write-Host "  created`t$($issue.title) -> $url" -ForegroundColor Green
    $created++

    if (-not $SkipProject) {
        # Resolve the issue's GraphQL node id, then add to the project.
        $nodeId = & gh issue view $url --repo $repo --json id -q .id
        if ($LASTEXITCODE -ne 0 -or -not $nodeId) {
            Write-Warning "    could not resolve node id for $url"
            continue
        }
        $addQuery = @'
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
'@
        $null = & gh api graphql -F projectId=$projectId -F contentId=$nodeId -f query=$addQuery 2>&1
        if ($LASTEXITCODE -eq 0) {
            $projectAdded++
        } else {
            Write-Warning "    project add failed for $url"
        }
    }
    Start-Sleep -Milliseconds 400  # gentle on the API
}

Remove-Item $bodyTmp -ErrorAction SilentlyContinue

Write-Host "`nDone. created=$created skipped=$skipped project_added=$projectAdded" -ForegroundColor Cyan
