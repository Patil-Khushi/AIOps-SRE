# RA-005 — Notification Router

The smart dispatcher between triaged alerts and humans. Reads a
`TriageVerdict` from RA-001 (Alert Triage), applies context-aware rules,
and emits one `ChatMessage` through the chatops seam. Anti-alert-fatigue
is its job.

```
   ┌──────────────┐    TriageVerdict    ┌──────────────┐   ChatMessage   ┌────────────────┐
   │  RA-001      │ ──────────────────► │  RA-005      │ ──────────────► │ chatops seam   │
   │  Alert Triage│                     │  (this agent)│                 │ → React panel  │
   └──────────────┘                     └──────────────┘                 │ → JSON audit   │
                                                                          │ → Slack/Teams* │
                                                                          └────────────────┘
                                                                                * = post-POC
```

## Why it exists

The on-call engineer's phone rings 30 times a night. 28 of those are
noise. By the time a real outage fires, they've stopped trusting any of
them. RA-005 looks at *context* — severity, time of day, ownership — and
decides whether each alert deserves a phone call, a chat message, or
silent logging.

## Routing rules (v1)

All decisions are pure functions of `(severity, time-of-day, ownership)`.
No LLM in v1 — deterministic, auditable, fast.

| Triage severity | Time | → Channel | Chat severity | Actions | Mention on-call? |
|---|---|---|---|---|---|
| Sev-1 | any | `#incidents` | P1 | page + chat | yes |
| Sev-2 | business hrs (UTC 9-18) | `team-<slug>` | P2 | chat | yes |
| Sev-2 | after hours | `#incidents` | P2 | page + chat | yes |
| Sev-3 | any | `#ops-daytime` | P3 | chat | no (anti-fatigue) |
| Sev-4 | any | `#alerts-noise` | INFO | chat | no |

The team slug is derived from `assigned_team` — `"Order Experience"` →
`team-order-experience`.

## Public API

```python
from agents.notification_router import decide, route, run, RoutingDecision

# Pure decision — no side effects, safe to call in tests
decision: RoutingDecision = decide(verdict, now=datetime.now(UTC))

# Decide + emit through the chatops seam (this is what /api/triage uses)
route(verdict)

# Eval-harness entry: takes a dict, returns a JSON-friendly dict
result_dict = run({"verdict": {...}, "now": "2026-05-13T11:00:00Z"})
```

## File map

| File | Purpose |
|------|---------|
| `agent.py` | `decide`, `route`, `run` |
| `models.py` | `RoutingDecision` Pydantic model |
| `__main__.py` | CLI — `cat verdict.json \| python -m agents.notification_router` |
| `__init__.py` | Public re-exports |
| `evals/golden.json` | 5 cases covering every routing branch |

## How it plugs in

- **Upstream:** `demo/ui/server.py` calls `route(verdict)` after every
  successful `triage()` in `/api/triage` and `/api/triage/live`.
- **Downstream:** `route()` drops one `ChatMessage` into
  `aiops.tools.chatops.get_client().send()`. The seam fans it to every
  registered adapter (WebSocket → React panel, JSON file → audit log,
  and any future Slack / Teams / PagerDuty adapters).
- **Side effects:** none in `decide()`; one chatops emit in `route()`.
- **HITL:** none — autonomy level is "None" per the agent catalog.

## Testing

```powershell
# Unit + integration tests
uv run --extra dev pytest tests/test_notification_router.py -v

# Eval harness (golden.json)
uv run --extra dev python -m evals.harness --agent notification_router
```

Current coverage:

- 10 unit tests on `decide()` — one per routing branch + edge cases
  (missing engineer, dup count, audit trace, team-name slugging)
- 2 end-to-end tests on `route()` — verifies the seam fan-out and that
  `incident_id` propagates through
- 5 parametric golden cases covering all four severities

## Demo

Repeatable demo scripts live in `scripts/demo/`. After starting the
server:

```powershell
.\scripts\demo\fire-all.ps1
```

Fires one fixture per severity and you should see four cards appear in
`/dashboard/notifications`, each in a different channel.

## Known v1 limitations (intentional)

Each item below is a clean v2 increment because the seam architecture is
already in place. None are blockers for the POC.

- **Template-based message body** — no LLM-driven prose.
- **No on-call load awareness** — primary on-call always gets the page;
  no "Raj already paged 3 times tonight → escalate to Meera" logic.
- **No escalation ladders** — no automatic escalation after N minutes
  with no ack.
- **UTC-hardcoded business hours** — all teams use 09:00–18:00 UTC.
  Per-team timezones are v2.
- **"page_oncall" is descriptive, not active** — until a real PagerDuty
  / Twilio adapter is added to the seam, the action is logged but no
  phone rings.
- **No dedup window** — same alert fired 20 times in 2 min currently
  produces 20 notifications.

## Where this fits in the catalog

- **Phase:** Reactive-Active
- **Step in pipeline:** 10 (Send notification)
- **Catalog ID:** RA-005
- **HITL level:** None
- **Primary tools (post-POC):** PagerDuty, ServiceNow, Microsoft Teams,
  Slack — each plugs in as a chatops adapter without agent changes.
- **KPIs:** acknowledgement latency, escalation rate.
