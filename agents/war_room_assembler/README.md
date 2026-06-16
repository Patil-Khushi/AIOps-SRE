# War-Room Assembler Agent (RA-006)

> Reactive-Active phase · HITL: **Optional** · Sellable standalone: **Yes**
> KPI: **Time-to-bridge**, **SME coverage %**

## What it does

On a **Sev-1 / Sev-2** incident, this agent stands up the incident "war room"
so humans don't have to scramble. It:

1. **Creates a bridge / channel** — a dedicated chatops channel for the incident.
2. **Invites the right SMEs** — picks experts by looking up the impacted
   Configuration Items (CIs) in the CMDB and the on-call/skill matrix.
3. **Posts a live context pack** — a one-shot snapshot of current metrics,
   recent traces, and recent changes so everyone starts from the same picture.
4. **Starts a timeline** — a timestamped event log the RCA / Incident Commander
   agents continue later.

Lower severities (Sev-3 / Sev-4) do **not** warrant a war room — the agent
returns a no-op decision for them, the same way RA-005 short-circuits
suppressed verdicts.

## Where it sits in the pipeline

```
RA-005 Notification Router  →  RA-006 War-Room Assembler  →  RA-007 Log Correlation
   (page ONE human)             (gather the WHOLE team)        (find the culprit)
```

It consumes the same upstream object as RA-005 — a `TriageVerdict` from RA-001
(Alert Triage), enriched downstream by RA-003 (Auto-Ticketing) with an
`incident_id`. Its output (channel + timeline + context pack) becomes the
working surface for RA-007 and the RCA Agent.

## Contract

| | |
|---|---|
| **Input** | `TriageVerdict` (from RA-001/RA-003) + optional `now` for deterministic tests |
| **Output** | `WarRoomAssembly` — bridge channel, invited SMEs (+ why), context pack, timeline, `audit_trace` |
| **Side effect** (`assemble` only) | Posts the war-room opening + context pack through the chatops seam |

### Seams used (never call vendors directly — CLAUDE.md principle #1)

| Need | Capability | Notes |
|---|---|---|
| Create/post war-room channel | `aiops.tools.chatops.get_client().send(...)` | Bridge = a dedicated channel `war-room-<incident>` |
| Impacted CIs + owners | `itsm.cmdb.lookup`, `itsm.cmdb.dependencies` | Drives SME selection |
| On-call / skill matrix | `oncall.schedule.lookup` | Reused from RA-005 |
| Context pack data | `observability.metrics.query`, `observability.traces.search` | Snapshot only — deep correlation is RA-007's job |
| Timeline → ticket | `itsm.incident.update`, `itsm.incident.attachment.add` | Transcript export |

## Design idiom (mirrors `notification_router`)

- `decide(verdict, now=None) -> WarRoomAssembly` — **pure**, no side effects, fully testable.
- `assemble(verdict, now=None) -> WarRoomOutcome` — calls `decide`, then emits through the seams.
- `run(payload) -> dict` — eval-harness entry point (pure, no live side effects).
- `audit_trace: list[str]` — one line per decision, for end-to-end explainability.

## HITL = Optional

Assembling a room is non-destructive, so there is **no** approval gate inside
the agent (principle #3 — HITL is platform-enforced, never agent-coded). "Optional"
means an operator *can* be asked to confirm SME invites, but the platform gate
decides that, not this code.
