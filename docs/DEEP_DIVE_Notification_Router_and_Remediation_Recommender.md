# Deep Dive — Notification Router (RA-005) & Remediation Recommender (PRS-001)

> Written so a complete beginner can follow it, **and** so a senior engineer gets the real mechanics — every technique named, explained at the algorithm level, and shown with worked numbers. Nothing summarized away.
>
> **A note on "similarity"**: you asked specifically about techniques like similarity. Important up front — **neither of these two agents uses AI embeddings / cosine similarity.** Those live in RA-001 (alert dedup) and RA-002 (incident classification). RA-005 and PRS-001 are **deterministic, rule/score based** — and below I explain *exactly* which technique each step uses (keyword set-intersection, weighted scoring, substring matching, business-hours gating, Slack Block Kit, PagerDuty Events API dedup) and *how that technique works* with examples. Where a step *could* use similarity but deliberately doesn't, I say why.

---

# PART 1 — Notification Router (RA-005)

## 1.1 What it is (non-technical)

When an incident is detected, **someone** needs to be told — but *who*, *where*, and *how loudly*? Waking an engineer at 3 a.m. for a tiny issue burns trust; missing a real Sev-1 is a disaster. The Notification Router is the **smart dispatcher** that answers three questions automatically:

1. **How loud?** → *page* (wake them now), *notify* (heads-up, look when free), or *log* (just record it).
2. **Where?** → which chat channel (the owning team's channel, the incidents channel, or a quiet noise bucket).
3. **Who?** → the on-call engineer, by name, and even the *sub-specialist* (e.g. the payment-*gateway* expert, not just "someone on the Payments team").

Then it writes a clean, structured message and sends it to every configured place at once (dashboard, Slack channel, a direct message, PagerDuty).

## 1.2 Where it sits (technical) + its contract

- **Input:** a `TriageVerdict` (RA-001's output) — `{affected_service, severity, alert_summary, assigned_team, assigned_engineer, recommended_runbook, duplicate_alert_count, status, …}`.
- **Output:** a `RoutingDecision` (pure, no side-effects) and, via `route()`, a `RoutingOutcome` (decision + per-adapter `DeliveryResult`s).
- **HITL level:** **None** — sending a message changes nothing in your systems, so there's no approval gate.
- **Public functions** (`agents/notification_router/agent.py`):
  - `decide(verdict, now=None) -> RoutingDecision` — *pure*. Decides everything, emits nothing. (Used by tests + eval harness.)
  - `route(verdict, now=None) -> RoutingOutcome` — calls `decide`, then actually emits through the chatops seam.
  - `run(input) -> dict` — eval-harness entry (calls `decide`, never emits).

## 1.3 The complete code flow (function by function)

Here is **everything `decide()` does, in order**, with the exact helper it calls:

```
decide(verdict, now)
 ├─ now = now or datetime.now(UTC)
 ├─ in_hours = _is_business_hours(now)          # UTC hour in [9, 18)
 ├─ sev = verdict.severity
 ├─ team_slug = _team_slug(verdict.assigned_team)   # "Payments Team" → "payments"
 ├─ oncall = _resolve_oncall(verdict)            # ← the on-call lookup (see 1.4 + 1.5)
 │    └─ keywords = _category_keywords_for(verdict)  # tokenize text → sub-domain keywords
 │    └─ get_registry().call("oncall.schedule.lookup",
 │             team=…, category_keywords=keywords, service=…)
 ├─ if verdict.status == "Suppressed":           # duplicate alert → no-op
 │    └─ return RoutingDecision(channel="suppressed", actions=[], response_mode="log", …)
 ├─ branch on severity (see 1.6 truth table):
 │    Sev-1 → P1, channel "incidents",  actions [page_oncall, post_to_chat]
 │    Sev-2 + in-hours  → P2, "team-<slug>", [post_to_chat]
 │    Sev-2 + off-hours → P2, "incidents",   [page_oncall, post_to_chat]
 │    Sev-3 → P3, "ops-daytime", [post_to_chat], mentions=[]   # anti-fatigue
 │    Sev-4 → INFO, "alerts-noise", [post_to_chat], mentions=[]
 ├─ mode = _response_mode(sev, in_hours)         # page | notify | log
 ├─ assignee = _assignee_from(verdict, oncall)   # (handle, name, email) — None in log mode
 ├─ body = _render_body(verdict, reason, oncall, mode)   # the structured key:value block
 └─ return RoutingDecision(chat_severity, channel, title, body, mentions, actions,
                           reason, audit_trace, response_mode=mode, assignee*, category_display)
```

Then `route()`:
```
route(verdict, now)
 ├─ decision = decide(verdict, now)
 ├─ if not decision.actions:  return RoutingOutcome(decision, deliveries={})   # Suppressed short-circuit
 ├─ msg = _decision_to_chat_message(verdict, decision)   # build the universal ChatMessage
 ├─ deliveries = get_client().send(msg)                  # ← fan-out to every adapter (see 1.7)
 └─ return RoutingOutcome(decision, deliveries)
```

**Where the request comes from (the call path):**
- In the live chain, **`aiops/runtime/orchestrator.py::run_reactive_flow(alert)`** calls `route(verdict)` as its last step, then `state_repo.save_notification(routing, verdict_id)` persists a `NotificationRow`.
- That orchestrator is invoked by **`POST /api/triage`** and by **RA-008 Incident Commander**.
- The dashboard's **Notifications page** shows them: it backfills from **`GET /api/notifications`** (the persisted rows) and live-updates over the **`/ws/chatops`** WebSocket.

## 1.4 Deep technique #1 — keyword tokenization + **set-intersection matching** (NOT embeddings)

**The job:** from the incident text, pull "sub-domain keywords" so the on-call DB can pick the *right specialist* (payment-gateway vs payment-database). 

**The technique: lexical tokenization + set intersection.** No NLP, no model — pure regex + Python set math. Here's *exactly* how it works:

**Step A — tokenize** (`_category_keywords_for`):
```python
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")   # alphanumeric runs of length ≥ 3
text = (service + " " + alert_summary + " " + recommended_runbook).lower()
tokens = de-duplicate(_TOKEN_RE.findall(text))
```
- Regex `[a-z0-9]{3,}` = "grab every lowercase-alphanumeric run of 3+ characters." So `"5xx"` is **kept** (3 chars) but `"a"`, `"to"` are dropped. Punctuation/spaces are separators.
- **Worked example.** Input `affected_service="payment"`, `alert_summary="Payment gateway 5xx spiking"`, `recommended_runbook="payment-runbook"`:
  - lowercased text: `"payment payment gateway 5xx spiking payment-runbook"`
  - regex matches: `payment, payment, gateway, 5xx, spiking, payment, runbook`
  - de-duped → **`["payment", "gateway", "5xx", "spiking", "runbook"]`**

**Step B — set intersection** (in the on-call DB, `oncall_repository._match_categories_for_team`):
Each failure category (seeded in `scripts/seed_oncall.py::CATEGORIES`) has a keyword set. Matching = **count the overlap** between the alert's token set and each category's keyword set: `overlap = |alert_tokens ∩ category_keywords|`.

> **How "set intersection" works (for the beginner):** a *set* is a bag of unique items. The *intersection* `A ∩ B` is the items in **both**. Its size (cardinality `|…|`) is just *how many words they share*. Bigger overlap = stronger match. That's the entire "matching algorithm" — no AI.

- **Worked example** — alert tokens `{payment, gateway, 5xx, spiking, runbook}` vs the Payments Team's three categories:
  | Category | Its keywords (seed) | ∩ with alert | overlap |
  |---|---|---|---|
  | **payment-gateway** | payment, gateway, api, 5xx, charge, authorize, stripe, … | {payment, gateway, 5xx} | **3** |
  | payment-database | payment, database, db, sql, connection, pool, … | {payment} | 1 |
  | payment-kafka | payment, kafka, queue, event, lag, … | {payment} | 1 |
  - Sorted by overlap → **payment-gateway wins (3)**. This is the "sub-domain" the alert is about.

**Why not embeddings here?** Because routing must be **explainable and operator-tunable**: a missed match is a one-line keyword addition in `seed_oncall.py`, not a model retrain. (Contrast: RA-002 *does* use embeddings because "find a similar past incident" is a fuzzy semantic task.)

## 1.5 Deep technique #2 — on-call **expertise scoring** (weighted linear score)

Once the category is known, the DB picks *which engineer* with a **weighted linear score** (`oncall_repository._score_expertise`). This is the deepest piece. The formula:

```
score(engineer, category) =
      proficiency_weight                       # novice 10 · intermediate 50 · expert 100 · principal 150
    + min(incidents_resolved, 25) * 2          # track record, capped so a veteran can't dominate
    + clamp(feedback_score, 0..5) * 20         # quality of past resolutions (1.0–5.0)
    + max(manual_priority, 0) * 50             # operator override nudge (rare)
then weighted by how strongly the alert matched the engineer's category:
    weighted = score * overlap_count
```
Pick the **highest weighted score** among engineers **on shift** for that team (role ladder primary → secondary → manager). Ties break to the **less-loaded** engineer (fewest assignments in the last 24 h), then lowest id.

- **Worked example** — Payments Team, alert matched `payment-gateway` (overlap 3). Two engineers on shift:
  - **Chinmay** — `payment-gateway`, proficiency *expert*, 15 incidents, feedback 4.5:
    `100 + min(15,25)*2 (=30) + 4.5*20 (=90) + 0 = 220`. Weighted: `220 × 3 = **660**`.
  - **Riya** — `payment-gateway` *intermediate* (4 incidents, fb 3.8) **and** `payment-database` *expert* (12, 4.6):
    - gateway: `50 + 8 + 76 = 134` → `× 3 = 402`
    - database: `100 + 24 + 92 = 216` → `× 1 (db overlap) = 216`
    - Riya's best = **402**.
  - **Chinmay 660 > Riya 402 → page Chinmay.** Explainable in one log line: *"matched payment-gateway(x3) → Chinmay weighted_score=660"*.

- **The sticky twist (important in the live chain):** RA-005 passes `service=` to the lookup. Because **RA-001 already ran and saved a verdict** naming an engineer < 2 h ago, the DB's *sticky* rule (`find_last_assigned_engineer`, 2 h window) **re-returns that same engineer** instead of re-scoring — so the incident doesn't bounce between people mid-flow. The expertise math above actually fires at **RA-001** time; RA-005 inherits it and just re-attaches the matched sub-domain for the message.

## 1.6 Deep — the severity → response truth table + business hours

**Business hours** (`_is_business_hours`): `BUSINESS_HOUR_START(9) ≤ now.hour < BUSINESS_HOUR_END(18)`, evaluated in **UTC**. So `14:00Z` = in-hours, `02:00Z` = off-hours. (Per-engineer timezone is a v2 item.)

**The full decision table** (`decide` branches + `_response_mode`):

| Severity | In hours? | chat_severity | channel | actions | response_mode | channel @-mention? |
|---|---|---|---|---|---|---|
| Sev-1 | any | P1 | `incidents` | page_oncall + post_to_chat | **page** | yes |
| Sev-2 | yes | P2 | `team-<slug>` | post_to_chat | **notify** | yes |
| Sev-2 | no | P2 | `incidents` | page_oncall + post_to_chat | **page** | yes |
| Sev-3 | any | P3 | `ops-daytime` | post_to_chat | **notify** | **no** (anti-fatigue) |
| Sev-4 | any | INFO | `alerts-noise` | post_to_chat | **log** | **no** |
| *Suppressed* | — | INFO | `suppressed` | *(none)* | **log** | no → **emit skipped** |

`_response_mode` is the small pure function encoding the page/notify/log column. The `response_mode` is the **authoritative** field the dashboard badge and the Slack bot DM logic read (it reflects both severity *and* business hours, unlike severity alone).

## 1.7 Deep — the chatops fan-out (where the message physically goes)

`route()` builds **one** `ChatMessage` and calls `get_client().send(msg)`. The `ChatOpsClient` is a **fan-out**: it loops over every registered adapter, calls `adapter.send(msg)`, catches per-adapter exceptions (one broken sink never blocks the others), and returns `{adapter_name: DeliveryResult{ok, error, latency_ms}}`.

The `ChatMessage` carries everything an adapter might need: `channel, severity, title, body, incident_id, service, category_display, mentions[], actions[], response_mode, assignee/_name/_email, timestamp`. `to_record()` serializes it to the canonical wire shape (used by the audit log + WS + dashboard).

**Each adapter — what it does, when it fires, what backend:**

| Adapter | Fires when | Backend / technique |
|---|---|---|
| **jsonfile** | every message | appends one JSON line to `demo/audit/chatops.jsonl` (the audit trail) |
| **websocket** | every message | pushes the record to `/ws/chatops` → the dashboard Notifications live feed |
| **slack** (webhook) | every message | HTTP POST to `AIOPS_SLACK_WEBHOOK_URL`. Renders **Slack Block Kit** with a severity color (P0/P1=red, P2=orange, P3=yellow, INFO=slate). **Rewrites mentions**: `@chinmay` → `<@U…>` using `slack_users.json` (see below). Adds approve/deny buttons if `msg.interactive` is set. |
| **slack_bot** (DM) | `page_oncall` in actions **or** `response_mode=page` (DM everyone) · `response_mode=notify` (DM the assignee only) | HTTP POST to Slack `chat.postMessage` with the user's ID as the channel (opens a DM). `AIOPS_SLACK_BOT_TOKEN`. |
| **pagerduty** | `page_oncall` in actions **AND** severity ≥ P2 | HTTP POST to `https://events.pagerduty.com/v2/enqueue` (Events API v2), on a **non-blocking daemon thread**, with **dedup** + 1 retry. |

**Deep — Slack mention rewriting (technique: dictionary lookup + token rewrite).** Slack only pings a user if the text contains `<@U_MEMBER_ID>`, not `@name`. So the adapter loads a map `{"@chinmay": "U0123"}` (committed placeholder `slack_users.json` merged with the encrypted `AIOPS_SLACK_USER_MAP_JSON`), and string-replaces each handle. Unmapped handles fall back to plain text (the message still lands; it just doesn't ping). That's why RA-005 prefers the on-call DB's `slack_handle` over the raw email.

**Deep — PagerDuty dedup (technique: idempotency key).** PagerDuty would create a *new* incident for every event unless you give it a stable `dedup_key`. The adapter uses `incident_id` when present, else `sha256(service|title)`. So a re-fired alert **updates** the existing PagerDuty incident instead of spamming duplicates. Severity maps P0/P1→`critical`, P2→`error`. The defence-in-depth check refuses to page below P2 even if `page_oncall` slipped in.

## 1.8 Two big examples (full traces)

### Example 1 — Payment Sev-1 at **02:00 UTC** (after hours)
**Input verdict:** `service=payment, severity=Sev-1, team="Payments Team", engineer=chinmay@…, summary="Payment gateway 5xx spiking", runbook="payment-runbook"`.
1. `now.hour=2` → `in_hours=False`.
2. `_category_keywords_for` → `["payment","gateway","5xx","spiking","runbook"]`.
3. `_resolve_oncall` → `oncall.schedule.lookup(team="Payments Team", category_keywords=[…], service="payment")` → DB sticky returns **Chinmay** (assigned by RA-001), `matched_category_display="Payment Gateway"`, `via_wildcard=False`.
4. Severity branch: **Sev-1** → `chat_severity=P1`, `channel="incidents"`, `actions=["page_oncall","post_to_chat"]`.
5. `_response_mode("Sev-1", False)` → **`page`**.
6. `body` rendered:
   ```
   What failed: Payment gateway 5xx spiking
   Application: payment
   Sub-domain: Payment Gateway
   Severity: Sev-1
   Response: PAGE - on-call paged now
   Owning team: Payments Team
   On-call: Chinmay <chinmay@…> — paged for Payment Gateway
   Runbook: payment-runbook
   Routing reason: Sev-1 critical — page on-call regardless of hour
   ```
7. `route()` → one `ChatMessage` → `send()` fans out:
   - **jsonfile** → audit line written.
   - **websocket** → Notifications page shows it with a **PAGE** badge.
   - **slack** → posts to `#incidents`, **red** P1 attachment, `@chinmay` rewritten to `<@U…>` (real ping).
   - **slack_bot** → `page_oncall` present → **DMs Chinmay** directly.
   - **pagerduty** → `page_oncall` + P1 ≥ P2 → **pages PagerDuty**, dedup_key = incident id, severity `critical`.
8. Orchestrator persists `NotificationRow(response_mode="page", channel="incidents", …)`.
**Net:** Chinmay is woken via channel ping + Slack DM + PagerDuty page; everything audited.

### Example 2 — Product-catalog Sev-3 at **14:00 UTC** (business hours)
**Input verdict:** `service=product-catalog, severity=Sev-3, team="Catalog Team", summary="Product catalog p95 latency elevated"`.
1. `now.hour=14` → `in_hours=True`.
2. tokens → `["product","catalog","p95","latency","elevated"]` → matches category **catalog-service**.
3. on-call resolved → **Riya** (Catalog day owner), `matched_category_display="Product Catalog"`.
4. Severity branch: **Sev-3** → `chat_severity=P3`, `channel="ops-daytime"`, `actions=["post_to_chat"]`, **`mentions=[]`** (anti-fatigue — no channel ping).
5. `_response_mode("Sev-3", True)` → **`notify`**.
6. `assignee` is still carried (mode≠log) so the bot can DM Riya a personal heads-up.
7. `route()` → fan-out:
   - **jsonfile / websocket** → recorded + Notifications page shows a **NOTIFY** badge.
   - **slack** → posts to `#ops-daytime`, yellow P3, **no @-ping**.
   - **slack_bot** → `response_mode=notify` → **DMs only the assignee (Riya)** quietly.
   - **pagerduty** → `page_oncall` NOT in actions → **skipped** (nobody paged).
**Net:** quiet, daytime, no 3 a.m. wake-up — Riya gets a personal DM and it's logged for morning triage.

---

# PART 2 — Remediation Recommender (PRS-001)

## 2.1 What it is (non-technical)

The RCA Agent says *"here's the cause and some fixes."* The Remediation Recommender turns that into a **ranked menu of options** a human can choose from — each option labelled with: how risky it is (*blast radius*), how confident, how long it'll take (*MTTR*), how to undo it (*rollback*), and **which tool would run it**. It also adds **extra "stop-the-bleeding" mitigations** the RCA might not have suggested (circuit breakers, fail-overs). It **recommends only — it never executes** (Auto-Healer does that, after human approval).

## 2.2 Where it sits + contract

- **Input:** `RemediationInput{rca_verdict (dict), triage_verdict? (dict), environment ("production"|"staging"|"dev"), operator_preferences}`.
- **Output:** `RemediationVerdict{affected_service, incident_summary, options[] (sorted), recommended_option_id, auto_pick_eligible=False, confidence_score, requires_hitl=True, rationale, audit_metadata}`.
- **Each option** (`RemediationOption`): `option_id, title, description, action_type, blast_radius, blast_radius_score (1–5), rollback, rollback_tested, confidence, estimated_mttr_minutes, requires_hitl=True, rationale, tool_capability, tool_args, source`.
- **HITL level:** **Required** — but note: *recommending* is harmless; the gate actually bites when **Auto-Healer** runs the chosen option.
- **It is a pure function:** no LLM, no DB, no I/O. Same input → same output. (`recommend(input) -> RemediationVerdict`, plus `run(dict)->dict` for evals.)

## 2.3 The complete code flow

```
recommend(input)
 ├─ rca = input.rca_verdict;  service = rca["affected_service"];  root_cause = rca["root_cause"]
 ├─ rca_confidence = rca["confidence_score"];  fix_steps = rca["ranked_fix_steps"]
 ├─ 1. RCA → options (1:1):  for each fix_step → _option_from_rca_step(...)   source = rca_fix_step
 ├─ 2. Catalog → options:    patterns_for_cause(root_cause) → _option_from_catalog(...)  source = playbook_pattern
 ├─ 3. (defensive) if no options at all → one "Investigate manually" placeholder
 ├─ 4. rank:  options.sort(key = (-composite_score, blast_radius_score, -confidence, option_id))
 ├─ recommended_option_id = options[0].option_id;  auto_pick_eligible = False
 ├─ confidence_score = mean(confidence of top-3 options)
 └─ return RemediationVerdict(...)
```

**Where the request comes from:**
- **`POST /api/remediation`** (`demo/ui/server.py`) → `RemediationInput.model_validate(...)` → `remediate(typed)` → `.model_dump()`.
- **`POST /api/triage-full`** runs RCA then remediation in one call.
- The dashboard **Remediation Recommender page** (`/agents/remediation-recommender`): pick incident → `api.rca(verdict)` → `api.remediation(rca, verdict)` → render ranked options → **"Send to Auto-Healer"** passes the chosen option to the Auto-Healer page → `api.executeOption(...)`.

## 2.4 Deep — building the options (two sources)

**(a) From each RCA fix step** (`_option_from_rca_step`):
- `blast_radius` copied from the step (default medium); `blast_radius_score` via `blast_radius_score()` (low=1, medium=3, high=5).
- `action_type` mapped (`set_flag`/`rollback_deploy`/`manual`); `tool_capability` inferred (`set_flag`→`feature_flags.set_variant`, `rollback_deploy`→`k8s.deployment.rollback`, …; manual→None).
- `tool_args` built for `set_flag` (`{flag, variant}`).
- `confidence = clamp(rca_confidence − 0.05 × rank_index)` — a **shallow decay** so RCA's #2/#3 steps rank slightly below #1 but a safer fallback isn't buried.
- `rollback_tested = (action_type == set_flag)` — flag flips are atomic & instantly reversible, so they're treated as proven.
- `estimated_mttr_minutes` from a blast-radius prior: low=3, medium=10, high=30.

**(b) From the catalog** (`patterns_for_cause` + `_option_from_catalog`): symptom-driven mitigations RCA may not propose. See 2.6.

## 2.5 Deep technique — the **composite ranking score** (weighted linear scoring)

This is the heart of PRS-001. The technique is a **transparent weighted linear score** (no ML), computed by `_composite_score`:

```
score(option) =
      (6 − blast_radius_score) * 10     # SAFETY dominates: low(1)→50, medium(3)→30, high(5)→10
    + confidence * 5                    # higher confidence helps, but can't outweigh safety
    + (3 if rollback_tested else 0)     # proven-reversible bonus
    + env_bonus                         # production+prefer_safe+low → +5 ; staging/dev+medium → +2 ; else 0
```
Then `options.sort` by the tuple `(-score, blast_radius_score, -confidence, option_id)` — i.e. **highest score first**, ties broken by safer blast radius, then higher confidence, then a stable id (so the order is 100% deterministic).

> **Why "(6 − blast_score) × 10"?** It makes safety the dominant term *on purpose*: even a max-confidence option (`+5`) can't out-rank a one-step-safer option (a blast-radius drop is worth `+20`). Day-1 philosophy: *first, do no harm* — nothing auto-executes anyway, so bias toward the reversible move.

## 2.6 Deep — the catalog & **substring set-containment matching**

The catalog (`remediation_catalog.py`) is **pure data**: a list of `(keyword_tuple, [CatalogOption…])`. Matching (`patterns_for_cause`) uses **substring containment with AND semantics**:

```python
cause_lower = root_cause.lower()
for keywords, options in _PATTERNS:
    if all(kw in cause_lower for kw in keywords):   # EVERY keyword must appear
        matched.extend(options)
```
> **How it works:** unlike RA-005's "count the overlap," this is **all-or-nothing**: a pattern fires only if *every* keyword in its tuple is a substring of the cause text. So `("kafka","lag")` matches *"kafka consumer lag climbing"* (both present) but not *"kafka broker down"* (no "lag").

**The full Day-1 catalog:**
| Trigger keywords (all required) | Option(s) it adds | action_type · blast · conf · MTTR · tool |
|---|---|---|
| `kafka` + `lag` | Restart consumer group | RESTART · medium · 0.7 · 8m · `k8s.deployment.restart` |
| | Open producer circuit breaker | CIRCUIT_BREAKER · **low** · 0.5 · 2m · `feature_flags.set_variant` |
| `connection` + `pool` | Increase pool size | SET_FLAG · low · 0.55 · 3m · `feature_flags.set_variant` |
| `memory` + `oom` | Roll back recent deploy | ROLLBACK_DEPLOY · medium · 0.6 · 12m · *(manual, no executor)* |
| `external` + `dependency` | Fail open to secondary provider | SET_FLAG · low · 0.65 · 2m · `feature_flags.set_variant` |
| `cart` + `redis` | Flush stale sessions | MANUAL · low · 0.5 · 4m · *(manual)* |

Templated `tool_args` like `{"flag": "{service}GatewayProvider"}` get `{service}` filled from the RCA verdict at runtime.

## 2.7 Two big examples (with the full ranking math)

### Example 1 — Payment external-dependency (RCA option wins)
**RCA verdict:** `service="payment", root_cause="payment gateway external dependency returning 5xx", confidence=0.9, ranked_fix_steps=[{description:"Disable paymentFailure flag", blast_radius:"low", action_type:"set_flag", flag:"paymentFailure", variant:"off", requires_hitl:true}]`.

1. **RCA → option** `rca-step-1`: set_flag, blast **low** (score 1), confidence `0.9 − 0 = 0.9`, rollback_tested **True**, MTTR 3, tool `feature_flags.set_variant{flag:paymentFailure, variant:off}`.
2. **Catalog:** cause contains both `external` + `dependency` → adds `external-fail-open-flag`: set_flag, blast **low**, conf 0.65, MTTR 2, tool `feature_flags.set_variant{flag:paymentGatewayProvider, variant:secondary}`. (No other pattern's keywords all appear.)
3. **Score** (env=production, prefer_safe=True):
   | option | (6−blast)×10 | conf×5 | rollback | env | **total** |
   |---|---|---|---|---|---|
   | rca-step-1 (low, 0.9, tested) | 50 | 4.5 | 3 | +5 | **62.5** |
   | external-fail-open (low, 0.65, tested) | 50 | 3.25 | 3 | +5 | **61.25** |
4. **Sort** → #1 `rca-step-1` (62.5), #2 `external-fail-open` (61.25). `recommended_option_id=rca-step-1`. `confidence_score = mean(0.9, 0.65) = 0.775`.
5. **Hand-off:** operator picks #1 → "Send to Auto-Healer" → Auto-Healer enforces `auto_heal.lite.execute` → on approve+live → `feature_flags.set_variant(paymentFailure, off)` → **payment heals**.

### Example 2 — Kafka consumer lag (ranking deliberately overrides RCA's #1)
**RCA verdict:** `service="checkout", root_cause="kafka consumer lag climbing on payment events", confidence=0.7, ranked_fix_steps=[{description:"Investigate stuck consumer", blast_radius:"medium", action_type:"manual", requires_hitl:true}]`.

1. **RCA → option** `rca-step-1`: manual (tool None), blast **medium** (score 3), conf 0.7, rollback_tested **False**, MTTR 10.
2. **Catalog:** cause contains `kafka` + `lag` → adds **two** options:
   - `kafka-restart-consumer`: RESTART, blast medium, conf 0.7, tested True, MTTR 8, tool `k8s.deployment.restart`.
   - `kafka-circuit-breaker-disable-producer`: CIRCUIT_BREAKER, blast **low**, conf 0.5, tested True, MTTR 2, tool `feature_flags.set_variant{flag:checkoutProducerCircuitBreaker, variant:on}`.
3. **Score** (production, prefer_safe):
   | option | (6−blast)×10 | conf×5 | rollback | env | **total** |
   |---|---|---|---|---|---|
   | **circuit-breaker** (low, 0.5, tested) | 50 | 2.5 | 3 | +5 | **60.5** |
   | kafka-restart (medium, 0.7, tested) | 30 | 3.5 | 3 | 0 | **36.5** |
   | rca-step-1 (medium, 0.7, untested) | 30 | 3.5 | 0 | 0 | **33.5** |
4. **Sort** → #1 **circuit-breaker (60.5)**, #2 kafka-restart (36.5), #3 rca-step-1 (33.5). `recommended_option_id=circuit-breaker`. `confidence_score = mean(0.5,0.7,0.7) = 0.633`.
5. **The teaching point:** RCA's own step (medium-blast, untested rollback) is ranked **last**; the catalog's **low-blast, proven-rollback** circuit breaker is recommended first. The safety-dominant score is *designed* to prefer "stop the bleeding reversibly" over a riskier direct fix — exactly the Day-1 "first, do no harm" intent.
6. **Hand-off:** picking #1 → Auto-Healer → `feature_flags.set_variant(checkoutProducerCircuitBreaker, on)` after approval.

---

# PART 3 — How the two relate (and the technique summary)

- **RA-005 is Reactive** — it answers *"who do we tell and how loudly?"* the moment an incident is triaged. It **emits**, never changes systems (HITL: None).
- **PRS-001 is Prescriptive** — it answers *"what should we do about it?"* after RCA. It **ranks options**, never executes (the gate bites at Auto-Healer, HITL: Required).
- They don't call each other; both are links in the same chain (`Triage → … → RA-005 notify`, and separately `RCA → PRS-001 → Auto-Healer`).

### Every technique used here, and how it works (one-liners)
| Technique | Used by | How it works |
|---|---|---|
| **Regex tokenization** | RA-005 | `[a-z0-9]{3,}` splits text into ≥3-char lowercase tokens |
| **Set intersection (overlap count)** | RA-005 category match | `\|alert_tokens ∩ category_keywords\|` — more shared words = stronger match |
| **Weighted linear scoring** | RA-005 on-call, PRS-001 ranking | sum of weighted terms; highest score wins; fully explainable |
| **Substring set-containment (AND)** | PRS-001 catalog | a pattern fires only if *every* keyword is a substring of the cause |
| **Business-hours gating** | RA-005 | UTC hour ∈ [9,18) flips Sev-2 between notify and page |
| **Slack Block Kit + mention rewrite** | chatops | dictionary lookup `@handle`→`<@U…>` so Slack actually pings |
| **Idempotency / dedup key** | PagerDuty adapter | stable `dedup_key` makes re-fires update one incident, not spam |
| **Fan-out with per-sink isolation** | ChatOpsClient | one message → N adapters; one failure can't block the rest |
| ~~Embeddings / cosine similarity~~ | **NOT here** | used in RA-001/RA-002 instead — these two are deterministic by design |

**Bottom line:** RA-005 and PRS-001 are intentionally **deterministic and explainable** — every routing decision and every ranking can be reproduced and reasoned about from the inputs, with no model in the loop. That's a feature (auditable, testable, tunable by a one-line config change), not a gap.

---

*Companion to [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md), [CODEBASE_INTERNALS.md](CODEBASE_INTERNALS.md), and [AGENT_EXECUTION_ANALYSIS.md](AGENT_EXECUTION_ANALYSIS.md).*
