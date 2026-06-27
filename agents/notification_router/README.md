# Notification Router Agent (RA-005)

> Reactive-Active phase · HITL: **Optional** · Sellable standalone: **Yes**
> KPI: **Time-to-notify**, **Notification accuracy**

Routes **one** notification per incident to the right people and channel — page
on-call (Sev-1, or Sev-2 after hours), chat the owning team (Sev-2 in hours),
post to a daytime triage channel (Sev-3), or quietly log to a noise bucket
(Sev-4). Suppressed verdicts (RA-001 duplicate cluster) emit nothing.

## Standalone vs integrated

This is an **individually sellable unit**: license RA-005 alone and it routes
notifications with no war-room dependency. The implementation is shared with
RA-006 in `agents/notification_assembler/` (single source of truth); this
package is a thin wrapper exposing RA-005's original contract and delegating to
it.

In the **integrated product flow** the platform calls
`notification_assembler.notify`, which folds the RA-006 war-room link into this
same routing message so the operator gets one message instead of two. Deployed
standalone, RA-005 emits the routing message only.

## Contract

| | |
|---|---|
| **Input** | `TriageVerdict` (from RA-001) + optional `now` for deterministic tests |
| **Output (`decide`)** | `RoutingDecision` — channel, severity, body, mentions, actions, `audit_trace` |
| **Output (`route`)** | `RoutingOutcome` — decision + per-adapter `deliveries` |
| **Side effect** (`route` only) | Sends one `ChatMessage` through the chatops seam |

## Public surface

- `decide(verdict, now=None) -> RoutingDecision` — **pure**, no side effects.
- `route(verdict, now=None) -> RoutingOutcome` — decide, then emit.
- `run(payload) -> dict` — eval-harness entry point (pure).
