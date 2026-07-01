# On-call DB — setup & swap-to-real-Slack runbook

The on-call DB drives RA-005's choice of *who* gets notified for each
alert: which team owns the service, which engineer is on shift right
now, what sub-domain (failure category) they specialise in, and which
Slack ID to ping for a real notification.

This doc is the operator's runbook. Read it once when you onboard a
teammate or swap from placeholder data to real Slack IDs.

---

## 1. What's already in place

Four tables in `data/state.db`:

| Table | What it stores |
|-------|----------------|
| `engineers` | name, email, slack_handle, slack_user_id, primary_team, skills (CSV), timezone, active |
| `shifts` | engineer_id, team, day_of_week (0=Mon..6=Sun), start_hour_utc, end_hour_utc, role |
| `failure_categories` | name (slug), display_name, description, team, keywords_csv |
| `engineer_expertise` | (engineer_id, category_id) PK, proficiency_level, incidents_resolved, feedback_score, manual_priority |

After running `uv run python -m scripts.seed_oncall`, the DB contains
**5 placeholder engineers across 3 teams**:

| Key       | Name    | Team             | Skills                    | Shift (UTC)         | Role                |
|-----------|---------|------------------|---------------------------|---------------------|---------------------|
| chinmay   | Chinmay | Payments Team    | payments, kafka           | Mon–Fri 03–12       | primary             |
| riya      | Riya    | Payments Team    | payments, kubernetes      | Mon–Fri 11–20       | primary (evening)   |
| arjun     | Arjun   | Order Experience | cart, checkout, payments  | Mon–Fri 03–12       | primary             |
| meera     | Meera   | Order Experience | cart, kubernetes          | Mon–Fri 11–20       | primary (evening)   |
| vikram    | Vikram  | Platform         | kubernetes, observability | All days, always-on | **wildcard** manager_escalation |

Vikram is the 24/7 **global** safety net — shift row tagged with the
special team key `"*"`, so any alert on a team that isn't otherwise
covered (Catalog Team, Ads Team, Communications, etc., from the
OpenTelemetry demo's CMDB) still pages Vikram on Sev-1 / Sev-2
after-hours. See §5 for the lookup ladder.

The Slack handles in the DB are `@chinmay`, `@riya`, … and the user IDs
are placeholders (`UPLACEHOLDER1`..`UPLACEHOLDER5`). Until you supply
real IDs via the env (Phase 3 below), Slack renders mentions as plain
text — the notification still posts but **doesn't ping anyone for real**.

Plus **8 placeholder failure categories** that RA-005 matches against
alert keywords to pick the right sub-domain specialist:

| Slug                  | Team             | Sample keywords                                |
|-----------------------|------------------|------------------------------------------------|
| payment-gateway       | Payments Team    | payment, gateway, 5xx, charge, authorize       |
| payment-database      | Payments Team    | payment, database, sql, connection, pool       |
| payment-kafka         | Payments Team    | payment, kafka, consumer, lag, partition       |
| cart-state            | Order Experience | cart, session, redis, valkey                   |
| checkout-flow         | Order Experience | checkout, order, placement, confirmation       |
| kubernetes-platform   | Platform         | kubernetes, k8s, pod, kubelet, etcd            |
| observability         | Platform         | prometheus, grafana, jaeger, otel              |
| networking            | Platform         | network, cdn, dns, ingress, latency            |

`engineer_expertise` then maps each engineer to the categories they
own, with `proficiency_level` (novice/intermediate/expert/principal),
`incidents_resolved` (track record), `feedback_score` (1.0–5.0), and
`manual_priority` (operator override). When two engineers from the
same team are on shift, the one whose expertise overlaps the alert's
keywords most strongly wins — see §5.

---

## 2. Quick start (placeholder mode)

```powershell
# 1. Make sure the DB exists
uv run python -m scripts.seed_oncall

# 2. Run the server
.\start.ps1

# 3. Trigger a notification (use any fixture)
.\scripts\demo\fire.ps1 payment_cpu_spike
```

On the dashboard's Notifications page you'll see the card mention
`@chinmay`. In Slack (if you have a webhook configured) it'll appear as
literal text `@chinmay`. **No real ping yet** — that's Phase 3 below.

---

## 3. Swap to real Slack pings (one-time, ~10 min)

You need three things:

### 3.1 A Slack webhook URL pointed at your team channel

Already set up if you've used the demo Slack integration before. Otherwise:

1. Go to https://api.slack.com/apps → "Create New App" → "From scratch".
2. Pick your workspace; name the app something like "Adaptive AIOps".
3. Enable **Incoming Webhooks** → **Add New Webhook to Workspace** → pick the channel.
4. Copy the URL into `.env`:
   ```
   AIOPS_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
   ```

### 3.2 Real Slack user IDs for the engineers in the DB

Each teammate's Slack user ID is **not** their @-handle. It's a string
like `U05ABC123`.

To find it:
1. Open Slack → click the person's name → "View profile" → "•••" menu → **"Copy member ID"**.
2. The ID starts with `U` (regular users) or `W` (guests).

Collect IDs for the 5 placeholders. Example:

| Placeholder | Real Slack user ID (yours will differ) |
|-------------|----------------------------------------|
| UPLACEHOLDER1 (Chinmay) | U05CHINMAY |
| UPLACEHOLDER2 (Riya) | U05RIYA |
| UPLACEHOLDER3 (Arjun) | U05ARJUN |
| UPLACEHOLDER4 (Meera) | U05MEERA |
| UPLACEHOLDER5 (Vikram) | U05VIKRAM |

### 3.3 Put real identities in `.env.shared` (encrypted), not in git

The committed `slack_users.json` and `scripts/seed_oncall.py` MUST stay
on placeholder values — committing real workspace IDs / emails would
publish them in git history forever. Real identities live in
`.env.shared` (encrypted via git-crypt; see `SECRETS.md`) and are
merged on top of the placeholders at startup / seed time.

#### a) Real Slack handle → user-ID map

Add a single-line JSON object to `.env.shared`:

```dotenv
AIOPS_SLACK_USER_MAP_JSON={"chinmay":"U05CHINMAY","riya":"U05RIYA","arjun":"U05ARJUN","meera":"U05MEERA","vikram":"U05VIKRAM","chinmay@example.com":"U05CHINMAY","riya@example.com":"U05RIYA","arjun@example.com":"U05ARJUN","meera@example.com":"U05MEERA","vikram@example.com":"U05VIKRAM"}
```

Keep both the handle and `@example.com` email keys — RA-005 emits the
email form when no slack_handle is stored on the engineer row. The
chatops seam's shared loader (`aiops/tools/chatops/adapters/_slack_user_map.py`)
reads this env var and merges it on top of the file at startup, for
both the webhook and bot adapters.

#### b) Real engineer roster (name, email, slack_handle, slack_user_id)

Add another single-line JSON object to `.env.shared`, keyed by the
stable engineer `key` (`chinmay`/`riya`/`arjun`/`meera`/`vikram`):

```dotenv
AIOPS_ONCALL_ROSTER_JSON={"chinmay":{"name":"Chinmay K","email":"chinmay@org.com","slack_handle":"@chinmay-k","slack_user_id":"U05CHINMAY"},"riya":{...},...}
```

Only the four fields shown above are honoured by `_resolve_engineers`;
other fields are ignored. Team / skills / timezone stay in code so
they show up in PR diffs.

Then re-seed:

```powershell
uv run python -m scripts.seed_oncall --force
```

`--force` wipes engineers, shifts, categories, and expertise rows
before re-inserting from the merged (env-overridden) roster.

Restart the server (`.\stop.ps1; .\start.ps1`); the Slack adapters
re-read the env-merged user map on startup.

### 3.4 Test the real ping

```powershell
.\scripts\demo\fire.ps1 payment_cpu_spike
```

In your Slack channel you should see a colored Block Kit message with
`<@U05CHINMAY>` rendered as a clickable mention. Chinmay (or whoever
the on-call algorithm picks) should get a real Slack notification —
red dot, phone buzz, the whole thing.

---

## 4. Day-to-day changes

| Task | How |
|------|-----|
| Add a new engineer | Edit `scripts/seed_oncall.py` → re-run with `--force` |
| Change someone's shift | Same as above (shifts live in the same script) |
| Change someone's skills | Edit `skills_csv` for that engineer, re-run with `--force` |
| Take someone off-call permanently | Set `active=False` on the row (SQL) or remove them from the seed script |
| Update a Slack handle | Edit the seed + `slack_users.json`, re-run seed with `--force` |
| Add a new team | Add engineers with `primary_team="New Team"` + shifts pointing at the team in the seed script |

> **Why `--force` rather than incremental edits?** POC-scale (a few
> dozen engineers). The seed script is the source of truth; the DB is
> a cache. Production deployments will get a `/dashboard/oncall` UI for
> live edits (issue ON-CALL-2, planned).

---

## 5. How the routing works under the hood

```
   Alert fires
        │
        ▼
   RA-001 Alert Triage
        │
        │  - Looks up CMDB → owning team = "Payments Team"
        │  - Looks up oncall.schedule.lookup → engineer_email
        │     (the DB provider now returns slack_handle too)
        │
        ▼
   TriageVerdict (carries assigned_engineer email + alert_summary)
        │
        ▼
   RA-005 Notification Router
        │
        │  - _category_keywords_for(verdict): tokenises service +
        │    alert_summary + recommended_runbook
        │  - _resolve_oncall(verdict): calls oncall.schedule.lookup
        │    with category_keywords; the DB provider routes via
        │    find_best_for_team_and_category and returns:
        │      { slack_handle, slack_user_id, role,
        │        matched_category, matched_category_display }
        │  - _mentions_from(verdict, oncall): picks the slack_handle
        │    (rewritten to <@U…> later) or falls back to the email
        │  - _render_body(verdict, reason, oncall): writes a
        │    structured body with "What failed / Application /
        │    Sub-domain / On-call: <name>"
        │
        ▼
   ChatMessage(mentions=["@chinmay"], category_display="Payment Gateway")
        │
        ▼
   Chatops seam fans out to every adapter
        ├─► JsonFileChatOpsAdapter → audit log (literal "@chinmay")
        ├─► WebSocketChatOpsAdapter → dashboard card
        ├─► SlackWebhookAdapter (channel post):
        │     1. Looks up "chinmay" in the merged user-map → "U05CHINMAY"
        │     2. Rewrites mention to "<@U05CHINMAY>"
        │     3. Adds Block Kit fields including the Sub-domain
        │     4. POSTs to AIOPS_SLACK_WEBHOOK_URL
        ├─► SlackBotAdapter (personal DM, only on page_oncall):
        │     1. chat.postMessage with channel=<user_id> → opens DM
        │     2. Same Block Kit fields as the webhook adapter
        └─► PagerDutyAdapter (if "page_oncall" in actions) → phone call
```

### Lookup ladder — never drop a Sev-1

`find_oncall_for_team(team)` walks the following ladder top-down and
returns the first match:

1. **Team-specific primary** on shift right now.
2. **Team-specific secondary** on shift.
3. **Team-specific manager_escalation** (treated as always-on regardless
   of stored hours; the role flag overrides the hours).
4. **Global wildcard escalation** — `ShiftRow.team == "*"` with role
   `manager_escalation`. Engaged when the requested team has no
   engineers in the DB at all (e.g. an OTel-demo alert for the `ad`
   service whose CMDB team `Ads Team` isn't onboarded yet). The
   returned `OnCallEngineer.team` keeps the original requested team so
   the audit trail / channel name stays honest; the `role` field is
   `manager_escalation` so RA-005 + Slack can mark the page as
   "platform escalation."

Returns `None` only when BOTH the team-specific ladder AND the
wildcard rung are empty — typically because the DB has been wiped and
not re-seeded. RA-005's Sev-3 / Sev-4 anti-fatigue rule clears
`mentions` anyway, so the missing engineer only matters for Sev-1 +
Sev-2 after-hours where a `page_oncall` action would otherwise fire.

### Specialist selection — overlap-weighted scoring

When two engineers from the same team are on shift,
`find_best_for_team_and_category` picks the one whose expertise area
best matches the alert. The score for each (engineer, matching
category) pair is:

```
expertise_score = proficiency_weight[level]
                + min(incidents_resolved, 25) * 2
                + feedback_score * 20
                + manual_priority * 50
weighted_score  = expertise_score × keyword_overlap_count
```

where `proficiency_weight = {novice: 10, intermediate: 50, expert: 100, principal: 150}`
and `keyword_overlap_count` is how many of the alert's tokens overlap
the category's `keywords_csv`. Multiplying by the overlap count is
load-bearing: every payment category contains the keyword "payment",
so without it the specialist with the highest *generic* expertise
would always win even on a clearly-database alert. The matched
category surfaced on the ChatMessage is the **alert's** top-overlap
category (so the Slack reader sees what failed, not what the picked
engineer happens to be best at).

The agent code is **completely unaware** of which adapters are active
or what Slack ID corresponds to which name. That isolation is what
makes vendor swaps cheap.

---

## 6. Troubleshooting

| Symptom | Probable cause |
|---------|----------------|
| Card on dashboard shows `@oncall@payments.example.com` | DB lookup returned no slack_handle. Either the engineer has none stored, or the team has no engineers. Re-seed. |
| Slack message arrives but doesn't ping | `slack_users.json` doesn't have this name mapped. Check that the file has both `<name>` and `<email>` keys. |
| Slack message says `<@UPLACEHOLDER1>` literally | You forgot to update `slack_users.json` to real IDs. Step 3.3.b. |
| Wrong person gets paged | Check shift rules: `SELECT * FROM shifts WHERE engineer_id = ?`. Wrong day_of_week or hours? |
| Nobody gets paged on Sev-1 | The team has no engineers in the DB AND no `manager_escalation` row. Add Vikram (or equivalent) to the team. |
| Server logs `"oncall: engineers table empty"` | Run `uv run python -m scripts.seed_oncall`. |

---

## 7. Where the code lives

| File | Role |
|------|------|
| [aiops/state/models.py](../aiops/state/models.py) | `EngineerRow` + `ShiftRow` + `FailureCategoryRow` + `EngineerExpertiseRow` SQLModel tables |
| [aiops/state/oncall_repository.py](../aiops/state/oncall_repository.py) | `find_oncall_for_team` (shift + skill ladder) + `find_best_for_team_and_category` (overlap-weighted expertise) |
| [aiops/tools/oncall.py](../aiops/tools/oncall.py) | DB-backed `oncall.schedule.lookup` provider; accepts `category_keywords` |
| [scripts/seed_oncall.py](../scripts/seed_oncall.py) | Seeds the 5 placeholder engineers + shifts + categories + expertise; `_resolve_engineers` merges `AIOPS_ONCALL_ROSTER_JSON` |
| [aiops/tools/chatops/adapters/_slack_user_map.py](../aiops/tools/chatops/adapters/_slack_user_map.py) | Shared loader: file placeholders + `AIOPS_SLACK_USER_MAP_JSON` env override |
| [aiops/tools/chatops/adapters/slack_users.json](../aiops/tools/chatops/adapters/slack_users.json) | Placeholder handle → Slack user-ID map (committed; real IDs from env) |
| [aiops/tools/chatops/adapters/slack.py](../aiops/tools/chatops/adapters/slack.py) | Webhook adapter; renders Application + Sub-domain Block Kit fields |
| [aiops/tools/chatops/adapters/slack_bot.py](../aiops/tools/chatops/adapters/slack_bot.py) | Bot adapter; DMs the picked engineer on `page_oncall` actions |
| [agents/notification_assembler/agent.py](../agents/notification_assembler/agent.py) | RA-005+006; `_category_keywords_for` + `_resolve_oncall` + `_mentions_from` + structured body |
| [tests/test_oncall.py](../tests/test_oncall.py) | 24 tests covering repo (shift + expertise) + tool + RA-005 integration |
