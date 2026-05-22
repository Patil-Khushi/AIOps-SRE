# RCA Agent — PRS-008 ★

The catalog's headline differentiator. Takes a triage verdict for a degraded
service and produces a structured `RCAVerdict` with the root cause and a small
ranked list of **reversible** fix steps. Every fix step carries `blast_radius`
+ `rollback` and is tagged `requires_hitl=true` — the platform HITL gate
enforces approval at the action boundary (this agent does not gate-check
itself).

| | |
|---|---|
| Catalog ID | PRS-008 ★ (starred — headline) |
| Phase | Prescriptive-Adaptive |
| HITL level | **Required** for every fix step (per catalog + Solution Design slide 10) |
| Inputs | `RCAInput` = `{ triage_verdict: <RA-001 dict>, scenario_id?: str }` |
| Outputs | `RCAVerdict` — see [models.py](models.py) |
| LLM | via `aiops.llm.complete` (provider-agnostic) |
| Deterministic fallback | yes — slow-product-catalog truth-file-derived verdict |

## v0 scope (locked — see [DEMO_PLAN.md](../../DEMO_PLAN.md))

- Single scenario: `slow-product-catalog`
- Single prompt template (`SYSTEM_PROMPT_V1` in [prompts.py](prompts.py))
- One golden case ([evals/golden.json](evals/golden.json))
- Pass-rate target: ≥ 0.6 in W1, ≥ 0.85 in W2 after prompt tuning
- **No retrieval phase** (W2)
- **No safety.py command allow-list** (W2)
- **No fix-step execution** (post-POC — that's Auto-Healer / Runbook Executor)

## Run locally

```powershell
# Single eval pass against the v0 golden
uv run python -m evals.harness --agent rca_agent

# As part of the full sweep (gates CI)
uv run python -m evals.harness
```

The agent works without LLM credentials — the deterministic fallback covers
the locked scenario so CI passes on a stub provider. The LLM path is what
W2's prompt-tuning work moves the pass rate on.
