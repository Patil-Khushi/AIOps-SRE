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
| LLM | via `aiops.llm.complete` — pinned to **Anthropic Claude Sonnet 4.6** through the Azure AI Foundry deployment (see "Why not Azure OpenAI" below) |
| Deterministic fallback | yes — slow-product-catalog truth-file-derived verdict |

## v0 scope (locked — see [DEMO_PLAN.md](../../DEMO_PLAN.md))

- Single scenario: `slow-product-catalog`
- Single prompt template (`SYSTEM_PROMPT_V3` in [prompts.py](prompts.py))
- One golden case ([evals/golden.json](evals/golden.json))
- Pass-rate target: ≥ 0.6 in W1, ≥ 0.85 in W2 after prompt tuning
- **No retrieval phase** (W2)
- **No safety.py command allow-list** (W2)
- **Fix-step execution is platform-side, not agent-side.** The agent annotates
  each step with a machine-readable `action_type` (`set_flag` / `rollback_deploy`
  / `manual`) so the platform executor ([aiops/tools/rca_remediation.py](../../aiops/tools/rca_remediation.py))
  can *follow the recommended step* — gated by the REQUIRED-HITL
  `rca.fix_step.execute` capability. v0 executes `set_flag` only; the agent
  still never acts on its own (CLAUDE.md #3). `rollback_deploy` / `manual` are
  advisory until their executors land (Auto-Healer / Runbook Executor).

## Why not Azure OpenAI for this agent

The platform default is Azure OpenAI (`gpt-5`) and that works fine for the
lighter agents (alert_triage, classifier). The RCA agent gets the **full**
decision-trace from RA-001 fed into its prompt, which has the structural
shape of clinical-lab report content — tagged IDs, parenthesized severity
scores, biomarker-shaped metric labels. Azure's content filter (tuned for
regulated tenants) classifies that as `self_harm: severity=medium` and
deterministically rejects the call.

Per-agent provider override solves it. The agent calls
`aiops.llm.complete(provider="anthropic", model="claude-sonnet-4-6", ...)`,
routed through the same Foundry endpoint your Azure resource already hosts.
Two env vars exposed for switching back if needed:

```bash
AIOPS_RCA_LLM_PROVIDER  # default: anthropic
AIOPS_RCA_LLM_MODEL     # default: claude-sonnet-4-6
```

The `ANTHROPIC_BASE_URL` (Foundry endpoint) carries forward from the global
`.env`. The Anthropic SDK is installed via `uv sync --extra llm-anthropic`
(included in `--extra dev`).

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
