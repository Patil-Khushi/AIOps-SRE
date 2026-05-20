# Running the AIOps POC — Daily Steps

Quick-reference for actually using the cluster. Assumes the one-time setup in [`ONBOARDING.md`](ONBOARDING.md) is done.

Two windows total: **Window A** = Rancher Desktop's tray, **Window C (PowerShell)** = where you do everything else. The one-command `.\start.ps1` flow below handles port-forwards as background jobs, so the "Window B port-forward stays running" pattern is only needed for the manual fallback.

---

## Start of day

### 1. Start Rancher Desktop

- Open **Rancher Desktop** from the Start menu (or click its tray icon if already pinned).
- Wait until the tray icon shows *Kubernetes: running*. **~30-60 seconds.**

### 2. Bring everything up (Window C)

```powershell
cd C:\Projects\AIops
.\start.ps1
```

`start.ps1` does, in order: verifies the Rancher Desktop k3s API; starts background port-forwards for Prometheus (9090), Jaeger (16686), and `frontend-proxy` (8080); builds the React dashboard on first run; runs `uv sync --extra ui` to make sure `.venv\` has `uvicorn` + `fastapi`; launches the demo UI server at <http://localhost:8765>; opens the dashboard in your browser.

Background jobs are named `pf-*`. To inspect or tail them:

```powershell
Get-Job -Name 'pf-*'                       # list
Get-Job -Name 'pf-*' | Receive-Job -Keep   # tail logs
```

### 3. Dashboards

| URL | What |
|---|---|
| <http://localhost:8765/dashboard/> | React dashboard (agent activity, scenarios, etc.) |
| <http://localhost:8765/> | Vanilla demo UI |
| <http://localhost:8080/> | The fake webstore — click around to generate traffic |
| <http://localhost:8080/grafana/> | Grafana dashboards — login `admin` / `admin` |
| <http://localhost:8080/jaeger/ui/> | Jaeger trace explorer |
| <http://localhost:8080/feature/> | flagd feature-flag UI |
| <http://localhost:9090/> | Prometheus (direct) |

You're ready to work.

### Manual alternative (no `start.ps1`)

Useful when you only need the webstore port-forward, or when you're debugging `start.ps1` itself.

```powershell
kubectl config current-context        # should print: rancher-desktop
kubectl get nodes                     # should show 1 Ready node

# Open a separate PowerShell window and leave this running:
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
```

In this mode the React dashboard / demo UI at 8765 are not running — only the webstore + Grafana + Jaeger on 8080.

---

## Use the demo

### Trigger a failure scenario (Window C)

```powershell
cd C:\Projects\AIops

# See what's available
uv run python -m demo.failure_injection.inject --list

# Pick one and inject it
uv run python -m demo.failure_injection.inject slow-product-catalog
```

Refresh <http://localhost:8080/> — product browsing will be visibly slow. Watch the latency spike in Grafana.

### Clear the failure when you're done

```powershell
uv run python -m demo.failure_injection.inject --clear
```

System returns to baseline within ~1 minute.

### Clean-slate reset (use this before every rehearsal / demo run)

```powershell
.\reset.ps1            # flagd flags off, chatops outbox truncated, scratch files removed
.\reset.ps1 -Hard      # also wipes verdicts / classifications / tickets from data\state.db
```

What it does (in order):

1. `POST /api/scenarios/reset-all` — flips every UI-known scenario flag back to `off` in one atomic kubectl patch.
2. Belt-and-suspenders: runs `inject.py --clear` for any flag the UI doesn't know about.
3. Truncates `demo\audit\chatops.jsonl`.
4. Removes `.tmp_eval*.txt` scratch files.
5. `-Hard` only: `DELETE FROM verdicts / classifications / tickets / notifications` in `data\state.db` so the dashboard's AI Reasoning page starts empty.

What it prints at the end: a "post-reset state" block showing scenarios still on, Prometheus active alerts, and persisted-verdict count.

> **About `prom active alerts` not going to 0 immediately:** Prometheus rules use rolling `[2m]` windows. An alert that was firing from a prior fault stays firing for ~2 min after you flip the flag off — that's *lag*, not a reset failure. The "scenarios" / "persisted verdicts" lines are the source of truth for whether reset actually worked.

### Run a full agent chain from one HTTP call (the demo path)

The dashboard's UI fixture endpoint runs **Alert Triage → Incident Classifier → Auto-Ticketing → Notification Router** as one chained call:

```powershell
Invoke-RestMethod -Method POST http://localhost:8765/api/triage/fixture/payment_cpu_spike -TimeoutSec 90 | ConvertTo-Json -Depth 4
```

Returns a single JSON with four blocks: `verdict`, `ticket` (real INC in ServiceNow PDI), `classification`, `persisted`. Takes ~30 s end-to-end (real Azure OpenAI round-trip).

Other passing fixtures: `checkout_latency_p95_high`, `severity_hint_critical_direct`, `cmdb_miss_unknown_service`, `severity_hint_p2_high`.

**The verdict/classification/ticket also appears in:**

- Dashboard → AI Reasoning tab (`http://localhost:8765/reasoning`)
- ServiceNow PDI → All → Incidents (the new `INC00100xx` record)
- `demo\audit\chatops.jsonl` (tail it: `Get-Content demo\audit\chatops.jsonl -Tail 5`)

### CLI usage caveats (read before you `--fixture`)

- Pass **`--provider openai`** to every agent CLI invocation. The CLIs default to `stub` for keyless smoke tests — the stub returns canned answers, not real LLM verdicts.
- **Do not run `python -m agents.incident_classifier --fixture` standalone** — the standalone CLI uses a stale fixture schema and crashes with a Pydantic `ValidationError`. The classifier still works correctly inside the UI chain above. Tracked for post-demo fix.
- `python -m agents.auto_ticketing` has no `__main__` — drive it via the eval harness or the UI fixture endpoint.

### Run the tests (Window C)

```powershell
cd C:\Projects\AIops
uv run pytest                                # smoke tests
uv run python -m evals.harness               # eval harness — should print pass_rate=1.0
```

---

## End of day

### 1. Stop the demo (port-forwards + UI server)

```powershell
.\stop.ps1
```

This stops the `pf-*` background jobs. If you used the manual fallback instead, go to the port-forward window and `Ctrl+C`.

### 2. Quit Rancher Desktop

Right-click the tray icon → **Quit Rancher Desktop**. This frees ~3-4 GB of RAM. The cluster state (OTel demo, helm release, all pods) is preserved in the WSL VHDX and comes back when you start Rancher Desktop tomorrow.

You do **not** need to teardown or re-bootstrap.

---

## When something's wrong

### "Cannot reach the Kubernetes API"

Rancher Desktop isn't running, or k3s is still booting. Open Rancher Desktop, wait for *Kubernetes: running*, retry.

### Port-forward died (or a `pf-*` job is gone)

If you used `start.ps1`: just re-run `.\start.ps1` — it cleans up stale `pf-*` jobs and restarts them. If you used the manual port-forward: open a new window and run `kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080` again. Port-forwards are stateless.

### UI server didn't come up within 45 s

`start.ps1` prints a warning and tells you to tail the job:

```powershell
Get-Job -Name pf-ui-server | Receive-Job -Keep
```

Most common causes: `.venv\` missing the `ui` extra (re-run `uv sync --extra ui`); something already bound to port 8765 (`Get-NetTCPConnection -LocalPort 8765`); a stray system Python on `PATH` shadowing `.venv\Scripts\uvicorn.exe`.

### A pod is in CrashLoopBackOff or ImagePullBackOff

Check the pod's logs first:

```powershell
kubectl -n otel-demo get pods
kubectl -n otel-demo logs <pod-name> --tail=100
kubectl -n otel-demo describe pod <pod-name>
```

If multiple pods are unhealthy at once → likely disk pressure on the node. Run `Get-PSDrive D | Select-Object Free` — if D: is under ~20 GB free, free space and retry.

### `flagd-config conflict with "kubectl-patch"` during a re-install

```powershell
.\infra\teardown.ps1
.\infra\bootstrap.ps1
```

---

## Reset the cluster (only when you actually need it)

Use this only if Phase 0 is broken and `teardown.ps1` + `bootstrap.ps1` doesn't help.

```powershell
# Removes the otel-demo helm release + namespace. Does NOT touch the cluster itself.
.\infra\teardown.ps1

# Re-installs the OTel demo fresh. Takes ~10 minutes.
.\infra\bootstrap.ps1
```

If even that doesn't help, full reset via Rancher Desktop UI: **Troubleshooting → Reset Kubernetes**. You'll need to re-run `bootstrap.ps1` after.

---

## The commands you actually need most days

```powershell
# 1. Bring everything up (port-forwards + UI server, as background jobs)
.\start.ps1

# 2. Clean slate before each rehearsal / demo run
.\reset.ps1            # or .\reset.ps1 -Hard for a virgin AI Reasoning page

# 3. Run the full agent chain on a fixture (returns triage+ticket+classification)
Invoke-RestMethod -Method POST http://localhost:8765/api/triage/fixture/payment_cpu_spike -TimeoutSec 90 | ConvertTo-Json -Depth 4

# 4. Inject a failure (CLI scenarios — bare flagd flip; does NOT auto-trigger agents)
uv run python -m demo.failure_injection.inject slow-product-catalog

# 5. Clear the failure
uv run python -m demo.failure_injection.inject --clear

# 6. Check pod health
kubectl get pods -n otel-demo

# 7. Tear it all down
.\stop.ps1
```

### Known sharp edges (be aware before tomorrow's demo)

- **Clicking *Inject* in the dashboard's Failure Injection panel does not fire Prometheus alerts** today. The flag flips, the panel turns red, but `Alert Stream` / `Active alerts` / `Severity mix` stay empty because the rules query `status_code="STATUS_CODE_ERROR"` and the OTel demo's payment/product-catalog spans emit `STATUS_CODE_UNSET`. Tracked separately; for the demo, drive the chain via the fixture endpoint in command #3 above.
- **`.\infra\port-forward.ps1` has a silent kuberlr trap inside its `Start-Job` block.** Use `.\start.ps1` instead — it passes the standalone kubectl into the job correctly.
