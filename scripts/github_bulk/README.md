# `scripts/github_bulk/` — Bulk-create GitHub Issues + add to Project board

Reusable PowerShell runner that takes a JSON manifest of issues, creates them in
the repo (idempotently), and adds each to a Project v2 board.

Originally built for the W1 sprint (27 issues across 4 dev streams). Re-run as
you add W2/W3 batches — the runner skips issues whose **exact title** already
exists, so re-running is safe.

## Prerequisites

- `gh` CLI on PATH. Install once:
  ```powershell
  winget install --scope user --source winget GitHub.cli
  ```
- `gh auth login` completed with scopes `repo, project, read:org`:
  ```powershell
  gh auth login --hostname github.com --git-protocol https --web --scopes "repo,project,read:org"
  ```

## Files

| File | Purpose |
|---|---|
| `issues.json` | Manifest: repo, project node ID, label catalog, list of issues (title + body + labels each). Edit this to add new issues. |
| `run.ps1` | Runner. Reads the manifest, creates labels, creates issues, adds them to the project. |

## Usage

```powershell
# Dry-run first (no API calls)
.\scripts\github_bulk\run.ps1 -DryRun

# Real run
.\scripts\github_bulk\run.ps1

# Re-running is safe — skips issues whose title already exists
.\scripts\github_bulk\run.ps1

# Faster re-runs after labels are in place
.\scripts\github_bulk\run.ps1 -SkipLabels

# Create issues without touching the project board
.\scripts\github_bulk\run.ps1 -SkipProject
```

## Adding new issues (W2 batches, ad-hoc work)

1. Append objects to the `issues` array in `issues.json`. Each entry needs:
   ```json
   {
     "title": "[X1] Something specific",
     "labels": ["dev-x", "stream-tag", "priority:high"],
     "body": "**As a** ...\n**I want to** ...\n**So that** ...\n\n## Task\n...\n\n## Done When\n..."
   }
   ```
   Bodies are JSON strings — newlines as `\n`, code fences/backticks pass through.
2. If you need new labels, add them to the `labels` array (idempotent — re-running won't fail).
3. Re-run `.\scripts\github_bulk\run.ps1`. New issues created; existing ones skipped.

## Where the project ID and repo come from

Already pre-filled in `issues.json`:

- `repo`: `UbiquotousPanda/AIops`
- `project_id`: `PVT_kwHOC7k8484BXefj` (= @UbiquotousPanda's "Ai Sre Ops" project #1)

To recover the project node ID for a different project:

```powershell
gh api graphql -F login='OWNER' -F number=PROJECT_NUMBER `
  -f query='query($login: String!, $number: Int!) { user(login: $login) { projectV2(number: $number) { id title } } }'
```

## What this script cannot do (do it manually in the UI)

GitHub Projects v2's GraphQL API does not expose mutations for creating custom
fields. After issues are imported, set up custom fields manually under
**Project Settings → Fields**:

- `Sprint` — Iteration field (W1, W2, …)
- `Story Points` — Number field
- `Stream` — Single select (RA-001, RA-002, RA-003, …)

## Notes

- The runner uses `--body-file` (temp file) instead of `--body` because gh's
  flag eats embedded newlines and code fences. Bodies render as proper Markdown
  on GitHub.
- Sleeps 400 ms between API calls. Bump if you hit secondary rate limits on
  large batches.
- Labels are created with fixed colors that match the project board's visual
  convention. Edit `labels[].color` in the manifest to change them globally.
