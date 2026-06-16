# Auto-Healer Lite — two surfaces in one agent

**Status:** **v1 — fires the tool for real when ``dry_run=False`` and the gate clears.** Every attempt is persisted to ``aiops.state.ExecutionRow``. The legacy HITL-1 narrow surface (issue #77) coexists and is unchanged.

**Phase:** Prescriptive-Adaptive · **HITL:** Required (platform-enforced at the tool gate)

---

## What this agent is

Auto-Healer Lite has **two coexisting paths** in the same module. Both go through the platform HITL gate (`aiops.policy.HITLGate`) — that is the only common ground.

### 1. Legacy HITL-1 narrow surface (issue #77)

Hardcoded to "restart this deployment". Built specifically to exercise the platform HITL gate end-to-end through the `automation.runbook.execute` capability so the principle "HITL is platform-enforced, not agent-enforced" became a runnable demo.

```python
from agents.auto_healer_lite import recommend_restart, RestartRecommendation

outcome = recommend_restart(
    RestartRecommendation(deployment="product-catalog", reason="stuck pod")
)
# outcome.status ∈ {"executed", "blocked", "denied", "expired", "error"}
```

Tests in `tests/test_auto_healer_lite.py` cover this path with a background-thread approver. The `__main__.py` CLI exposes it for reviewers without standing up the FastAPI server. **This surface is untouched by PRS-002.**

### 2. PRS-002 generic surface (v1, this agent)

Receives a chosen `RemediationOption` from PRS-001 and produces a structured `ExecutionVerdict` after the platform HITL gate runs. When `dry_run=True` (default — safer), the agent stops at `DRY_RUN_OK` and records what *would* have run via `would_execute=True`. When `dry_run=False` and the gate clears, the agent **dispatches the tool for real** via `aiops.tools.get_registry().call(tool_capability, **tool_args)` and maps the `ToolResult` to `EXECUTED` (ok=True) or `EXECUTION_FAILED` (ok=False, capability not registered, or tool raised). Every attempt is persisted to `aiops.state.ExecutionRow`.

```python
from agents.auto_healer_lite import execute, ExecutionRequest

req = ExecutionRequest(
    option={
        "option_id": "rca-step-2",
        "action_type": "set_flag",
        "blast_radius": "low",
        "tool_capability": "feature_flags.set_variant",
        "tool_args": {"flag": "paymentEventsV2", "variant": "off"},
        "rollback": "Re-enable the flag.",
        "requires_hitl": True,
    },
    incident_id="INC-1234",
    affected_service="payment",
    operator="alice@example.com",
)

verdict = execute(req)
# verdict.status ∈ ExecutionStatus
# verdict.decision.allowed: bool
# verdict.would_execute: bool   # True when v1 would have fired the tool here
```

---

## PRS-002 contract

### `ExecutionRequest`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `option` | `dict` (RemediationOption-shape) | yes | The chosen option from PRS-001. Must carry `option_id`, `action_type`, `tool_capability` (for non-manual), `tool_args`, `blast_radius`, `rollback`, `requires_hitl=True`. |
| `affected_service` | `str` | yes | The service the option targets. |
| `incident_id` | `str \| None` | no | RA-001 incident id for cross-reference. |
| `operator` | `str \| None` | no | Who initiated the execution. Recorded for audit. |
| `dry_run` | `bool` | default `True` | Day-1: forced True in the stub regardless. v1 will honour `False`. |
| `hitl_context` | `dict` | default `{}` | Extra context the gate's approver UI can render. |

### `ExecutionVerdict`

```
ExecutionVerdict {
  request_id           : "ahl-<12 hex>"          # stable per call
  option_id            : str                     # echoes input
  affected_service     : str
  status               : ExecutionStatus
  dry_run              : bool                    # echoes input
  requires_hitl        : Literal[True]           # invariant
  decision             : GateDecisionSummary     # what the platform gate said
  tool_capability      : str | None              # what tool WOULD have run
  tool_args            : dict                    # ...with which args
  tool_result          : dict | None             # v1 only; null in Day-1
  would_execute        : bool                    # True iff v1 would fire here
  error                : str | None              # v1 / refused-with-error
  rationale            : str                     # human-readable summary
  audit_metadata.created_by  : "PRS-002"
  audit_metadata.decision_trace : [str]
}
```

### `ExecutionStatus`

| Value | When |
|---|---|
| `refused` | Option failed validation (missing `requires_hitl`, missing `option_id`, non-manual without `tool_capability`). Never reached the gate. |
| `pending_approval` | Gate flow opened an approval request; awaiting a human. |
| `blocked` | Gate denied or the approval window expired. Includes "REQUIRED but no approver installed". |
| `approved` | Gate cleared but the tool was not fired. v1 successor state. |
| `dry_run_ok` | Day-1 success — gate cleared, stub did NOT call the tool. |
| `executed` | v1 only — tool returned ok. |
| `execution_failed` | v1 only — tool raised or returned not-ok. |

---

## HITL story

The agent enforces on a dedicated action: `auto_heal.lite.execute`, declared `REQUIRED` in `aiops/policy/gate.py:DEFAULT_LEVELS`. This is **stricter** than the catalog's generic `auto_heal.execute` (OPTIONAL) because the "lite" Day-1 path trades autonomy for safety until the policy story matures.

The agent calls `get_gate().check(...)` (not `enforce`) so the verdict carries the gate's full `Decision` payload regardless of outcome. The dashboard / chatops sink can render it without re-deriving fields.

**Invariants enforced at the model layer:**

- `ExecutionVerdict.requires_hitl: Literal[True]` — pydantic rejects any verdict that tries to declare itself non-gated.
- The validator in `_validate_option` rejects an option whose own `requires_hitl` is not truthy. The agent never overrides the upstream's autonomy declaration.

**v1 still does NOT:**

- Roll back automatically. The verdict reports the rollback string but the agent does not invoke the reverse capability on `EXECUTION_FAILED`. The operator follows the option's rollback plan manually.
- Pre-flight blast-radius re-validation. The option's stored radius is taken on trust; future work compares against current CMDB / topology.
- Rollback rehearsal. v-next will confirm the rollback string maps to a registered reverse capability before forward fire.

---

## Files

| File | Role |
|---|---|
| `models.py` | Both surfaces' Pydantic models — legacy `RestartRecommendation` / `RestartOutcome` and PRS-002 `ExecutionRequest` / `ExecutionVerdict` / `ExecutionStatus` / `GateDecisionSummary` / `AuditMetadata` |
| `agent.py` | Both entry points — legacy `recommend_restart` (unchanged) and PRS-002 `execute`. Shared `run(input)` dispatches on input shape. |
| `__main__.py` | CLI for the legacy HITL-1 demo (unchanged). |
| `evals/golden.json` | 6 deterministic cases for the PRS-002 generic surface. 6/6 pass on the harness. |
| `__init__.py` | Public exports for both surfaces. |

---

## Testing it

### Eval harness (deterministic outcomes)

```powershell
uv run python -m evals.harness --agent auto_healer_lite
```

The 6 goldens pin to REFUSED and BLOCKED outcomes because the eval harness runs with the default `_no_approver`. A valid option correctly comes back BLOCKED (the gate did its job; nobody approved). Refused outcomes prove the validator catches malformed options before the gate ever sees them.

### v1 happy + failure paths (with an installed approver)

The DRY_RUN_OK / EXECUTED / EXECUTION_FAILED paths all require an approver installed in the gate AND a registered tool capability. Those live in `tests/test_auto_healer_lite_prs002.py`:

```powershell
uv run pytest tests/test_auto_healer_lite_prs002.py -v
```

Nine cases cover:
- Gate clears + `dry_run=True` → `DRY_RUN_OK`, no tool call
- Gate clears + `dry_run=False` + tool ok → `EXECUTED`, `tool_result` populated, captured args asserted
- Gate clears + tool returns `ok=False` → `EXECUTION_FAILED` with the error string
- Gate clears + tool capability not registered → `EXECUTION_FAILED` with "not registered"
- Gate clears + tool raises → `EXECUTION_FAILED` (registry catches; agent surfaces)
- Gate denies → `BLOCKED`
- `ExecutionRow` persisted on `EXECUTED`, `REFUSED`, and `BLOCKED` outcomes

### Legacy HITL-1 path

```powershell
# Block path (no approver):
uv run python -m agents.auto_healer_lite --deployment product-catalog --no-approve

# Happy path (auto-approve after 2s):
uv run python -m agents.auto_healer_lite --deployment product-catalog --auto-approve-after 2
```

---

## What v1 ships (this PR) vs what's deferred

| v1 (this PR) | v-next (deferred) |
|---|---|
| ✅ Real `aiops.tools.get_registry().call(tool_capability, **tool_args)` when `dry_run=False` AND gate clears | Pre-flight blast-radius re-validation against current CMDB / topology |
| ✅ Honours caller's `dry_run` flag | Rollback rehearsal — confirm the rollback string maps to a registered reverse capability before forward fire |
| ✅ `tool_result` populated on `EXECUTED` / `EXECUTION_FAILED` | Automatic rollback on `EXECUTION_FAILED` (today: surfaces the rollback string for the operator) |
| ✅ Audit-trail row in `aiops.state.ExecutionRow` on every attempt | Caller-supplied `approval_id` for deterministic test seeding |
| ✅ `list_executions()` repository query for dashboard history | Dashboard panel that renders the option list + Execute buttons |

The contract is **locked** — the wire shape (`ExecutionRequest` / `ExecutionVerdict`) is stable across v1, v-next, and future LLM-driven re-ranking, so PRS-001 callers and the dashboard never break on agent upgrades.

---

## Why the dual surface

The legacy HITL-1 path is real working code that demonstrates the platform HITL gate end-to-end. Replacing it for PRS-002 would have:

- Broken 4 active tests (`tests/test_auto_healer_lite.py`).
- Broken the `__main__.py` CLI reviewers use to see the gate fire without the demo server.
- Lost the only existing proof that the platform gate enforces REQUIRED actions.

So PRS-002 layers on top: same module, same gate, broader contract. The `run(input)` dispatcher routes by input shape so the eval harness sees both worlds.
