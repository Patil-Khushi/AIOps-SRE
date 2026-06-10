# ADR-005: Policy engine

## Status

Accepted (direction). OPA is the chosen engine; wiring it as the runtime authority is a
Phase 2 step. Until then it runs reference-only alongside an in-process default.

## Context

CLAUDE.md non-negotiable #4 (and Solution Design §2.4): every action passes through a
declarative policy layer before execution, and that policy lives in Git, reviewed like code.
The first policy this governs is the HITL autonomy level per action — None / Optional /
Required — which mirrors the agent catalog.

The candidates were a hand-rolled policy check in Python versus a dedicated policy engine.
A hand-rolled check is faster to ship but couples policy to code: changing a rule means a
code review of application logic, and there's no independent test surface or audit of the
policy itself.

Today the levels live in two places that must agree: `policies/hitl.rego` (the declarative,
reference copy) and `aiops/policy/gate.py::DEFAULT_LEVELS` (the in-process map the gate
actually consults at runtime).

## Decision

**Open Policy Agent (OPA), with Rego, is the policy engine.** It is industry-standard,
Git-reviewable, decouples policy from application code, and brings its own test framework
(`opa test policies/`). Phase progression:

- **Now (POC):** `hitl.rego` is reference-only; the gate enforces `DEFAULT_LEVELS`. The two
  are kept in sync by hand, and the smoke test exercises the in-process gate.
- **Phase 2:** OPA runs as a sidecar (or in-process via `opa-python-client`); the gate
  evaluates `hitl.rego`, which becomes the single authority. `DEFAULT_LEVELS` is retired.

## Consequences

- **Easier:** policy is reviewed and versioned independently of code; one declarative source
  of authority once Phase 2 lands; OPA's test framework grades the policy directly.
- **Harder:** until Phase 2 there are **two** sources of truth (rego + `DEFAULT_LEVELS`) that
  can silently drift — adding an action means editing both; OPA adds a runtime dependency /
  sidecar to operate.
- **We now can't:** honestly claim `hitl.rego` is load-bearing yet — `DEFAULT_LEVELS` is the
  real gate today. Treat the rego file as the spec the runtime will be promoted to, and keep
  them in lockstep until then.
