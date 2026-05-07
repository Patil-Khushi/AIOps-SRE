# `evals/` — eval harness

Hand-rolled JSON test cases. Per the POC guide §9.5: **build the eval set in the same week you build the agent.** A prompt change is a model change — re-run.

## How an agent plugs in

Each agent ships its eval cases beside its code:

```
agents/ra-001-alert-triage/
├── agent.py                # exposes ``run(input: dict) -> dict``
└── evals/
    └── golden.json
```

`golden.json` is a list of cases:

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
      },
      "recent_alerts": [
        {"id": "ALERT-100", "title": "CPU > 90% on inv-app-07",
         "fired_at": "2026-05-06T09:40:00Z"}
      ]
    },
    "expected": {
      "has_keys": ["incident_id", "severity", "owner_team"],
      "field": {"name": "severity", "check": "matches", "value": "Sev-3"}
    },
    "tags": ["dedup", "phase-1"]
  }
]
```

## Running

```powershell
uv run python -m evals.harness                       # all agents
uv run python -m evals.harness --agent ra-001-alert-triage
uv run python -m evals.harness --ci --min-pass-rate 0.85
```

Phase 0 has no agents yet, so the harness emits an empty-but-passing summary. CI uses this to confirm the wiring works.

## Supported checks (`expected`)

| Check | Means |
|---|---|
| `equals` | strict equality with `actual` |
| `contains` | substring of a string `actual`, or element of a list/set |
| `has_keys` | `actual` is a dict containing every listed key |
| `matches` | case-insensitive equality (string only) |
| `field` | nested check against `actual[name]`: `{name, check, value}` |

Add new checks in `evals/scoring.py` only when a real agent forces it. Don't pre-build for hypotheticals.

## Truth files vs golden cases

- **`evals/golden.json`** lives next to the agent and tests the agent in isolation.
- **`demo/truth_files/<scenario>.yaml`** describes a full demo scenario (failure injected into the OTel demo, expected RCA, expected fix steps). Multiple agents are scored against the same truth file.

Both are required. The catalog row tells you which checks matter for which agent.
