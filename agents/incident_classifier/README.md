# RA-002 — Incident Classifier (skeleton)

Reactive-Active phase. Takes a triaged incident from RA-001 and assigns it a
category so downstream routing (Auto-Ticketing, runbook selection, RCA) can
pick the right path.

**Status:** skeleton only. `classify()` raises `NotImplementedError`. The
authoritative contract is the catalog row in
`docs/Adaptive_AIOps_Agent_Catalog.xlsx` (Reactive-Active sheet) — read it
before filling this in.

## Public surface

```python
from agents.incident_classifier import (
    Classification,
    ClassificationInput,
    IncidentType,
    classify,
)
```

### Contract

| Symbol | Shape |
|---|---|
| `ClassificationInput` | `{ triage_verdict: TriageVerdict }` — wraps RA-001's output, the documented seam between the two agents. |
| `IncidentType` | Literal of `infrastructure` \| `application` \| `network` \| `external_dependency` \| `change_related`. |
| `Classification` | `{ incident_type, confidence (0..1), rationale, tags: list[str] }`. |
| `classify(payload)` | `ClassificationInput → Classification`. Raises `NotImplementedError` in v0. |

## Run locally

```powershell
uv run python -m agents.incident_classifier --list
uv run python -m agents.incident_classifier --fixture <id>      # raises NotImplementedError in v0
```

## Layout

```
agents/incident_classifier/
├── README.md
├── __init__.py
├── __main__.py          # CLI runner
├── agent.py             # entry point: classify()
├── models.py            # Incident, IncidentVerdict, AuditMetadata
├── prompts.py           # SYSTEM_PROMPT, CLASSIFY_PROMPT_USER (placeholders)
└── evals/
    └── golden.json      # empty until v1
```

## Done when (skeleton ticket)

- [x] Directory exists with the files above.
- [x] `from agents.incident_classifier import classify` resolves.

## Next (out of scope for this ticket)

1. Read the catalog row for RA-002. Lock the input/output contract.
2. Fill in `models.py` against that contract.
3. Implement `classify()` — rule-based first, LLM consult for ambiguous cases
   (mirror the pattern in `agents/alert_triage/agent.py`).
4. Populate `evals/golden.json` in the same week (CLAUDE.md principle #7).
5. Set the HITL level in `policies/hitl.rego` to match the catalog.
