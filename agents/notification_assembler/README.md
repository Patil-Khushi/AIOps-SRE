# Notification Assembler Agent (RA-005+006)

> Reactive-Active phase · HITL: **Optional** · Sellable standalone: **Yes**
> KPI: **Time-to-notify**, **Time-to-bridge**, **SME coverage %**

Merges the former **Notification Router (RA-005)** and **War-Room Assembler
(RA-006)** into one agent. An operator now gets **one** notification per
incident — the routing message *with* the war-room join link inline — instead
of two separate posts.

## What it does

For every `TriageVerdict` from RA-001 (ticketed by RA-003), in one pass:

1. **Routes the notification** — page on-call, chat the owning team, post to a
   daytime channel, or quietly log to a noise bucket, based on severity,
   time-of-day, and ownership.
2. **Stands up the war room on Sev-1 / Sev-2** — a dedicated chatops channel,
   the on-call SME, a live context pack (current metrics + recent traces), and
   a seed timeline for RCA. Lower severities get **no** room.
3. **Emits exactly one chatops message** — the routing notification, with the
   war-room channel + join link + invited SMEs folded into the same body when a
   room was created.

Sev-3 / Sev-4 verdicts get the plain notification (`war_room.assembled=False`).
Suppressed verdicts (RA-001 duplicate cluster) short-circuit *both* the emit and
the room — the same no-op the two predecessors had.

## Where it sits in the pipeline

```
RA-003 Auto-Ticketing  →  RA-005+006 Notification Assembler  →  RA-007 Log Correlation
                          (notify ONE human + gather the team       (find the culprit)
                           in ONE message)
```

It consumes a `TriageVerdict` from RA-001 (Alert Triage), enriched downstream by
RA-003 (Auto-Ticketing) with an `incident_id`. Its war-room channel + timeline +
context pack become the working surface for RA-007 and the RCA Agent.

## Contract

| | |
|---|---|
| **Input** | `TriageVerdict` (from RA-001/RA-003) + optional `now` for deterministic tests |
| **Output (`decide`)** | `NotificationAssembly` — the `RoutingDecision` + optional `WarRoomAssembly` (`None` only when Suppressed; `assembled=False` below Sev-2) |
| **Output (`notify`)** | `NotificationOutcome` — decision + war room + per-adapter `deliveries` for the single message |
| **Side effect** (`notify` only) | (1) Sev-1/Sev-2: creates the Slack war room via the `chatops.war_room.create` seam (simulated without a bot token); (2) sends **one** `ChatMessage` through the chatops seam |

### Public surface

- `decide(verdict, now=None) -> NotificationAssembly` — **pure**, no side effects.
- `notify(verdict, now=None) -> NotificationOutcome` — creates the bridge (Sev-1/2) then emits the single combined message.
- `decide_war_room` / `assemble_war_room` — war-room-only helpers for the dashboard try-it inspector.
- `run(payload) -> dict` — eval-harness entry point (pure; flat dict with routing fields + `war_room_*` fields).

### Seams used (never call vendors directly — CLAUDE.md principle #1)

| Need | Capability | Notes |
|---|---|---|
| Send the notification | `aiops.tools.chatops.get_client().send(...)` | One message per incident |
| Create the war-room channel | `chatops.war_room.create` | Real Slack with a bot token; simulated otherwise |
| On-call / skill matrix | `oncall.schedule.lookup` | Resolved **once**, feeds mentions, body, and SME invites |
| Context pack data | `observability.metrics.query`, `observability.traces.search` | Snapshot only — deep correlation is RA-007's job |

## HITL = Optional

Routing a message and opening a room are non-destructive, so there is **no**
approval gate inside the agent (principle #3 — HITL is platform-enforced, never
agent-coded).
