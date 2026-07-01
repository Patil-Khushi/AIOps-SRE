# Adaptive AIOps end-to-end demo — frozen walkthrough

**Goal:** drive the full **Reactive → Prescriptive** loop on the OpenTelemetry demo with one PowerShell session, ending in a HITL-gated tool call that flips an OTel failure flag back off and resolves the injected incident.

**This walkthrough is locked.** A scenario-locked smoke test ([tests/test_chained_demo.py](../tests/test_chained_demo.py)) fails CI if the chain shape ever drifts — if anyone changes endpoint URLs, response keys, or the agent call order, the test trips and points back here.

**For audience-facing presenter notes**, see [DEMO_SCRIPT.md](../DEMO_SCRIPT.md) in the repo root. This doc is the operator runbook; that one is the talking-points script.

**Prereqs:**
- Rancher Desktop's k3s up, OTel demo deployed (`.\infra\bootstrap.ps1` if you haven't yet).
- `.env` has real Slack identities + LLM provider creds. See [SECRETS.md](../SECRETS.md).
- On-call DB seeded: `uv run python -m scripts.seed_oncall`.

---

## 0. One-time setup (skip if you've done this today)

```powershell
# Bring up the cluster port-forwards + the FastAPI server on :8765 + the React dashboard at /dashboard/
.\start.ps1

# Verify the server is up
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
```

Open the dashboard in a browser: <http://localhost:8765/dashboard/>. Leave it open — it streams updates over WebSocket as the demo runs.

---

## 1. Inject a real failure (≈30 seconds)

We use a service whose CMDB row exists and whose engineer roster is seeded, so the Slack ping actually fires:

```powershell
uv run python -m demo.failure_injection.inject slow-product-catalog
```

This flips a flagd flag in the OTel demo. The product-catalog service starts returning slowly. Within ~30 seconds a Prometheus alert fires.

**What to watch:** the dashboard's Alert Stream panel turns red as the alert appears.

---

## 2. Drive the full chain (one HTTP call)

```powershell
$body = @{
  alert = @{
    alert_id  = 'DEMO-FULL-1'
    service   = 'product-catalog'
    metric    = 'product_catalog_latency_p95_ms'
    value     = 4500.0
    threshold = 1000.0
    timestamp = '2026-06-12T10:00:00Z'
    source    = 'Prometheus'
    summary   = 'Product catalog p95 latency 4500ms over 1000ms threshold'
  }
  scenario_id = 'slow-product-catalog'
  environment = 'production'
} | ConvertTo-Json -Depth 5

$chain = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/triage-full' `
  -ContentType 'application/json' -Body $body -TimeoutSec 90
```

`/api/triage-full` runs six agents in order:

| # | Agent | Output on `$chain` |
|---|---|---|
| 1 | RA-001 Alert Triage | `$chain.verdict` — severity, owning team, on-call engineer |
| 2 | RA-002 Incident Classifier | `$chain.classification` — incident type, probable root cause |
| 3 | RA-003 Auto-Ticketing | `$chain.ticket` — real ServiceNow PDI incident id |
| 4 | RA-005 Notification Router | `$chain.notifications` (chatops decision) + `$chain.deliveries` (Slack/JSONL/WebSocket outcomes) |
| 5 | PRS-008 RCA Agent | `$chain.rca` — `root_cause` + ranked fix steps with `blast_radius` + `rollback` |
| 6 | PRS-001 Remediation Recommender | `$chain.remediation` — ranked options with `tool_capability` + `tool_args` |

**The chain stops here.** Execution (PRS-002) is the next, separate step so a human is always in the loop.

Inspect the recommendation:

```powershell
$chain.remediation.recommended_option_id
$chain.remediation.options | Select-Object option_id, blast_radius, confidence, tool_capability | Format-Table
```

You'll see something like:

```
option_id                            blast_radius confidence tool_capability
---------                            ------------ ---------- ---------------
rca-step-1                           low                0.85 feature_flags.set_variant
catalog-restart-product-catalog      medium             0.70 k8s.deployment.restart
rca-step-2                           medium             0.78
```

The top option is whichever scored highest under PRS-001's composite scoring (low blast radius beats raw confidence on purpose — first, do no harm).

---

## 3. Pick an option and execute (HITL-gated)

Take the recommended option from step 2 and POST it to `/api/execute`. Default is `dry_run=true` — a safety net so you can preview the outcome before the real fire.

### Step 3a — dry-run preview

```powershell
$top = $chain.remediation.options[0]

$exec_body = @{
  option           = $top
  incident_id      = $chain.ticket.ticket_id
  affected_service = $chain.verdict.affected_service
  operator         = 'demo-operator'
  dry_run          = $true
} | ConvertTo-Json -Depth 6

$dry = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/execute' `
  -ContentType 'application/json' -Body $exec_body -TimeoutSec 30

$dry | Select-Object status, would_execute, tool_capability
$dry.decision | Format-List
```

Expected:
- `status` = `dry_run_ok` (gate cleared, no tool call)
- `would_execute` = `True` (PRS-002 confirms it has a tool registered to run)
- `decision.allowed` = `True` (operator approved via the /hitl page or auto-approved during the demo)

If the gate refused, you'll see `status=blocked` and `decision.reason` explains why. Approve via the /hitl page in your browser then re-run.

### Step 3b — fire for real

```powershell
$exec_body = @{
  option           = $top
  incident_id      = $chain.ticket.ticket_id
  affected_service = $chain.verdict.affected_service
  operator         = 'demo-operator'
  dry_run          = $false       # ← the real fire
} | ConvertTo-Json -Depth 6

$result = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8765/api/execute' `
  -ContentType 'application/json' -Body $exec_body -TimeoutSec 30

$result | Select-Object status, tool_capability, error
$result.tool_result | Format-List
```

Expected:
- `status` = `executed`
- `tool_result.ok` = `True`
- `tool_result.data` = the tool's response payload (e.g. `{flag: "...", variant: "off"}`)

**Behind the scenes:** PRS-002 called `aiops.tools.get_registry().call(option.tool_capability, **option.tool_args)`. The platform tool dispatched. The OTel demo flag flipped. The injected failure is no longer occurring.

---

## 4. Verify the fix landed

```powershell
# Confirm Prometheus says the alert recovered (give it ~30s)
Start-Sleep -Seconds 30
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/live-alerts' -TimeoutSec 5 |
  Select-Object -ExpandProperty alerts |
  Where-Object { $_.labels.alertname -like '*product-catalog*' }

# Confirm the audit row landed in aiops.state.ExecutionRow
uv run python -c "from aiops.state.repository import list_executions; import json; rows = list_executions(affected_service='product-catalog', limit=5); print(json.dumps(rows, indent=2))"
```

The Prometheus query should return nothing (no active alerts for product-catalog), and the audit row shows `status=executed`, the engineer who approved, the tool that fired, and the rollback string in case you need to undo manually.

---

## 5. Clean up

```powershell
uv run python -m demo.failure_injection.inject --clear
```

Returns flagd to baseline. The demo is now in a fresh state for the next run.

---

## What's locked

The walkthrough above is held in place by [tests/test_chained_demo.py](../tests/test_chained_demo.py):

- `/api/triage-full` returns the expected top-level keys (`verdict`, `classification`, `ticket`, `notifications`, `deliveries`, `rca`, `remediation`, `persisted`, `errors`).
- The `rca` and `remediation` steps soft-fail to `errors.{step}` instead of breaking the chain when something upstream blips.
- `/api/execute` rejects an option missing `requires_hitl=True` (catalog principle #3).
- `/api/execute` with a valid option + installed approver + registered tool returns `status=executed` and persists an `ExecutionRow`.

If anyone changes those shapes, that test fails and tells them to update this document.

---

## What this demo does NOT do (yet)

These are deliberately out of scope for the frozen walkthrough:

- **No auto-execute.** Even with high-confidence options, the operator must POST to `/api/execute`. No autonomous fix.
- **No dashboard panel for the option list.** The operator picks from `$chain.remediation.options` via PowerShell, not a UI button. The React panel is a separate PR.
- **No automatic rollback on `EXECUTION_FAILED`.** The rollback string is in the verdict; the operator runs it manually.
- **No live re-evaluation of `blast_radius` against current CMDB / topology.** The option's stored radius is taken on trust.

Each is a tracked v-next item in the relevant agent's README.

---

## Where to look in the code

| Surface | File |
|---|---|
| `/api/triage-full` chain endpoint | [demo/ui/server.py](../demo/ui/server.py) — search for `triage_full_endpoint` |
| `/api/remediation` standalone | [demo/ui/server.py](../demo/ui/server.py) — `remediation_endpoint` |
| `/api/execute` standalone | [demo/ui/server.py](../demo/ui/server.py) — `execute_endpoint` |
| Reactive chain step bodies | [agents/alert_triage/](../agents/alert_triage/), [agents/incident_classifier/](../agents/incident_classifier/), [agents/auto_ticketing/](../agents/auto_ticketing/), [agents/notification_assembler/](../agents/notification_assembler/) |
| Prescriptive chain step bodies | [agents/rca_agent/](../agents/rca_agent/), [agents/remediation_recommender/](../agents/remediation_recommender/), [agents/auto_healer_lite/](../agents/auto_healer_lite/) |
| Scenario-lock test | [tests/test_chained_demo.py](../tests/test_chained_demo.py) |
