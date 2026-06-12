"""Agentic AI Runtime components.

The architecture deck names six runtime components — Planner, Router,
Orchestrator, Memory, Tool Registry, Eval Harness. In Phase 1 only two are
real: the **Tool Registry** (``aiops.tools``) and the **Eval Harness**
(``evals/``). This package lands the **Orchestrator** (INFRA-2, issue #74):
a thin, explicit in-house v0 that runs the Reactive-Active flow as a
sequence of agent calls — no framework, no DSL, just the seam the
retrospective flagged as missing (``docs/architect_retrospective_phase1.md``
§5, issue #18).

Planner / Router / Memory remain deferred to Phase 3 (ADR-002). When
multi-agent orchestration outgrows a straight-line function, the framework
decision (LangGraph vs in-house) is re-opened — until then the seam matters
more than the machinery behind it.
"""
