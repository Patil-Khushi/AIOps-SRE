# Demo MVP Plan — 3.5 Weeks to Demo-Ready

**Target demo date:** Friday 2026-06-14
**Today:** 2026-05-21 — W0 (planning)
**Status:** Reactive backbone shipped. Headline (RCA + HITL) and the phone-buzz beat (PagerDuty) remain.

---

## The 6-minute demo we're rehearsing for

| Time | What the audience sees |
|---|---|
| 0:00 | "OpenTelemetry Astronomy Shop on Rancher Desktop k3s — Prometheus, Jaeger, ServiceNow PDI, Slack, PagerDuty all live." Open the dashboard. |
| 0:10 | Click **Inject** on `payment_failure` (Sev-1). |
| 0:15 | **Slack channel** lights up with a red Block Kit alert. |
| 0:20 | **Lead's phone buzzes** — real PagerDuty push notification. |
| 0:25 | **ServiceNow incident** appears with severity, on-call, runbook, decision trace. |
| 1:00 | RCA Agent posts to Slack: *"Root cause: `paymentFailure` flag at 100%. Fix: set flag to off. Rollback: re-flip. Blast radius: low. **Approve?**"* |
| 1:15 | SRE taps **Approve** in Slack → action runs → confirmation message posted. |
| 1:30 | Show `demo/audit/chatops.jsonl` — *"every action the platform took is here, queryable."* |
| 2:00 | Inject `slow-product-catalog` (Sev-2) to demonstrate the second scenario end-to-end. |
| 4:00 | Wrap: the 8 non-negotiable design principles → show how each one is exercised in the live demo. |

---

## What's already shipped (don't touch)

| Capability | Closed by | Verify with |
|---|---|---|
| Alert Triage with verdict + dedup | RA-001 | Dashboard → "Alert Stream" panel |
| ServiceNow ticket creation with rich description | RA-003 + DEMO-3 | Open any incident in the PDI |
| Slack notifications (one-way, Block Kit) | CHAT-1 | `#aiops-poc-alerts` channel |
| Notification persistence to SQL | CHAT-2 | `GET /api/notifications` |
| Feature-flag mutation seam | ARCH-1 | `aiops/tools/feature_flags/` |
| Truth-file eval wiring | EVAL-1 | `uv run python -m evals.harness --truth-files-only` |
| Scenarios consolidated under one dir | DEMO-12 | `demo/scenarios/` (single source of truth) |
| start.ps1 builds both SPAs + propagates env | DEMO-10, ARCH-1 | `.\start.ps1` from fresh clone |
| Sprint 0 polish: docs, gitignore, classifier-ui build | DEMO-6/7/13/14 + B7 | RUNNING.md |

---

## What still needs to ship (5 issues, ~8.5 dev-days)

Three parallel tracks. Each track has one owner so there's no merge contention.

### Track A — The headline (the wow moment)

The RCA Agent is the product's differentiator. v0 ships scoped to a single scenario so we can polish the prompt + eval pass rate without scope creep.

| # | Item | Owner | Est. | Depends on |
|---|---|---|---|---|
| [#37](https://github.com/UbiquotousPanda/AIops/issues/37) | PRS-008 RCA Agent ★ v0 on `slow-product-catalog` | **dev-a (@UbiquotousPanda)** | **5 days** | EVAL-1 ✅ |

**v0 scope (locked — do not expand):**
- Single scenario: `slow-product-catalog`
- Single prompt template, versioned in `agents/rca_agent/prompts.py`
- Output: structured `RCAVerdict` with `root_cause`, `ranked_fix_steps` (each carrying `blast_radius` + `rollback`), `confidence_score`
- Fix steps that touch the cluster are tagged `requires_hitl=true` → Track B's gate catches them
- Eval: golden case against `demo/truth_files/slow-product-catalog.yaml`; pass rate ≥ 0.6 in W1, ≥ 0.85 in W2 after prompt tuning
- HITL level: **Required** for every fix step (matches CLAUDE.md non-negotiable #3)

### Track B — The HITL gate (the SRE-is-in-the-loop moment)

When the RCA Agent proposes a destructive fix, the platform blocks it until a human approves via Slack. This is the demo beat that proves principle #3 is real, not a slide.

| # | Item | Owner | Est. | Depends on |
|---|---|---|---|---|
| [#77](https://github.com/UbiquotousPanda/AIops/issues/77) | HITL-1 v1 — Slack interactive approve/deny | **dev-c (@Patil-Khushi)** | **2 days** | RCA Agent (loose — mock until W2 link-up) |

**v1 scope (locked):**
- One Required-HITL action wired end-to-end
- Slack bot token (`xoxb-...`) + interactivity request URL on the FastAPI server (`POST /api/slack/interactivity`)
- Block Kit message with **Approve** / **Deny** buttons + reason field
- Approval flips through `aiops.policy.get_gate().enforce()` — agent code does NOT know about the approval check
- Decision logged to `demo/audit/chatops.jsonl` with approver Slack user ID
- Timeout: 10 min (configurable via `AIOPS_HITL_APPROVAL_TIMEOUT_SECONDS`) → auto-deny with reason `"expired"`
- Signature verification on inbound Slack requests (use Slack's signing secret)

### Track C — The phone buzz (lead's PagerDuty requirement)

Sev-1 alerts page on-call via PagerDuty alongside the Slack message. Three small follow-ons that all build on the chatops seam pattern.

| # | Item | Owner | Est. | Depends on |
|---|---|---|---|---|
| [#83](https://github.com/UbiquotousPanda/AIops/issues/83) | CHAT-3 — `actions[]` field on ChatMessage | **dev-d (@Gaurav-Patil-1695)** | 1 h | — |
| [#85](https://github.com/UbiquotousPanda/AIops/issues/85) | CHAT-5 — PagerDuty adapter | **dev-d (@Gaurav-Patil-1695)** | 4 h | #83 |
| [#86](https://github.com/UbiquotousPanda/AIops/issues/86) | CHAT-6 — Slack user-ID mapping (real `<@U12345>` mentions) | **dev-d (@Gaurav-Patil-1695)** | 2 h | CHAT-1 ✅ |

**Track C total:** ~7 hours. Adapter pattern mirrors `SlackWebhookAdapter` exactly — `aiops/tools/chatops/adapters/slack.py` is the reference implementation, follow the same shape.

---

## Timeline by week

| Week | Dates | Goal | Definition of done |
|---|---|---|---|
| **W1** | May 22–28 | RCA scaffolding + first prompt + truth-file eval pass | `uv run python -m evals.harness --agent rca_agent` returns pass rate ≥ 0.6 |
| **W2** | May 29–Jun 4 | HITL UI live in Slack + Track C shipped | Sev-1 inject → real PagerDuty page + Slack approval prompt that gates a real action |
| **W3** | Jun 5–11 | Integration: RCA → HITL → action → audit + 2 demo dry-runs | Full 6-minute demo runs cleanly twice in a row |
| **W3.5** | Jun 12–14 | Rehearsal + recording + buffer | Recorded video walkthrough as a fallback if anything breaks live |

---

## Day-1 setup checklists

### Track B (HITL UI / Slack interactivity) — Khushi

The existing "Adaptive AIOps POC" Slack app needs more scopes than CHAT-1 used:

1. Open https://api.slack.com/apps → "Adaptive AIOps POC" app
2. **Interactivity & Shortcuts** → toggle **On** → request URL = ngrok'd FastAPI server at `/api/slack/interactivity` (we'll provision ngrok in W2)
3. **OAuth & Permissions** → add scopes: `chat:write`, `users:read`, `users:read.email`
4. Reinstall the app to the workspace, copy the new **Bot User OAuth Token** (`xoxb-...`) into local `.env`:
   ```
   AIOPS_SLACK_BOT_TOKEN=<xoxb-...>
   AIOPS_SLACK_SIGNING_SECRET=<from "Basic Information">
   ```

### Track C (PagerDuty) — Gaurav

1. Sign up at https://developer.pagerduty.com (free, no credit card)
2. Create a service → **Integrations** → add "Events API v2" integration
3. Copy the 32-char integration key into local `.env`:
   ```
   AIOPS_PAGERDUTY_INTEGRATION_KEY=<key>
   ```
4. Install the PagerDuty mobile app, log in, add yourself to the service's on-call schedule (so test pages land on your phone)

Each adapter is opt-in via env var (same pattern as `AIOPS_SLACK_WEBHOOK_URL` from CHAT-1) — empty env var means the adapter is skipped at startup. The local demo works without these credentials; they only matter during rehearsals + the live demo.

---

## Backlog — pick up as time permits

Demo doesn't need these but they're real cleanups. **Anyone can claim** any item by adding their dev-X label + assigning themselves. Order by ROI per hour:

| # | Item | Est. |
|---|---|---|
| [#61](https://github.com/UbiquotousPanda/AIops/issues/61) | DEMO-9 — verdict double-save fix | 2 h |
| [#67](https://github.com/UbiquotousPanda/AIops/issues/67) | DEMO-15 — FastAPI lifespan migration (kill deprecation warnings) | 2 h |
| [#57](https://github.com/UbiquotousPanda/AIops/issues/57) | DEMO-5 — Dashboard scenario↔alert mapping UX | 4 h |
| [#63](https://github.com/UbiquotousPanda/AIops/issues/63) | DEMO-11 — `.env.example` overhaul | 2 h |
| [#84](https://github.com/UbiquotousPanda/AIops/issues/84) | CHAT-4 — per-adapter `DeliveryResult` from ChatOpsClient | 2 h |
| [#80](https://github.com/UbiquotousPanda/AIops/issues/80) | RA-005 v2 — env-var routing policy | 1 d |
| [#68](https://github.com/UbiquotousPanda/AIops/issues/68) | DEMO-16 — server.py decompose (residual) | 1 d |
| [#87](https://github.com/UbiquotousPanda/AIops/issues/87) | CHAT-7 — Teams adapter (only if audience needs Teams) | 4 h |

### Out of demo MVP — post-POC

| # | Item | Why deferred |
|---|---|---|
| [#73](https://github.com/UbiquotousPanda/AIops/issues/73) | INFRA-1 — Loki + Tempo deployment | Demo doesn't need log/trace querying; Jaeger covers traces, agents work on metrics |
| [#36](https://github.com/UbiquotousPanda/AIops/issues/36) | RA-004 — Log Correlation | Blocked on #73 |
| [#74](https://github.com/UbiquotousPanda/AIops/issues/74) | INFRA-2 — Orchestrator stub | Internal seam; tape-and-glue works for the 6-minute demo |
| [#78](https://github.com/UbiquotousPanda/AIops/issues/78) | AUDIT-1 — Structured audit trail v1 | `chatops.jsonl` is good enough for the demo audit beat |
| [#76](https://github.com/UbiquotousPanda/AIops/issues/76) | PRS-007 — Knowledge Synthesizer v0 | Phase 4 work pulled forward; defer back |
| [#38](https://github.com/UbiquotousPanda/AIops/issues/38) | RA-008 — Incident Commander | Retro recommended deferring to Phase 3 |

---

## Daily cadence

**Standup: 09:30 local, 15 minutes.**

Format (one minute per person, no narrative):

1. What's merged since yesterday (PR # + one line)
2. What's in flight today + ETA
3. Blockers — concrete asks, not vague worries

PR descriptions carry the detail. Standup is for unblocking only.

**PR review SLA:** 4 working hours during W1–W2, 2 hours during W3. Tag the track owner; they own getting it through.

---

## If a track slips

Each track has a graceful degradation path so the demo still happens on June 14.

| Slipped track | Fallback for the demo |
|---|---|
| **A (RCA)** | Cut #86 (Slack user mapping) — audience won't notice `@chinmay` rendering as literal text. Push demo to Jun 18 (1-week buffer). |
| **B (HITL UI)** | Mock the Slack approval in the dry-run; show the policy gate blocking via the registry directly. Weaker visual, same architectural beat. |
| **C (PagerDuty)** | Narrate "this would page on-call" verbally; show the `actions: ["page_oncall"]` field in the routing decision. **Check with lead before agreeing — they specifically asked for the real phone-buzz.** |

---

## Pre-demo run-through (W3)

```powershell
# Clean slate
.\reset.ps1 -Hard

# Bring up cluster + UI
.\start.ps1

# Fire the demo scenario; expect all 4 sinks to react
Invoke-RestMethod -Method POST http://localhost:8765/api/scenarios/payment_failure/inject

# Within 60 seconds, verify:
#   - #aiops-poc-alerts channel has a red Sev-1 Block Kit message
#   - PagerDuty mobile app shows an unacked incident
#   - ServiceNow PDI has a new incident with full description body
#   - demo/audit/chatops.jsonl has new lines

# Trigger the RCA path
Invoke-RestMethod -Method POST http://localhost:8765/api/triage/live

# Expect:
#   - RCA Agent verdict appears on dashboard with fix steps
#   - Slack channel has an interactive "Approve / Deny" message
#   - Tap Approve in Slack
#   - flagd flag flips back to off; alert clears within ~30s
```

Detailed pre-demo checklist will land in `RUNNING.md` in W3.

---

**Hot links:** every `#NNN` above is clickable to the GitHub issue. Track owners — claim your track Monday morning by commenting on each of your issues with your start time. Standup link will be posted in the team Slack channel.
