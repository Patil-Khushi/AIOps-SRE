# `agents/` — one directory per agent

**Phase 0: this directory is intentionally empty of implementations.** The POC guide (§8.1) excludes agents from Phase 0; the first ones land in Phase 1 (`Alert Triage`, `Auto-Ticketing`, `Notification Router`, `Log Correlation`).

When you add an agent, the contract for it lives in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`. Read its row before writing code — that row is the source of truth for: description, key features, primary tool mapping, secondary integrations, inputs, outputs, HITL level, KPI.

## Layout convention

```
agents/
├── ra-001-alert-triage/
│   ├── README.md           # short contract summary + how to run locally
│   ├── agent.py            # entry point
│   ├── prompts/
│   │   └── system.md
│   └── evals/
│       ├── golden.json     # hand-built test cases
│       └── README.md       # how to score, what failure means
├── ra-003-auto-ticketing/
│   └── ...
└── prs-008-rca/
    └── ...
```

Directory naming: `<phase-prefix>-<id>-<slug>` matching the catalog (`RA-001` → `ra-001-alert-triage`).

## Adding an agent (checklist)

1. Find the agent's row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`. Read it.
2. Create the directory above with `agent.py`, `prompts/`, `evals/golden.json`, `README.md`.
3. Wire LLM calls through `aiops.llm.complete` / `acomplete`. **No direct SDK imports.**
4. Wire tool calls through `aiops.tools.get_registry().call(...)`. **No direct vendor SDKs.**
5. For destructive or high-blast-radius actions, set `requires_hitl=True` on the tool and call `aiops.policy.get_gate().enforce(...)` before invocation.
6. Build the eval set in the same week. A prompt change is a model change — re-run.
7. Set the agent's HITL level in `policies/hitl.rego` to match the catalog.
8. If you introduce a failure scenario for testing, add a truth file under `demo/truth_files/`.

## What goes in `agent.py`

Keep it small — the prompt is the brain, the framework around it is what makes it shippable. Most agents are: parse input → optionally retrieve context via tools → call LLM → validate output → emit structured result → log.

Don't add a fancy class hierarchy until at least three agents share a non-trivial method.
