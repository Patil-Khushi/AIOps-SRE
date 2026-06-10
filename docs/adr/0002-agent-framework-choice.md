# ADR-002: Agent framework choice (none, for the POC)

## Status

Accepted — revisit at the Phase 2 / post-POC boundary when multi-agent orchestration becomes
load-bearing.

## Context

The CLAUDE.md reference stack lists the agent framework as "LangGraph (or AutoGen / in-house) —
pick one and stick to it." The architecture deck names a six-component Agentic Runtime
(Planner, Router, Orchestrator, Memory, Tool Registry, Eval Harness).

The POC reality (and explicit scope): 6–10 agents on **one** Reactive→Prescriptive flow,
"end-to-end ugly first, refactor second." Today four Reactive-Active agents ship as plain
functions — `triage()`, `classify()`, `ticket()`, `route()` — and the Reactive flow runs as a
sequence of calls (the dashboard route, then the human), not through an orchestrator. Of the
six runtime components, only **Tool Registry** (`aiops/tools`) and **Eval Harness** (`evals/`)
exist. No `langgraph`, `autogen`, or `crewai` dependency is in `pyproject.toml`.

Adopting a heavyweight framework now would add a large dependency, a learning curve, and
lock-in — to orchestrate four functions that a `for` loop can run.

## Decision

**Use no external agent framework for the POC.** Agents are plain Python functions behind
three platform seams: the LLM gateway (`aiops.llm`), the tool registry (`aiops.tools`), and
the HITL gate (`aiops.policy`). Multi-agent orchestration is deferred to a thin in-house
`aiops/runtime/orchestrator.py` (a v0 that runs the Reactive flow as explicit steps —
issue #74). The framework decision (LangGraph vs in-house) is explicitly re-opened when an
agent flow needs graph state, checkpointing, or dynamic routing the hand-wired version can't
carry.

## Consequences

- **Easier:** zero framework lock-in; agents are trivially unit-testable; the seams stay thin
  and the per-agent contract (input/output schema) is the only coupling; no heavy dependency
  on a 16 GiB laptop.
- **Harder:** no built-in Planner/Router/Memory — flows are hand-wired, and as the agent count
  grows the wiring cost grows with it; we carry the risk of building an ad-hoc framework by
  accretion if we don't revisit this deliberately.
- **We now can't:** lean on framework features (persisted graph state, automatic retries,
  visual flow debugging) until we adopt one — which this ADR commits us to re-evaluating
  rather than drifting into.
