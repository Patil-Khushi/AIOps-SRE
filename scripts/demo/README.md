# Demo scripts

PowerShell helpers for reproducible RA-005 / chatops demos. All scripts
assume the FastAPI demo server is running on `http://127.0.0.1:8765`.

## Start the server (one-time per session)

```powershell
cd c:\Users\GP127945\Documents\Adaptive-AIOps\AIops
.\.venv\Scripts\Activate.ps1
uv run --extra ui --extra dev uvicorn demo.ui.server:app --host 127.0.0.1 --port 8765
```

Leave that terminal open. In a second PowerShell window:

```powershell
cd c:\Users\GP127945\Documents\Adaptive-AIOps\AIops
```

…then run any of the scripts below.

## Scripts

### `fire-all.ps1` — the headline demo

Fires four fixtures, one per severity (Sev-1, Sev-2, Sev-3, Sev-4), with
a 1-second pause between each. The Notifications dashboard should show
four cards routed to four different channels — the visual proof that
RA-005's routing logic actually distinguishes severity.

```powershell
.\scripts\demo\fire-all.ps1
```

Use this for the live demo segment of a pitch.

### `fire.ps1` — fire one fixture

For ad-hoc testing or showing a specific routing decision.

```powershell
# List available fixtures
.\scripts\demo\fire.ps1 -List

# Fire one
.\scripts\demo\fire.ps1 severity_hint_critical_direct
.\scripts\demo\fire.ps1 payment_cpu_spike
```

## Inspect the audit log

```powershell
Get-Content demo\audit\chatops.jsonl
```

Pretty-printed:

```powershell
Get-Content demo\audit\chatops.jsonl | ForEach-Object {
    $r = $_ | ConvertFrom-Json
    "[$($r.severity.ToUpper().PadLeft(4))]  #$($r.channel.PadRight(20)) $($r.title)"
}
```

## Reset state between demos

```powershell
Clear-Content demo\audit\chatops.jsonl
```

The dashboard panel still shows in-memory history until the server is
restarted. To fully reset, stop the server (Ctrl+C in its window) and
restart it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach http://127.0.0.1:8765/api/health` | Start the server (see top of this file) |
| 404 from `fire.ps1` | Wrong fixture id — run `fire.ps1 -List` |
| Card doesn't appear on dashboard | Severity filter on the page is set narrower than the fired severity; pick "All severities" |
| `curl: parameter cannot be found` | You wrote `curl` — these scripts use `Invoke-RestMethod`, which is PowerShell-native |
