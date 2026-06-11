# Remediation Recommender — PRS-001

**Status:** Day-1 scaffold (deterministic stub, no LLM call). Locked
contract; logic upgrades land incrementally without breaking the I/O
shape.

**Phase:** Prescriptive-Adaptive · **HITL (recommendation):** Optional
· **HITL (execution):** Required (enforced at the platform tool gate)

---

## What it does

Sits between the **RCA Agent (PRS-008)** and **Auto-Healer / `auto_healer_lite` (PRS-002)**:

```
Alert → RA-001 Triage → … → PRS-008 RCA ─→ PRS-001 Remediation Recommender ─→ Auto-Healer (gated)
                              │                       │                              │
                              │                       │                              ▼
                              │                       │                       platform HITL gate
                              │                       │                              │
                              ▼                       ▼                              ▼
                       root_cause +            ranked options +              tool execution
                       fix steps                blast/rollback/MTTR
```

- **Input** — the upstream RCA causal chain (`RCAVerdict` from PRS-008) plus the original triage context.
- **Output** — a **ranked decision set** of remediation options. Each option carries everything the operator needs to pick one, and everything Auto-Healer needs to execute the chosen one once HITL clears.

PRS-001 adds value over RCA's `ranked_fix_steps` by:

1. **Mixing in symptom-driven mitigations** RCA may not have proposed (circuit-breakers, rate-limiters, provider fail-overs) from a static catalog (`remediation_catalog.py`).
2. **Re-ranking with a transparent composite score** that puts safety (low blast radius) above raw confidence — first, do no harm.
3. **Environment-aware ordering** — production tilts safer; staging/dev tolerates higher blast.
4. **Tool-seam handoff** — every option declares its `tool_capability` + `tool_args`, so the chosen option flows straight into the platform tool registry once HITL approves.

---

## Contract

### Input — `RemediationInput`

| Field | Type | Required? | Meaning |
|---|---|---|---|
| `rca_verdict` | `dict` (RCAVerdict-shape) | **yes** | Upstream RCA output. The agent reads `affected_service`, `root_cause`, `confidence_score`, and `ranked_fix_steps[]`. |
| `triage_verdict` | `dict \| None` | no | Original RA-001 output. The agent reads `alert_summary` for the `incident_summary` field on the output. |
| `environment` | `"production" \| "staging" \| "dev"` | default `"production"` | Influences the composite ranking — production prefers safer options more strongly. |
| `operator_preferences` | `dict` | default `{}` | Forward-compat hook. Day-1 honours `prefer_safe: bool` (defaults `True`). v1 will add cost / speed / surface preferences. |

### Output — `RemediationVerdict`

```text
RemediationVerdict {
  affected_service          : str
  incident_summary          : str   # one-line "what this is about"
  options                   : [RemediationOption]   # sorted, len ≥ 1
  recommended_option_id     : str   # = options[0].option_id
  auto_pick_eligible        : False # Day-1 invariant; v1 may unlock
  confidence_score          : float (0..1)          # mean of top-3 option confidences
  requires_hitl             : True  # invariant
  rationale                 : str   # one-paragraph "why this top pick"
  audit_metadata.created_by : "PRS-001"
  audit_metadata.decision_trace : [str]   # reasoning steps
}
```

### One option — `RemediationOption`

```text
RemediationOption {
  option_id              : str            # stable, e.g. "rca-step-1" or "kafka-restart-consumer"
  title                  : str            # human-readable, 1-line
  description            : str            # 1-3 sentences
  action_type            : "set_flag" | "rollback_deploy" | "scale" | "restart" | "circuit_breaker" | "manual"
  blast_radius           : "low" | "medium" | "high"
  blast_radius_score     : int (1..5)     # lower = safer; UI sorts on this
  rollback               : str            # 1-2 sentences
  rollback_tested        : bool           # has the reverse been verified?
  confidence             : float (0..1)
  estimated_mttr_minutes : int            # median wall-clock to resolve via this option
  requires_hitl          : True           # invariant — Auto-Healer still gates at the tool boundary
  rationale              : str            # why this option ranks where it does
  tool_capability        : str | None     # what platform tool would execute it
  tool_args              : dict           # ready-to-pass kwargs (after operator approval)
  source                 : "rca_fix_step" | "playbook_pattern" | "operator_seeded"
}
```

**`requires_hitl: True` is enforced at the model layer** (`Literal[True]`) so an LLM-supplied `"requires_hitl": false` is rejected by pydantic before any caller sees it.

---

## HITL story

| Boundary | Level | Where enforced |
|---|---|---|
| **Recommendation** ("publish the ranked option list") | **Optional** | The agent runs without HITL. The platform may surface the recommendation to the operator before any execution — that's the optional gate. |
| **Execution** ("fire `tool_capability` with `tool_args`") | **Required** | NOT inside this agent. Auto-Healer / `auto_healer_lite` calls `aiops.tools.get_registry().call(option.tool_capability, **option.tool_args)` and the platform HITL gate (`aiops.policy.get_gate().enforce(...)`) blocks the call until an operator approves. |

The agent does **not** call the HITL gate itself. CLAUDE.md non-negotiable #3: HITL is platform-enforced. PRS-001 declares the autonomy level via `requires_hitl: True` on every option; the platform enforces it at the action boundary.

---

## Ranking (Day-1)

Transparent composite score — no LLM, no hidden weights:

```
score = (6 - blast_radius_score) * 10        # safer dominates; LOW=50, HIGH=10
      + confidence * 5                        # max +5 for full confidence
      + (3 if rollback_tested else 0)         # proof-of-reversibility bonus
      + env_bonus                             # production+prefer_safe+LOW: +5
                                              # staging/dev+MEDIUM:         +2
```

Ties break on (`blast_radius_score` ascending, `confidence` descending, `option_id` lexicographic). The lexicographic tail-break makes the ranking **deterministic** so the eval harness gets reproducible results.

---

## What this Day-1 stub does NOT do

- **No LLM call.** Pure deterministic mapping. v1 will add an LLM re-rank with a structured rationale.
- **No historical effectiveness.** v1 will pull from `HistoricalIncidentRow` to weight options by past success rate.
- **No cost-aware ordering.** Scaling out costs money; flag flips don't. v1 will surface a `$$$` annotation.
- **No execution.** Auto-Healer owns the tool call.
- **`auto_pick_eligible` is hard-`False`.** v1 may unlock auto-pick for blast=LOW + confidence≥0.9 + rollback_tested options when policy explicitly allows.

---

## Files

| File | Role |
|---|---|
| `models.py` | Pydantic I/O models + `BlastRadius` / `ActionType` / `OptionSource` enums |
| `remediation_catalog.py` | Static lookup of failure-pattern → option templates (the symptom-driven mitigations RCA might miss) |
| `agent.py` | `recommend()` (typed) + `run()` (eval-harness entry); deterministic stub |
| `__init__.py` | Public surface |
| `evals/golden.json` | 6 hand-curated cases covering RCA-mapping, catalog merge, ranking, and the manual fallback |

---

## Testing it

```powershell
# Eval harness (matches the convention for all other agents)
uv run python -m evals.harness --agent remediation_recommender

# Lint
uv run ruff check agents/remediation_recommender
```

Each golden case scores at least the headline assertions: `affected_service`, `recommended_option_id` (or `_in` for multi-valid-top cases), `requires_hitl: true`, `auto_pick_eligible: false`. Adding a new case: drop it into `evals/golden.json` — the harness auto-discovers.

---

## Catalog reference

`docs/Adaptive_AIOps_Agent_Catalog.xlsx` (Prescriptive-Adaptive sheet) is the authoritative spec. This Day-1 scaffold aligns the I/O shape and HITL levels to the catalog; any divergence found during the v1 LLM build is a catalog-vs-code drift bug, not a contract change.
