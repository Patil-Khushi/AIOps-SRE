# `evals/` — eval harness

Hand-rolled JSON test cases. Per the POC guide §9.5: **build the eval set in the same week you build the agent.** A prompt change is a model change — re-run.

## How an agent plugs in

Each agent ships its eval cases beside its code:

```
agents/alert_triage/
├── agent.py                # exposes ``run(input: dict) -> dict``
└── evals/
    └── golden.json
```

`golden.json` is a list of cases (or a `{"cases": [...]}` wrapper carrying top-level metadata):

```json
[
  {
    "id": "single-cpu-alert",
    "description": "One CPU alert deduped against a recent identical alert.",
    "input": {
      "alert": {
        "id": "ALERT-101",
        "title": "CPU > 90% on inv-app-07",
        "fired_at": "2026-05-06T09:42:00Z"
      }
    },
    "expected": {
      "severity_in": ["Sev-2", "Sev-3"],
      "decision_trace_contains": "dedup",
      "min_confidence": 0.5
    },
    "tags": ["dedup", "phase-1"]
  }
]
```

## Running

```powershell
uv run python -m evals.harness                       # all agents
uv run python -m evals.harness --agent alert_triage
uv run python -m evals.harness --ci --min-pass-rate 0.85
```

Phase 0 has no agents yet, so the harness emits an empty-but-passing summary. CI uses this to confirm the wiring works.

## Supported checks (`expected`)

Each `expected` block is a **flat dict** whose keys encode both the target field and the
check type via a suffix grammar (see `evals/scoring.py`):

| Key form | Means |
|---|---|
| `<field>` | exact equality: `actual[<field>] == want` |
| `<field>_in: [list]` | membership: `actual[<field>] in [...]` |
| `<field>_contains: value` | substring (str) or element (list) containment in `actual[<field>]` |
| `min_<field>: number` | numeric `actual[<field>] >= number` |
| `max_<field>: number` | numeric `actual[<field>] <= number` |

A case **passes** only when every check in its `expected` block passes (`score` is the
fraction that passed; `passed` requires all). The full methodology — scoring, pass-rate
definition, CI gating, champion/challenger, per-agent status — lives in
[`../EVAL_METHODOLOGY.md`](../EVAL_METHODOLOGY.md).

Add new checks in `evals/scoring.py` only when a real agent forces it. Don't pre-build for hypotheticals.

## Truth files vs golden cases

- **`evals/golden.json`** lives next to the agent and tests the agent in isolation.
- **`demo/truth_files/<scenario>.yaml`** describes a full demo scenario (failure injected into the OTel demo, expected RCA, expected fix steps). Multiple agents are scored against the same truth file.

Both are required. The catalog row tells you which checks matter for which agent.
