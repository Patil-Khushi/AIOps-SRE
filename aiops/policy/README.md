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

- **Phase 0 (now):** levels are hard-coded in `gate.py::DEFAULT_LEVELS`. Approver always returns `None` so `REQUIRED` actions block.
- **Phase 1:** approver wired to a Slack interaction or a simple web UI.
- **Phase 2:** levels sourced from `policies/hitl.rego` via OPA. The Rego file becomes the source of truth.
