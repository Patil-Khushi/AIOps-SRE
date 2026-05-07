# `policies/` — policy-as-code

Solution Design §2.4: every action passes through a declarative policy layer before execution. Policies live in Git, are reviewed like code, and are the source of truth that platform-layer gates query.

## Files

| File | What |
|---|---|
| `hitl.rego` | Autonomy level per action (None / Optional / Required). Mirrors the catalog. |

## Phase progression

- **Phase 0 (now):** `hitl.rego` is reference-only. Runtime checks happen in `aiops/policy/gate.py::DEFAULT_LEVELS`. Both must agree — keep them in sync until OPA is wired in.
- **Phase 1:** approver function in the gate is wired to a Slack interaction or web UI so OPTIONAL/REQUIRED actions can actually be approved.
- **Phase 2:** OPA runs as a sidecar (or in-process via `opa-python-client`); the gate evaluates `hitl.rego` instead of consulting `DEFAULT_LEVELS`. From this phase on, `hitl.rego` is the authority.

## Adding a new action

1. Add a row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx` (or use the existing one).
2. Add a `level := "..."` rule in `hitl.rego` keyed by `input.action`.
3. Mirror it in `aiops/policy/gate.py::DEFAULT_LEVELS` until Phase 2 lands.
4. If the action is destructive or high-blast-radius, set `requires_hitl=True` on the corresponding tool in `aiops/tools/`.

## Testing

OPA's own test framework will run via `opa test policies/` in Phase 2. Until then, `tests/test_smoke.py` exercises the in-process gate.
