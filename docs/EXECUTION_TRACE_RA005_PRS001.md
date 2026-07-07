# Execution Trace — Notification Router (RA-005) & Remediation Recommender (PRS-001)

> Step-by-step execution analysis in reviewer format. For **each step**: *what the agent did · why · inputs · outputs · tools/functions called · effect on the next decision · assumptions & issues.* Two concrete executions traced end to end (same incident), with decision points, tool interactions, and data transformations.
>
> Companion to [DEEP_DIVE_Notification_Router_and_Remediation_Recommender.md](DEEP_DIVE_Notification_Router_and_Remediation_Recommender.md) (which explains the *mechanics*); this doc traces a single *run* through them. See also [AGENT_EXECUTION_ANALYSIS.md](AGENT_EXECUTION_ANALYSIS.md) for the full reactive→prescriptive chain.

---

## The scenario being traced

A payment outage. The two agents run at different points of the same incident:
- **RA-005** runs inside the reactive flow the moment RA-001 produces a verdict — it *notifies*.
- **PRS-001** runs after the RCA Agent diagnoses the cause — it *recommends fixes*.

**RA-005 input** (`TriageVerdict` from RA-001):
```json
{ "affected_service":"payment", "severity":"Sev-1", "alert_summary":"Payment gateway 5xx spiking",
  "assigned_team":"Payments Team", "assigned_engineer":"chinmay@example.com",
  "recommended_runbook":"payment-runbook", "duplicate_alert_count":12, "status":"Active",
  "incident_id":"INC0012345" }
```
**Clock:** `02:00 UTC` (after hours).

---

# PART A — Notification Router (RA-005) execution

`route(verdict, now)` is the entry. It calls the pure `decide(...)`, then emits. Steps below are the actual call order in `agents/notification_router/agent.py`.

### A1 — Entry: `route(verdict)` invoked
- **What:** the orchestrator (`run_reactive_flow`) calls `route(verdict)` as the reactive chain's final step.
- **Why:** the verdict exists and is `Active`; someone must be told.
- **Inputs:** the `TriageVerdict` above.
- **Outputs:** (defers to `decide`) — eventually a `RoutingOutcome`.
- **Tools/functions:** `route()` → `decide()`.
- **Effect on next:** hands control to `decide` to compute the routing.
- **Assumptions/issues:** assumes RA-001 already persisted the verdict (it did) — this matters for A4's sticky lookup.

### A2 — Compute time context
- **What:** `now = datetime.now(UTC)`; `in_hours = _is_business_hours(now)`.
- **Why:** severity-2 routing and the response mode depend on business hours.
- **Inputs:** `now.hour = 2`.
- **Outputs:** `in_hours = False` (2 ∉ [9,18)).
- **Tools/functions:** `_is_business_hours`.
- **Effect on next:** off-hours will push borderline severities toward *page* (here Sev-1 pages regardless, but the flag still feeds `_response_mode`).
- **Assumptions/issues:** ⚠️ fixed **UTC** window — not the engineer's local time (v2 item).

### A3 — Tokenize the incident text (technique: regex tokenization)
- **What:** `_category_keywords_for(verdict)` extracts sub-domain keywords.
- **Why:** so the on-call DB can pick the *specialist* (payment-gateway vs payment-database), not just anyone on the team.
- **Inputs:** `service + alert_summary + recommended_runbook` = `"payment Payment gateway 5xx spiking payment-runbook"`.
- **Outputs:** `["payment","gateway","5xx","spiking","runbook"]` (regex `[a-z0-9]{3,}`, lowercased, de-duped).
- **Tools/functions:** `_category_keywords_for`, `_TOKEN_RE`.
- **Effect on next:** these tokens are passed to the on-call lookup (A4) for category matching.
- **Assumptions/issues:** pure lexical — `"5xx"` survives (3 chars); a synonym RA didn't use ("error" vs "5xx") would miss unless seeded. Tunable in `seed_oncall.py:CATEGORIES`, no model.

### A4 — Resolve on-call ◇ (decision point; tool call)
- **What:** `_resolve_oncall(verdict)` does **one** registry call to find the engineer.
- **Why:** the message must name a real person and DM them; resolved once so mentions + body + `category_display` stay consistent.
- **Inputs:** `team="Payments Team"`, `category_keywords=[…]`, `service="payment"`.
- **Outputs:** `{engineer_name:"Chinmay", engineer_email:"chinmay@example.com", slack_handle:"@chinmay", matched_category_display:"Payment Gateway", via_wildcard:false}` (or `None` on failure).
- **Tools/functions:** `get_registry().call("oncall.schedule.lookup", …)` → DB provider → `find_best_for_team_and_category` (expertise scoring) **or** sticky re-pick.
- **Decision branch:** sticky (RA-001 assigned < 2 h ago) → **re-return same engineer**; else expertise score picks highest. Provider that ignores `category_keywords` → registry drops the kwarg (signature filter), no error.
- **Effect on next:** drives mentions (A6), assignee (A8), and the `Sub-domain` line in the body (A9).
- **Assumptions/issues:** `None` is tolerated → falls back to verdict fields. Real names need `AIOPS_ONCALL_ROSTER_JSON`; else placeholders.

### A5 — Suppressed short-circuit ◇
- **What:** if `verdict.status == "Suppressed"`, return a `log`-mode decision with `actions=[]`.
- **Why:** a duplicate alert must not re-notify.
- **Inputs:** `status = "Active"` → **branch not taken**.
- **Outputs:** (skipped this run).
- **Tools/functions:** —.
- **Effect on next:** proceeds to severity branching.
- **Assumptions/issues:** empty `actions` is the signal `route()` uses (A11) to skip the emit entirely.

### A6 — Severity branch ◇ (the core routing decision)
- **What:** map severity (+ in_hours) → `chat_severity`, `channel`, `actions`, `reason`, `mentions`.
- **Why:** different severities warrant different loudness/destinations.
- **Inputs:** `sev = "Sev-1"`.
- **Outputs:** `chat_severity=P1`, `channel="incidents"`, `actions=["page_oncall","post_to_chat"]`, `mentions=["@chinmay"]` (from A4 via `_mentions_from`).
- **Tools/functions:** the `if/elif` ladder in `decide`; `_mentions_from`.
- **Decision branch:** Sev-1 → page+chat to `incidents`. (Sev-2 would split on `in_hours`; Sev-3/4 zero the mentions for anti-fatigue.)
- **Effect on next:** `actions` decides which downstream adapters fire (Slack-bot DM, PagerDuty); `mentions` decides the channel @-ping.
- **Assumptions/issues:** ⚠️ correctness hinges on RA-001's severity, which in the demo comes partly from a pre-set hint (documented shortcut).

### A7 — Response mode
- **What:** `mode = _response_mode(sev, in_hours)`.
- **Inputs:** `("Sev-1", False)`.
- **Outputs:** `"page"`.
- **Tools/functions:** `_response_mode`.
- **Effect on next:** drives the assignee-carry rule (A8), the body's `Response:` line, the dashboard badge, and the Slack-bot DM mode.
- **Assumptions/issues:** authoritative human-response signal (reflects hours, not just severity).

### A8 — Resolve assignee
- **What:** `_assignee_from(verdict, oncall)` → `(handle, name, email)`; `None`s when `mode=="log"`.
- **Why:** the bot DMs the owner even when the channel @-ping is suppressed.
- **Inputs:** `oncall` (A4), `mode="page"`.
- **Outputs:** `("@chinmay","Chinmay","chinmay@example.com")`.
- **Tools/functions:** `_assignee_from`.
- **Effect on next:** travels on the `ChatMessage` so `slack_bot`/PagerDuty can target the person.
- **Assumptions/issues:** prefers Slack handle (pings) over email (doesn't).

### A9 — Render the body
- **What:** `_render_body(verdict, reason, oncall, mode)` builds the structured `key: value` block.
- **Inputs:** all prior outputs.
- **Outputs:** multi-line text — `What failed / Application / Sub-domain: Payment Gateway / Severity / Response: PAGE / Owning team / On-call: Chinmay <…> — paged for Payment Gateway / Runbook / Duplicate alerts grouped: 12 / Routing reason`.
- **Tools/functions:** `_render_body`.
- **Effect on next:** becomes `ChatMessage.body`; rendered identically by every sink.
- **Assumptions/issues:** plain ASCII labels on purpose (cp1252/JSONL-safe).

### A10 — Assemble `RoutingDecision` → `ChatMessage`
- **What:** `decide` returns the `RoutingDecision`; `_decision_to_chat_message` maps it to the universal `ChatMessage`.
- **Inputs:** the decision fields.
- **Outputs:** a `ChatMessage{channel, severity=P1, title, body, incident_id, service, category_display, mentions, actions, response_mode, assignee*, timestamp}`.
- **Tools/functions:** `_decision_to_chat_message`.
- **Effect on next:** the single object fanned out in A11.
- **Assumptions/issues:** —.

### A11 — Fan-out emit ◇ (tool interactions)
- **What:** `route()` checks `decision.actions` (non-empty) → `get_client().send(msg)`.
- **Why:** deliver to every configured sink at once.
- **Inputs:** the `ChatMessage`.
- **Outputs:** `deliveries = {adapter: DeliveryResult{ok,error,latency_ms}}`.
- **Tools/functions:** `ChatOpsClient.send` → each adapter:
  | Adapter | Fired? (this run) | Why |
  |---|---|---|
  | jsonfile | ✅ | every message |
  | websocket | ✅ | every message → dashboard |
  | slack (webhook) | ✅ | posts to `#incidents`, P1 red, `@chinmay`→`<@U…>` |
  | slack_bot | ✅ | `response_mode=page` → DM Chinmay |
  | pagerduty | ✅ | `page_oncall` + P1 ≥ P2 → page (dedup_key=INC0012345) |
- **Effect on next:** `deliveries` returns to the orchestrator → feeds the deliverability KPI.
- **Assumptions/issues:** per-adapter exceptions caught — one broken sink can't block the others.

### A12 — Persist
- **What:** orchestrator calls `save_notification(routing, verdict_id)`.
- **Outputs:** a `NotificationRow{channel, chat_severity, response_mode="page", actions, audit_trace}`.
- **Tools/functions:** `aiops.state.repository.save_notification`.
- **Effect on next:** backfills the dashboard Notifications page (`/api/notifications`) across restarts.
- **Assumptions/issues:** FK-guarded (only if `verdict_id` exists); best-effort.

**RA-005 result:** Chinmay paged via channel + Slack DM + PagerDuty; `response_mode=page`; fully audited.

---

# PART B — Remediation Recommender (PRS-001) execution

Now RCA has run. `recommend(input)` is a **pure function** (no I/O). Trace below is the call order in `agents/remediation_recommender/agent.py`.

**PRS-001 input** (`RemediationInput`):
```json
{ "rca_verdict": { "affected_service":"payment", "confidence_score":0.9,
    "root_cause":"payment gateway external dependency returning 5xx",
    "ranked_fix_steps":[{"description":"Disable paymentFailure flag","blast_radius":"low",
      "action_type":"set_flag","flag":"paymentFailure","variant":"off","requires_hitl":true}] },
  "triage_verdict": {...}, "environment":"production", "operator_preferences":{} }
```

### B1 — Entry + extract
- **What:** `/api/remediation` → `RemediationInput.model_validate` → `recommend(input)`; pull `service, root_cause, rca_confidence=0.9, fix_steps`.
- **Why:** these drive option construction + scoring.
- **Inputs:** the JSON above.
- **Outputs:** locals; `trace[]` seeded.
- **Tools/functions:** `recommend`.
- **Effect on next:** feeds B2/B3.
- **Assumptions/issues:** reads upstream verdict as a **dict** (loose coupling — PRS-001 ships independently of RCA's Python types).

### B2 — Options from RCA steps (with confidence decay)
- **What:** loop `fix_steps` → `_option_from_rca_step(...)`.
- **Why:** RCA's diagnosed fixes are the primary options.
- **Inputs:** the one set_flag step, `rank_index=0`, `rca_confidence=0.9`.
- **Outputs:** option `rca-step-1`: `action=set_flag`, `blast=low (score 1)`, `confidence=0.9−0=0.9`, `rollback_tested=True`, `MTTR=3`, `tool=feature_flags.set_variant{flag:paymentFailure,variant:off}`, `source=rca_fix_step`.
- **Tools/functions:** `_option_from_rca_step`, `blast_radius_score`.
- **Decision logic:** `confidence = clamp(rca_confidence − 0.05*rank_index)`; `rollback_tested = (action==set_flag)`; tool inferred from action.
- **Effect on next:** enters the option pool for scoring (B5).
- **Assumptions/issues:** shallow decay keeps a safe fallback from being buried.

### B3 — Options from the catalog ◇ (technique: substring AND-containment)
- **What:** `patterns_for_cause(root_cause)` → catalog options.
- **Why:** add symptom-driven mitigations RCA didn't propose.
- **Inputs:** `root_cause` lowercased.
- **Outputs:** matches `("external","dependency")` (both substrings present) → `external-fail-open-flag`: `set_flag, blast low, conf 0.65, MTTR 2, tool feature_flags.set_variant{flag:paymentGatewayProvider, variant:secondary}`. (Other patterns' keywords absent → no match.)
- **Tools/functions:** `patterns_for_cause`, `_option_from_catalog`.
- **Decision branch:** a pattern fires only if **every** keyword in its tuple is in the cause text (AND).
- **Effect on next:** second option in the pool; de-duped by `option_id`.
- **Assumptions/issues:** substring match → a cause phrased without "external"/"dependency" would skip this mitigation.

### B4 — Defensive placeholder ◇
- **What:** if the pool is empty, append `manual-investigate`.
- **Inputs:** pool size = 2 → **branch not taken**.
- **Effect on next:** guarantees `options ≥ 1` (model invariant).
- **Assumptions/issues:** a non-empty pool means this never fires here.

### B5 — Composite scoring (technique: weighted linear score) — worked math
- **What:** `_composite_score` each option; `prefer_safe=True` (default), `environment="production"`.
- **Formula:** `(6−blast_score)*10 + confidence*5 + (3 if rollback_tested else 0) + env_bonus` (env_bonus = +5 for production+prefer_safe+low).
- **Outputs:**
  | option | (6−blast)×10 | conf×5 | rollback | env | **total** |
  |---|---|---|---|---|---|
  | rca-step-1 (low,0.9,tested) | 50 | 4.5 | 3 | +5 | **62.5** |
  | external-fail-open (low,0.65,tested) | 50 | 3.25 | 3 | +5 | **61.25** |
- **Tools/functions:** `_composite_score`.
- **Effect on next:** the sort key (B6).
- **Assumptions/issues:** safety term dominates by design (a blast-radius drop = +20 > full confidence +5).

### B6 — Sort + recommend ◇
- **What:** `options.sort(key=(-score, blast_radius_score, -confidence, option_id))`; `recommended_option_id = options[0]`.
- **Outputs:** order → **#1 rca-step-1 (62.5)**, #2 external-fail-open (61.25); `recommended_option_id="rca-step-1"`; `auto_pick_eligible=False`.
- **Tools/functions:** `sort`.
- **Effect on next:** the operator's default choice; nothing auto-executes.
- **Assumptions/issues:** deterministic tie-break (id) → reproducible ordering.

### B7 — Verdict-level confidence + assemble
- **What:** `confidence_score = mean(top-3 confidences)`; build `incident_summary`, `rationale`; return `RemediationVerdict`.
- **Inputs:** confidences `[0.9, 0.65]`.
- **Outputs:** `confidence_score = 0.775`; `RemediationVerdict{options:[…], recommended_option_id:"rca-step-1", requires_hitl:True, …}`.
- **Tools/functions:** `recommend` tail.
- **Effect on next:** returned to `/api/remediation` → dashboard renders the ranked menu.
- **Assumptions/issues:** `requires_hitl=True` is a `Literal` — un-overridable.

### B8 — Hand-off to Auto-Healer ◇ (where execution actually happens)
- **What:** operator clicks "Send to Auto-Healer" → `api.executeOption(option, …)`.
- **Why:** PRS-001 only recommends; PRS-002 executes through the HITL gate.
- **Inputs:** the chosen option (`tool_capability=feature_flags.set_variant`, `tool_args={flag:paymentFailure,variant:off}`).
- **Outputs:** an `ExecutionVerdict` (after approval).
- **Tools/functions:** Auto-Healer `execute` → `get_gate().enforce("auto_heal.lite.execute")` → (live) `feature_flags.set_variant`.
- **Effect on next:** on approve+live → flag flips → payment heals → Resolution Verifier runs.
- **Assumptions/issues:** PRS-001 itself made **no external call** — its `requires_hitl` only bites here.

---

## Consolidated views

### Decision points (both agents)
| Agent | # | Decision | This run | Alternatives |
|---|---|---|---|---|
| RA-005 | A4 | on-call resolution | sticky → Chinmay | expertise score / wildcard / None |
| RA-005 | A5 | suppressed? | no (Active) | yes → skip emit |
| RA-005 | A6 | severity branch | Sev-1 → page+chat | Sev-2 hrs split / Sev-3/4 quiet |
| RA-005 | A7 | response mode | page | notify / log |
| RA-005 | A11 | which adapters fire | all 5 | bot/PD skip when not paging |
| PRS-001 | B3 | catalog match | external-dependency hit | no match → RCA-only |
| PRS-001 | B4 | empty pool? | no | yes → placeholder |
| PRS-001 | B6 | ranking | rca-step-1 #1 | safer/ higher-conf option could overtake |

### Tool / API interactions
| Step | Capability / function | Provider | Real/Pure | Effect |
|---|---|---|---|---|
| A4 | `oncall.schedule.lookup` | db | Real (DB) | resolve engineer + sub-domain |
| A11 | chatops fan-out | slack/pagerduty/ws/jsonfile | Real | page/notify + audit |
| A12 | `save_notification` | state repo | Real (SQLite) | persist NotificationRow |
| B1–B7 | `recommend` | — | **Pure** (no I/O) | rank options |
| B8 | `auto_heal.lite.execute` → `feature_flags.set_variant` | gate → flagd | Gated Real | heal (after approval) |

### Data transformations
| Stage | In | Out |
|---|---|---|
| RA-005 A1–A10 | `TriageVerdict` | `RoutingDecision` → `ChatMessage` |
| RA-005 A11 | `ChatMessage` | `{adapter: DeliveryResult}` |
| RA-005 A12 | `RoutingDecision` | `NotificationRow` |
| PRS-001 B1–B7 | `RCAVerdict` (dict) | `RemediationVerdict` (ranked options) |
| PRS-001 B8 | one `RemediationOption` | `ExecutionVerdict` (via Auto-Healer) |

## Assumptions & potential issues (reviewer summary)
| Area | Assumption / risk | Severity |
|---|---|---|
| RA-005 severity input | inherits RA-001's severity (demo: partly pre-set) | Medium |
| RA-005 business hours | fixed UTC window, not per-engineer TZ | Low |
| RA-005 channel names | derived (`team-payments`) — map to real channels in prod | Low |
| RA-005 on-call identity | real emails need `AIOPS_ONCALL_ROSTER_JSON` | Low (mitigated) |
| PRS-001 ranking | deterministic; no learning-from-history / cost yet | Medium |
| PRS-001 executable breadth | only `set_flag` truly runnable; scale/restart/rollback advisory | Medium |
| Both | HITL enforced at the platform boundary — un-bypassable | Strength |

**Verdict:** RA-005 is deterministic routing with graceful per-sink isolation; PRS-001 is a pure, explainable ranking function. Neither uses an LLM or similarity model — every decision is reproducible from inputs, and the only state-changing action (the flag flip) sits behind the HITL gate in PRS-002.

---

*Companion to [DEEP_DIVE_Notification_Router_and_Remediation_Recommender.md](DEEP_DIVE_Notification_Router_and_Remediation_Recommender.md), [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md), [CODEBASE_INTERNALS.md](CODEBASE_INTERNALS.md).*
