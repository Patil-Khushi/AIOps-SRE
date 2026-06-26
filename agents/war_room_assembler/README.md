# War-Room Assembler Agent (RA-006)

> Reactive-Active phase · HITL: **Optional** · Sellable standalone: **Yes**
> KPI: **Time-to-bridge**, **SME coverage %**

On a **Sev-1 / Sev-2** incident, stands up the incident war room: a dedicated
chatops channel, the on-call SME pulled in, a live context pack (current metrics
+ recent traces), and a seed timeline for RCA. Sev-3 / Sev-4 and Suppressed
verdicts get a no-op assembly (`assembled=False`).

## Standalone vs integrated

This is an **individually sellable unit**: license RA-006 alone and it stands up
war rooms with no notification-routing dependency. The implementation is shared
with RA-005 in `agents/notification_assembler/` (single source of truth); this
package is a thin wrapper exposing RA-006's original contract and delegating to
it.

In the **integrated product flow** the platform calls
`notification_assembler.notify`, which creates the war room and folds its join
link into the single routing notification. Deployed standalone, RA-006 posts the
war-room opening to its own channel.

## Contract

| | |
|---|---|
| **Input** | `TriageVerdict` (from RA-001/RA-003) + optional `now` for deterministic tests |
| **Output (`decide`)** | `WarRoomAssembly` — channel, invited SMEs (+ why), context pack, timeline, `audit_trace` |
| **Output (`assemble`)** | `WarRoomOutcome` — assembly (bridge-enriched) + per-adapter `deliveries` |
| **Side effect** (`assemble` only) | Creates the Slack war room (`chatops.war_room.create`; simulated without a token) and posts the opening |

## Public surface

- `decide(verdict, now=None) -> WarRoomAssembly` — **pure**, no side effects.
- `assemble(verdict, now=None) -> WarRoomOutcome` — decide, create bridge, emit.
- `run(payload) -> dict` — eval-harness entry point (pure).
