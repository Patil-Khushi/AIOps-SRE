# `aiops.policy` — HITL gate

Solution Design slide 10 says every action has one of three autonomy levels:

| Level | Meaning | Examples |
|---|---|---|
| `NONE` | Fully autonomous | Topology Discovery, Noise Reducer, Notification Router, Log Correlation, Seasonality Learner |
| `OPTIONAL` | Default-on; tenant can switch on a human gate per policy | Alert Triage, Anomaly Detector, Auto-Healer, Toil Detector, Reliability Forecaster, Cost-Aware Scaler |
| `REQUIRED` | Human approval mandatory | Runbook Executor, Capacity Planner, Change Impact Predictor, Policy Optimizer, Chaos Orchestrator, **every RCA Agent fix step** |

## Usage

```python
from aiops.policy import get_gate, GateError

gate = get_gate()
decision = gate.check("rca.fix_step.execute",
                      {"incident_id": "INC0123", "blast_radius": "low"})
if decision.allowed:
    ...  # proceed
else:
    raise GateError(decision.reason)
```

Or fail-closed:

```python
gate.enforce("automation.runbook.execute", {...})  # raises GateError if blocked
```

## Why this is a platform seam, not in agent code

Putting HITL checks inside agents means a buggy or compromised agent can skip them. The gate is enforced at the platform layer so this cannot happen.

## Phase progression

- **Phase 0:** levels are hard-coded in `gate.py::DEFAULT_LEVELS`. Approver always returns `None` so `REQUIRED` actions block.
- **Phase 2 (now — HITL-1, issue #77):** approver wired to `ApprovalRequester` (see `approvals.py`). REQUIRED actions create a pending request, post an interactive prompt through the `aiops.tools.chatops` seam (Slack + WebSocket + JSONL), block the calling thread until a human approves / denies, or expire after `AIOPS_HITL_APPROVAL_TIMEOUT` seconds (default 600). The dashboard's web buttons and the Slack interactivity callback both resolve the request via `ApprovalRegistry.decide`.
- **Phase 2+:** levels sourced from `policies/hitl.rego` via OPA. The Rego file becomes the source of truth. `ApprovalRegistry` will consult OPA to authorize who *can* approve (today it trusts any chatops-supplied identity).

## HITL approval flow at a glance

```
agent → registry.call(capability, hitl_context={...})
      → gate.check                 (sees level == REQUIRED)
      → approver(action, ctx)      (= ApprovalRequester.__call__)
          → ApprovalRegistry.create  → ChatOpsClient.send (interactive)
          → wait_for(timeout)         ── blocks ──
          ← human approves via Slack callback / web POST
              → ApprovalRegistry.decide → ChatOpsClient.send (audit)
          ← request returned
      ← approver id (or None on deny / expire)
   ← Decision(allowed=...) → tool runs or returns ToolResult(ok=False, ...)
```

## Demo it

```powershell
# CLI happy path — agent recommends a restart, background thread approves:
uv run python -m agents.auto_healer_lite --auto-approve-after 2

# Gate-blocked path — no approver, request expires:
uv run python -m agents.auto_healer_lite --no-approve --timeout 3
```

The demo server (`.\start.ps1`) wires the same approval flow into Slack +
the dashboard. Set `AIOPS_SLACK_WEBHOOK_URL` and `AIOPS_SLACK_SIGNING_SECRET`
to exercise the Slack path end-to-end; otherwise approve via
`POST /api/approvals/{id}/approve`.
