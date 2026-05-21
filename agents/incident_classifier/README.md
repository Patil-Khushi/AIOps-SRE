# RA-002 — Incident Classifier

Reactive-Active phase. Takes a triaged alert from RA-001 and assigns it a
category so downstream routing (Auto-Ticketing, runbook selection, RCA) can
pick the right path.

**Status:** Phase 1 shipped (PR #48). `classify()` runs a tiered rule →
LLM → fallback pipeline with persistence + a standalone React dashboard at
`/classifier`. The authoritative contract is the catalog row in
`docs/Adaptive_AIOps_Agent_Catalog.xlsx` (Reactive-Active sheet).

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
| `ClassificationInput` | `{ alert: Alert, triage_verdict: TriageVerdict }` — both the upstream alert and RA-001's verdict; the classifier reads labels from one and ownership/severity from the other. |
| `IncidentType` | Literal of `infrastructure` \| `application` \| `network` \| `external_dependency` \| `change_related`. |
| `Classification` | `{ incident_type, confidence (0..1), rationale, tags: list[str] }`. |
| `classify(payload)` | `ClassificationInput → Classification`. Multi-tier: deterministic rules first, LLM consult for ambiguous cases, fallback when the LLM gateway is unreachable. |

## Run locally

```powershell
uv run python -m agents.incident_classifier --list
uv run python -m agents.incident_classifier --fixture <id>
uv run python -m evals.harness --agent incident_classifier
```

The standalone classifier dashboard ships under [`demo/classifier-ui/`](../../demo/classifier-ui/) and is served by the FastAPI demo app at `/classifier` once `start.ps1` builds it.

## Layout

```
agents/incident_classifier/
├── README.md
├── __init__.py
├── __main__.py          # CLI runner
├── agent.py             # entry point: classify() + tiered pipeline + state persistence
├── models.py            # ClassificationInput, Classification, IncidentType
├── prompts.py           # SYSTEM_PROMPT, CLASSIFY_PROMPT_USER
└── evals/
    └── golden.json      # hand-built golden cases
```
