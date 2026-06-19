# AI-SRE Live Demo Showcase — Engineering Walkthrough

> Audience: Engineering Managers, Principal Engineers, SRE, Architects, Product Leadership.
> Goal: a live product demo that *feels like a real production incident* and showcases the engineering, not slides.
> Anchor scenario: **product-catalog latency spike** (`slow-product-catalog`) — a `flagd` feature flag `productCatalogFailure` injects ~5s latency, driving p95 to ~5.2s against a 1.0s SLO. Inject it as **customer-facing / Sev-1** so the War-Room Assembler engages. This is the only fully wired, CLI-runnable, eval-backed end-to-end path, so the demo is *real*, not staged screenshots.

---

## THE 6 AGENTS (final lineup)

| # | Agent | Catalog ID | Role in the story |
|---|-------|-----------|-------------------|
| 1 | Alert Triage | RA-001 | Detects, dedups, severity-classifies, assigns owner |
| 2 | Incident Classifier | RA-002 | RAG over history — "have we seen this?" + self-learning + routing |
| 3 | Auto-Ticketing | RA-003 | Opens the ServiceNow incident (Grafana panel attached), suppresses dupes |
| 4 | War-Room Assembler | RA-006 | Sev-1/2: stands up a real Slack channel + bridge, invites the SME |
| 5 | RCA Agent | PRS-008 | Identifies root cause, grounded vs live infra; ranked reversible fix under human approval |
| 6 | Knowledge Synthesizer | PRS-007 | Postmortem + runbook + redacted KB; closes the loop |

The human-in-the-loop **approval moment** is preserved on the **RCA console** itself: clicking *Approve & Apply* triggers the REQUIRED-gated `rca.fix_step.execute`, which blocks until a human approves (Approvals tab + Slack prompt). So we keep the trust drama without featuring a separate remediation agent. (Notification Router RA-005 still fires automatically and can be named in passing.)

---

# PART A — THE 6 AGENTS

For each: (1) why impressive, (2) manual work replaced, (3) business value, (4) inputs, (5) outputs, (6) intelligence/reasoning, (7) why leadership should care.

---

## AGENT 1 — Alert Triage (RA-001)

**1. Why impressive**
An 8-stage pipeline, not a route script. Semantic dedup (`sentence-transformers/all-MiniLM-L6-v2`, cosine ≥ 0.85) against SQLite-persisted cluster centroids that update via an **EMA (α=0.2)** so a chain of near-duplicates can't "walk" the centroid away from origin; parallel Prometheus + Jaeger fetch via `ThreadPoolExecutor` (latency collapses from sum-of-queries to max-of-queries); a 30s **idempotency** window distinct from dedup (handles Alertmanager transport retries); defense-in-depth — prompt-injection sanitization treating all monitoring fields as *untrusted data*, plus PromQL label-value escaping. Deterministic-first; the LLM is only a fallback consultant, so it's reproducible and eval-scorable.

**2. Manual work replaced**
The on-call's first five minutes: read the alert, pull dashboards/traces, judge severity, find the owning team, find the on-call engineer and runbook, suppress duplicate pages, write the initial summary.

**3. Business value**
MTTA → near-zero; eliminates alert-storm fatigue. Every page that reaches a human is already de-duplicated, severity-ranked, owner-assigned, and summarized.

**4. Inputs**
Canonical `Alert` (alert_id, service, metric, value, threshold, timestamp, source, severity_hint, labels, annotations) + live Prometheus series (rate, 5xx, p95, CPU/mem) + Jaeger trace summaries.

**5. Outputs**
`TriageVerdict`: `severity` (Sev-1…Sev-4), `affected_service`, `confidence_score`, `alert_summary`, `assigned_team`, `assigned_engineer`, `recommended_runbook`, `duplicate_alert_count`, `status` (Active/Suppressed), `audit_metadata.decision_trace`.

**6. Reasoning**
Validate → idempotency short-circuit → two-stage dedup (exact cluster-key hash, then embedding cosine) → parallel multi-signal correlation → severity (rule-first, LLM consult only when inconclusive) → ownership via CMDB + on-call seams → LLM one-line summary → assemble + persist. Honest low-confidence signaling.

**7. Why leadership should care**
The funnel that protects every downstream human. Deterministic-first means CI parity with prod — that's what makes it safe in front of real pages.

---

## AGENT 2 — Incident Classifier (RA-002)

**1. Why impressive**
A 4-tier **retrieval-augmented + cost-aware escalation** classifier that *learns over time without retraining*. It embeds the incident, runs brute-force cosine vector search (top-5, ≥0.60) over a SQLite store of historical incidents, and — if the top match is ≥0.85 similar and the top-3 agree — returns a classification **with no LLM call at all** (Tier 1). Tiers 2/3 invoke the LLM with retrieved evidence or cold few-shot, confidence clamped per tier; Tier 4 is a keyword fallback. Every live classification is **persisted back** as a `LIVE-<alert_id>` row, so the agent gets smarter with every incident. It also **re-queries the CMDB independently** rather than trusting upstream fields.

**2. Manual work replaced**
"Have we seen this before?", manual categorization (infra / app / network / vendor / change), and lookup of owning team, on-call specialist, runbook, and similar past incidents.

**3. Business value**
Institutional memory that compounds — the 50th payment incident is classified faster and more accurately than the 1st — plus controlled LLM spend (the cheap path handles the cases we already understand).

**4. Inputs**
`Alert` + `TriageVerdict`. Embedding text built from service, severity, summary, metric, annotations, sorted labels.

**5. Outputs**
`Classification`: `incident_type` (1 of 5), `confidence`, `rationale`, `tags`, `probable_root_cause`, `routing_team`, `on_call_engineer`, `recommended_runbook`, `dependencies`, **`similar_incident_ids`** (+ matched incidents and similarity scores in the audit snapshot).

**6. Reasoning**
Embed → vector search → Tier-1 similarity-wins (no LLM) → Tier-2 LLM+evidence → Tier-3 LLM cold → Tier-4 keyword, with per-tier confidence clamping and graceful degradation when embeddings/LLM are unavailable. Independent CMDB/on-call re-query. Persist-back learning loop.

**7. Why leadership should care**
Turns every incident into a permanent asset, and clearly demonstrates the system is engineered for cost and calibration — it knows when *not* to spend an LLM call.

---

## AGENT 3 — Auto-Ticketing (RA-003)

**1. Why impressive**
Solid integration engineering with the right safety reflexes. It translates RA-002's internal `incident_type` into ServiceNow's fixed category choice list **at the vendor boundary** (so the classifier taxonomy stays vendor-neutral); maps severity → ServiceNow urgency (Sev-1→1 … Sev-4 clamps to 3); **suppresses duplicate tickets** when the verdict status is `Suppressed`; auto-renders and **attaches the relevant Grafana panel PNG** to the incident (every failure path swallowed and audited so a missing graph never blocks ticket creation); fires the chat-ops notification **even if ticket creation fails** so a human always sees the alert; and sanitizes attachment filenames against path traversal.

**2. Manual work replaced**
Manually opening a ServiceNow incident, setting urgency/category/assignment group, pasting the alert context and runbook, screenshotting and attaching a Grafana panel, posting to the right channel — and recognizing/suppressing duplicates.

**3. Business value**
Zero-touch, consistent, fully-contextualized incident records — with the dashboard screenshot already attached — and no duplicate-ticket noise.

**4. Inputs**
A `TriageVerdict` dict + optional `Classification` (RA-002) for the SNOW category + optional `alert_name` (Prometheus rule name) for the Grafana panel lookup (`grafana_panels.json`).

**5. Outputs**
`TicketRecord`: `created`, `ticket_id` (e.g. `INC0010001`), `system` (servicenow/mock/none), `urgency` (1–3), `short_description` (≤160 chars), `channel_notified`, `notification_sent`, audit trace.

**6. Reasoning**
Suppression short-circuit → severity→urgency → severity→channel → build multi-section description (alert summary, routing, RA-002 classification, RA-001 decision trace) → ITSM create → chat-ops notify (fires even on ITSM failure) → best-effort Grafana panel attach. Deterministic (no LLM).

**7. Why leadership should care**
This is the system-of-record integration leadership and audit care about: every incident is captured in ServiceNow, correctly categorized and evidenced, automatically — and the duplicate suppression directly cuts ticket-queue noise.

---

## AGENT 4 — War-Room Assembler (RA-006)

**1. Why impressive**
On a Sev-1/Sev-2 it stands up the incident bridge for you — and it's **real, not mocked**: with a Slack bot token it makes live `conversations.create` → `conversations.invite` → `chat.postMessage` calls to create an actual `war-room-<incident-id>` channel, invites the resolved on-call SME, and posts the opening context. It mints a working **Jitsi** click-to-join bridge per incident, builds a **context pack** (verdict facts + live telemetry), and seeds a **timeline**. It's vendor-neutral (the agent never imports the Slack SDK — it goes through the `chatops.war_room.create` capability), has a clean pure-`decide` / side-effecting-`assemble` split, a full `audit_trace`, idempotent channel reuse, and a **graceful simulated fallback** (identical shape) when no token is present so it never breaks the pipeline. It's fired in the **background** off the triage hot path because the Slack calls are slow.

**2. Manual work replaced**
The human incident commander's first-15-minutes scramble: create the bridge/channel, figure out and page the right on-call SME, pull current metrics/traces into one place, and start the incident timeline. (KPIs in its README: *time-to-bridge* and *SME coverage %*.)

**3. Business value**
Cuts the coordination delay at the start of every major incident to seconds, so responders join a fully-prepared room instead of building one under pressure.

**4. Inputs**
The `TriageVerdict` (enriched with `incident_id` by RA-003): severity, status, affected_service, assigned_team/engineer, alert_summary, runbook. Plus read-only seams: on-call lookup, Prometheus, Jaeger.

**5. Outputs / state**
`WarRoomAssembly`: `assembled`, `channel`, `title`, `invited` (list of `InvitedSME` with handle/name/team/reason/source/invite_status), `context_pack` (label/value/source), `timeline`, and bridge fields `bridge_status` (created/simulated/…), `bridge_url` (Slack deep link), `meeting_url` (Jitsi). The server wraps it in a feed row with lifecycle `status` (open → in_call → call_ended → resolved) and per-SME attendance.

**6. Reasoning**
Severity gate (Sev-1/2 and not Suppressed, else no-op) → resolve on-call SME for the owning team → build context pack from verdict facts + best-effort live telemetry → create the Slack channel + Jitsi bridge (or simulate) → post opening message → seed timeline. **Fully deterministic — no LLM.**

**7. Why leadership should care**
This is the most visceral "the machine did the human's job" moment in the demo — a real Slack war room appears with the right person already invited. *(Honest scope: v1 SME selection is the owning team's on-call only; CMDB-owner / dependency-owner invites and deeper context are documented and planned but not yet implemented. War-room state is in-memory; lifecycle/attendance are operator-advanced.)*

---

## AGENT 5 — RCA Agent (PRS-008) ★ headline

**1. Why impressive**
An LLM reasoner (`claude-sonnet-4-6`, temp 0.2) whose output is **grounded against live infrastructure** to defeat hallucination: it corrects a wrong feature-flag name against a curated service→flag map, and queries live `flagd` config to **downgrade any invented flag to a `manual` step** so the dashboard never offers a one-click button for a flag that doesn't exist. The human-in-the-loop guarantee is enforced at the **type level** (`requires_hitl: Literal[True]`) — the schema literally cannot represent an ungated fix. When evidence is thin it returns an honest **0.2-confidence "manual investigation required"** verdict instead of a confident wrong answer.

**2. Manual work replaced**
The senior engineer's diagnostic leap: read the evidence, deduce the root cause, hand-write a ranked, reversible remediation plan with rollback and blast-radius per step.

**3. Business value**
Where MTTR is won — a correct, reversible, ranked fix in seconds, grounded so it can't recommend a fix for something that isn't real.

**4. Inputs**
The triage verdict (affected_service, severity, summary, decision trace); optional correlation evidence; optional `scenario_id`.

**5. Outputs**
`RCAVerdict`: `root_cause`, `ranked_fix_steps` (≤3, index 0 highest confidence; each with `description`, `blast_radius`, `rollback`, `action_type` set_flag/rollback_deploy/manual, `flag`, `requires_hitl=True`), `confidence_score`, full decision trace.

**6. Reasoning**
Single JSON-mode reasoning pass with domain heuristics in the system prompt (Occam's razor; "a flipped feature flag is a more common cause of sudden, service-isolated latency than a bad deploy"; "restarting a pod does NOT unset a feature flag"). Untrusted-data hardening on all injected fields. Then the grounding layer: curated-map correction + live-flagd validation + action coercion. Deterministic confident fallback for the known scenario; honest low-confidence otherwise.

**7. Why leadership should care**
The direct, demonstrable answer to "what if the AI confidently recommends the wrong fix?" — it grounds against reality, ranks by honest confidence, and cannot bypass the human gate.

---

## AGENT 6 — Knowledge Synthesizer (PRS-007)

**1. Why impressive**
Closes the loop: after a ticket resolves, it drafts a blameless postmortem, suggests a new-or-updated runbook, and produces a redacted KB article — and it **physically cannot self-publish** (writes `pending_review`; publishing is HITL-gated at the seam). Highlights: **redaction-before-persist** that scrubs PEM keys, AWS keys, JWTs, bearer tokens, emails, and validated IPs *before* storage and emits a value-free audit (counts per category, never the secret); **dual-mode RAG dedup** (embedding cosine 0.9, or Jaccard signature overlap when embeddings are absent); and a production-grade **ServiceNow watcher** that polls for resolved tickets with a 5-failure circuit breaker, checkpointing, and poison-ticket isolation.

**2. Manual work replaced**
The post-incident write-up nobody has time for: the blameless postmortem, timeline reconstruction, runbook creation/update from the actual fix, manual secret/PII scrubbing, and KB duplicate-checking — for *every* resolved ticket.

**3. Business value**
Every incident becomes permanent, searchable, compliance-safe institutional knowledge automatically — and feeds straight back into RA-002's memory.

**4. Inputs**
The resolved bundle: triage verdict, RCA verdict, optional classification/ticket/change records, resolved-at. The watcher also consumes live ServiceNow resolved/closed tickets.

**5. Outputs**
`SynthesisResult`: a `Postmortem` (what broke, root cause, timeline, fix, impact), a `RunbookSuggestion` (new/update), a redacted `KBArticle` (`pending_review`), a `DedupDecision`, a `quality_score`, a `redaction_summary`.

**6. Reasoning**
Resolve incident id / idempotency → reconstruct timeline from upstream agents' audit timestamps → grounded LLM postmortem ("do NOT invent causes not in the inputs") with deterministic fallback → runbook new/update suggestion → redact every persisted field → quality score + RAG dedup → persist as `pending_review`.

**7. Why leadership should care**
The compounding-returns agent — and proof of end-to-end discipline: even *knowledge publication* respects the human gate, and secrets never reach storage.

---

# PART B — THE DEMO STORYLINE (strongest flow)

```
  Inject: product-catalog latency spike — Sev-1 (flagd productCatalogFailure = on → p95 5.2s vs 1.0s SLO)
        │
        ▼ RA-001  Alert Triage ........ detect, dedup, Sev-1, assign owner          [AlertStream + Reasoning]
        ▼ RA-002  Incident Classifier . categorize + "seen this 3× before" + routing + self-learning
        ▼ RA-003  Auto-Ticketing ...... ServiceNow INC opened, RA-002 category, Grafana panel attached
        ▼ RA-006  War-Room Assembler .. REAL Slack war room + Jitsi bridge + on-call SME invited + timeline   [WarRoom]
        ▼ PRS-008 RCA Agent ........... root cause = productCatalogFailure, grounded vs live flagd; ranked fix
        │                                → human APPROVES on RcaConsole (HITL gate) → flag off → p95 recovers   [Approvals]
        ▼ PRS-007 Knowledge Synthesizer  blameless postmortem + runbook update + redacted KB (pending_review)   [Knowledge]
                                          └── feeds back into RA-002's memory ──┐
                                                                                ▲ (loop closed)
```

This mirrors the system's real execution (`run_reactive_flow`: RA-001 → RA-002 → RA-003 → RA-005, with RA-006 fired in the background for Sev-1/2; then RcaConsole `apply-fix` through the HITL gate; then the SNOW watcher → PRS-007). The demo *is* the product.

---

# PART C — STEP-BY-STEP DEMO NARRATIVE

> Setup: `.\start.ps1 -Fresh`; ensure Prometheus(9090)/Jaeger(16686)/frontend(8080) port-forwards; confirm the approver is installed (so HITL shows PENDING, not silent BLOCKED); for a live Slack war room, set `AIOPS_SLACK_BOT_TOKEN` (else it shows a clearly-labeled simulated room). Pre-open tabs: Overview, AlertStream, Reasoning, RcaConsole, Approvals, Knowledge, **WarRoom**.

---

### STEP 1 — The incident begins

**What appears on screen**
The **Overview** page. You click **Inject → product-catalog latency spike**. Within one broadcaster tick a red alert hits **AlertStream**: `latency_p95_seconds 5.2 (threshold 1.0)` on `product-catalog`.

**What I say**
> "Here we have a latency spike in production — product-catalog p95 just jumped to 5.2 seconds against a 1-second SLO. This is the 2am page that wakes someone up. Normally the next 30–40 minutes is one person hunting across dashboards. Watch what happens instead."

**Expected audience reaction**
Recognition — every SRE in the room has lived this page.

**Business value**
Establishes a real, relatable incident and the 30–40-minute manual baseline we're about to compress.

---

### STEP 2 — Alert Triage (RA-001)

**What appears on screen**
The alert resolves into a **TriageVerdict**: `Sev-1 · product-catalog · confidence 0.9 · Team: Catalog · On-call: <engineer> · Runbook: rb-product-catalog-latency`. Switch to **Reasoning** — the 8-stage trace animates: Validate → Deduplicate → Correlate → Classify severity → Resolve ownership → Summarize → Verdict.

**What I say**
> "Our Alert Triage agent already did the first five minutes of work. It pulled Prometheus and Jaeger *in parallel*, ran semantic de-duplication so a storm of identical alerts collapses to one page, judged severity, and resolved the owning team and on-call engineer from the CMDB. And it isn't a black box — every decision is traced, and this exact path runs identically in our CI test suite."

**Demo Script**
> "Normally an engineer spends the first five minutes just orienting — which service, how bad, who owns it, is this a duplicate. The Triage agent does that deterministically in under a second and hands a human a page that's already de-duplicated, ranked, owned, and summarized."

**Expected audience reaction**
Principal engineers clock "deterministic-first + decision trace" — the credibility moment.

**Business value**
MTTA → near-zero; dedup kills fatigue; CI parity = prod-safe.

---

### TRANSITION 1 → 2
> "Triage tells us *what* and *who*. The next question a good SRE asks immediately is — *have we seen this before?* That's usually locked in the head of whoever's been here longest. Watch the system answer it."

---

### STEP 3 — Incident Classifier (RA-002)

**What appears on screen**
The classification panel: `incident_type: application · routing: Catalog Team · similar_incident_ids: [INC-…, INC-…]` with similarity scores and the matched prior incidents.

**What I say**
> "Our Incident Classifier just searched our entire incident history with vector similarity and found we've seen this pattern before — here are the matching incidents and how similar they are. The clever part: when the historical match is strong enough, it skips the LLM call *entirely* — the cheap, deterministic path handles cases we already understand. And every new incident gets embedded back into the store, so this agent gets smarter with every page, with zero model retraining. It also picks the owning team, on-call specialist, and runbook — and that classification is about to feed straight into the ticket."

**Demo Script**
> "Normally 'have we seen this' depends on tribal memory. Our Classifier turns every past incident into searchable memory, finds the similar ones automatically, learns continuously, and is cost-aware enough to know when *not* to spend an LLM call."

**Expected audience reaction**
EMs and Product hear "compounding asset" and "cost-aware"; engineers note the self-learning persist-back loop.

**Business value**
Institutional knowledge that compounds; controlled LLM spend; faster classification on every repeat.

---

### TRANSITION 2 → 3
> "Now it routes — and the first thing that has to happen for any real incident is a system-of-record entry. Watch ServiceNow."

---

### STEP 4 — Auto-Ticketing (RA-003)

**What appears on screen**
A **ServiceNow incident** card: `INC0010001 · urgency 1 · category from RA-002 · short_description "[Sev-1] product-catalog latency…"`, with a **Grafana panel screenshot attached** and a chat-ops notification posted.

**What I say**
> "The Auto-Ticketing agent opened the ServiceNow incident — and notice it set the urgency from severity, and the *category* from the Classifier we just saw, translated into ServiceNow's own taxonomy at the vendor boundary. It even rendered the relevant Grafana panel and attached it to the ticket, so whoever opens this incident sees the graph immediately. And if ServiceNow had been down, it would still have fired the chat notification so a human saw the alert. Duplicates? Suppressed — no ticket storm."

**Demo Script**
> "Normally someone hand-opens the ticket, sets urgency and category, pastes context, screenshots Grafana, and posts to Slack. RA-003 does all of it in one shot — correctly categorized, evidenced, and de-duplicated."

**Expected audience reaction**
EMs and audit-minded leaders value the system-of-record discipline and the auto-attached evidence.

**Business value**
Zero-touch, consistent, fully-contextualized incident records; no duplicate-ticket noise.

---

### TRANSITION 3 → 4
> "We have a ticket. But a Sev-1 needs *people* — fast. Normally an incident commander now scrambles a bridge, figures out who to pull in, and starts a timeline by hand. Watch the system do it."

---

### STEP 5 — War-Room Assembler (RA-006)

**What appears on screen**
The **WarRoom** page (5s live poll): a new room `war-room-INC0010001` appears — status **open**, a **Join meeting** (Jitsi) button, a **Slack channel** button, **Invited SMEs** (the on-call engineer, with invite status), a **Context pack** (service, severity, runbook, live metrics), and a seeded **Timeline**. *(If a Slack token is configured, open the actual Slack channel it just created.)*

**What I say**
> "The moment this was classified Sev-1, the War-Room Assembler stood up the incident bridge — and this is *real*: it created an actual Slack channel, invited the on-call engineer, posted the opening context, and minted a video bridge. It also assembled a context pack — the verdict facts plus live telemetry — so whoever joins doesn't start from a blank page. A human IC would normally spend fifteen minutes doing exactly this under pressure."

**Demo Script**
> "Normally the first fifteen minutes of a major incident is pure coordination overhead — spin up a channel, page the right person, gather context, start the timeline. RA-006 does it in seconds, and responders join a room that's already prepared."

**Expected audience reaction**
The most visceral "the machine did the human's job" moment — especially if a real Slack channel opens on screen.

**Business value**
Coordination delay at the start of a major incident drops to seconds; responders join prepared, not scrambling.

*(If asked: v1 invites the owning team's on-call; CMDB-owner and dependency-owner invites are designed and on the roadmap. Be honest — they'll respect it.)*

---

### TRANSITION 4 → 5
> "People are in the room, the ticket's open. Now the question we actually pay senior engineers for — *why*. And this is where everyone's nervous about AI, because a confidently-wrong root cause is worse than none. Watch how we handle that."

---

### STEP 6 — RCA Agent (PRS-008) + the human gate

**What appears on screen**
The **RcaConsole**: `Root cause: feature flag productCatalogFailure is ON, injecting ~5s latency`; ranked fix step 1: `Set productCatalogFailure → off · blast radius: low · rollback: set back to on · requires approval`. Confidence 0.85. You click **Approve & Apply** → status flips to **PENDING_APPROVAL**. Switch to **Approvals** — a pending card appears (+ Slack prompt). You approve → flag flips to `off` → **EXECUTED** → p95 recovers on the chart.

**What I say**
> "The RCA agent identified the root cause — a feature flag flipped on, injecting five seconds of latency — and proposed a ranked, reversible fix. Here's the engineering that matters: the LLM proposes, then we *ground* it against reality. We validate the flag name against live flagd config; if the model invents a flag that doesn't exist, we automatically downgrade that step to 'manual' so this console never offers a one-click button pointing at nothing. And watch — it does *not* just apply the fix. The code path that touches production is physically unreachable until a human approves. Here's the approval, in the dashboard and Slack. I approve… and latency recovers. Who approved, what ran, the rollback plan — all in the audit trail."

**Demo Script**
> "Our RCA Agent identifies the root cause and proposes a ranked, reversible fix — grounded against live infra, and if it's not confident it *says so*, 0.2 and 'manual investigation required', instead of guessing. And nothing reaches production without a human, by construction. That's the difference between a toy and a tool."

**Expected audience reaction**
The "oh" moment for Principal Engineers and risk-conscious leadership: the hallucination problem and the "what stops it breaking prod" problem are both visibly engineered against.

**Business value**
Where MTTR is won — a correct, reversible, grounded fix in seconds, applied only under human approval with a full audit trail.

---

### TRANSITION 5 → 6
> "Incident resolved — and a verifier confirmed recovery and closed the ServiceNow ticket through a *second* human gate. Most teams stop here, exhausted, and the postmortem never gets written. Ours doesn't."

---

### STEP 7 — Knowledge Synthesizer (PRS-007)

**What appears on screen**
The **Knowledge** tab: a freshly drafted **postmortem** (what broke, root cause, timeline, fix, impact), a **runbook update suggestion** for `rb-product-catalog-latency`, and a **KB article** marked `pending_review` with `redaction_summary: no PII/secrets detected`.

**What I say**
> "The moment the ticket closed, the Knowledge Synthesizer drafted a blameless postmortem, reconstructed the timeline from every agent's audit trail, and proposed a runbook update based on the *actual* fix we applied. Two things to notice: it redacts secrets and PII *before* anything is written to storage — keys, tokens, emails, IPs — and reports what it scrubbed without ever logging the secret itself. And it *cannot* publish on its own; it's pending_review, gated for a human, like everything else. And here's the loop closing — next time this happens, the Classifier from step three finds *this* article."

**Demo Script**
> "Normally the postmortem is the thing nobody has time for, so the next on-call relearns the same lesson. Ours writes it automatically, safely, for every incident — and feeds it right back into the Classifier's memory."

**Expected audience reaction**
Product leadership sees compounding ROI; security sees redaction-before-persist; the room recognizes the loop closing back to Step 3.

**Business value**
Every incident → permanent, searchable, compliance-safe knowledge, automatically.

---

### CLOSING BEAT
> "Start to finish: a production latency spike — detected, recalled from history, ticketed, a war room stood up with the right people, root-caused, fixed under human approval, and documented — in the time it would normally take just to *find the right dashboard*. Six agents, each doing a different kind of reasoning, every one auditable and gated. This isn't a slide deck — it's the system, running."

---

# PART D — LEADERSHIP-FACING MATERIALS

## 1. 30-Second Executive Summary
> "We built an AI-SRE platform: six specialized agents that take a production incident from alert to closed postmortem with a human in the loop at every change. In a live latency incident, it detects and de-duplicates the alert, recalls similar past incidents, opens a fully-categorized ServiceNow ticket with the Grafana graph attached, stands up a real Slack war room with the on-call engineer invited, identifies the root cause — grounded against live infrastructure so it can't hallucinate a fix — applies a reversible fix only after human approval, and auto-writes the postmortem. It compresses 30–40 minutes of senior-engineer work into seconds, and every decision is audited and gated. It's not a prototype — it runs end-to-end on our real stack."

## 2. Two-Minute Demo Opening
> "Everyone's seen an AI demo that summarizes a log file. That's not this. What I'm going to show you is a *team* of agents handling a real production incident end-to-end — and more importantly, the *engineering that makes it safe enough to trust*, because that's the actual hard part.
>
> Three things to watch for. First — it's deterministic where it needs to be. Triage, de-duplication, ticketing, and the safety gate are rule engines that behave identically in CI and production; the LLM is used surgically. Second — it grounds itself against reality: when the root-cause agent proposes a fix, we validate it against live infrastructure config, and if it invents something that doesn't exist, we catch it. Third — nothing touches production without a human; the code that changes prod is physically unreachable until a person approves, and it fails closed.
>
> The scenario is real: a feature flag is about to inject five seconds of latency into our product-catalog service. That's a Sev-1. Let's watch six agents handle it — and I'll point out the engineering as we go."

## 3. Agent-Wise Talking Points (one line each)
- **RA-001 Alert Triage** — "Eight-stage pipeline; semantic dedup with drift-controlled centroids; parallel telemetry fetch; deterministic-first so it's CI-reproducible."
- **RA-002 Incident Classifier** — "RAG over incident history with self-learning persist-back; cost-aware tiering skips the LLM when history is decisive; finds similar incidents automatically."
- **RA-003 Auto-Ticketing** — "Zero-touch ServiceNow incident; classifier's category translated at the vendor boundary; Grafana panel auto-attached; duplicates suppressed; chat fires even if ITSM is down."
- **RA-006 War-Room Assembler** — "Real Slack channel + Jitsi bridge + on-call SME invited + context pack, in seconds; vendor-neutral; graceful simulated fallback offline."
- **PRS-008 RCA Agent** — "LLM reasoning *grounded against live flagd*; hallucinated fixes downgraded to manual; human gate enforced at the type level; honest low-confidence instead of confident-wrong."
- **PRS-007 Knowledge Synthesizer** — "Auto postmortem + runbook + KB; redaction-before-persist; cannot self-publish; production-grade ServiceNow watcher; closes the loop into the Classifier."

## 4. Questions Leadership Might Ask — and Best Answers

**Q: What stops the AI from breaking production?**
> "By construction, not policy. The fix-execution path calls the policy gate with `enforce()`, which raises and halts — the code that touches prod is unreachable until a human approves. It's dry-run-capable and fails *closed*: if approvals aren't wired, it blocks rather than acts. The human-in-the-loop requirement is enforced at the platform layer and pinned at the type level, so even a buggy agent can't emit an ungated fix."

**Q: How do you know it won't hallucinate a wrong root cause?**
> "Two layers. The RCA agent grounds its LLM output against live infrastructure — it validates proposed flag names against actual flagd config and downgrades anything invented to a manual step. And when evidence is thin it returns honest low confidence with 'manual investigation required' rather than a confident guess. We also score it against ground-truth 'truth files' in our eval harness."

**Q: Is this just GPT wrapped around our logs?**
> "No. Most of the load-bearing logic is deterministic rule engines — triage, de-duplication, severity, ticketing, war-room assembly, and the safety gate. The LLM is used surgically: root-cause reasoning, classification (only when history doesn't already answer it), and prose summaries. That's why it's reproducible in CI and safe near production."

**Q: What does this save us, concretely?**
> "It compresses the 30–40 minutes of orient-diagnose-coordinate at the front of every incident into seconds, and it eliminates the postmortem backlog. The compounding win is the Classifier: every incident becomes searchable memory, so repeat incidents resolve faster over time. MTTA → near-zero; coordination and known-pattern MTTR drop sharply."

**Q: What's the cost / LLM spend story?**
> "Engineered in from the start. The Classifier's tiered escalation skips the LLM entirely when historical similarity is decisive, and several agents — ticketing, war-room — use no LLM at all. We spend tokens only on the reasoning that needs them."

**Q: Is it locked to one vendor (Slack, ServiceNow, Grafana…)?**
> "No. Every external call goes through a capability registry — agents never import a vendor SDK. The classifier's category is even translated into ServiceNow's taxonomy at the boundary. Swapping Slack for Teams, or ServiceNow for Jira, is a configuration change at the seam, not an agent rewrite."

**Q: How do we trust it / audit what it did?**
> "Every agent emits a full `decision_trace` at each stage, persisted. You saw the 8-stage triage reasoning rendered live, and the war room carries its own audit trace. Every gated action records who approved, what ran, with which arguments, and the rollback plan."

**Q: Is the Slack war room real or staged?**
> "Real. With a bot token it makes live Slack API calls to create the channel, invite the on-call engineer, and post context, plus a working Jitsi bridge. Offline or without a token it produces an identically-shaped *simulated* room so demos and CI never break — and it labels it as simulated."

**Q: How much is real today vs. roadmap?**
> "The end-to-end path you saw runs live on our real stack today and is backed by golden evals. Some enhancements are explicitly deferred and labeled in code: the war room's CMDB-owner / dependency-owner SME invites, automatic rollback-on-failure, and LLM-generated escalation ladders. We shipped the safe core first."

**Q: What happens when the LLM provider is down?**
> "Graceful degradation — every LLM stage has a deterministic fallback, so triage, classification, ticketing, war-room assembly, and the safety gate all keep working. You lose some prose quality and nuance, not the pipeline."

---

### Pre-flight checklist (run before the room)
- `.\start.ps1 -Fresh` — clean flags, state.db, archived chatops log.
- Confirm Prometheus (9090), Jaeger (16686), frontend-proxy (8080) port-forwards are up.
- Confirm the approver is installed (so the HITL step shows PENDING, not silent BLOCKED).
- For a live Slack war room, set `AIOPS_SLACK_BOT_TOKEN` (scopes: channels:manage, chat:write, channels:read). Otherwise it shows a labeled simulated room.
- Inject the **Sev-1 / customer-facing** variant so RA-006 engages.
- Pre-open tabs: Overview, AlertStream, Reasoning, Knowledge/Classifier, **WarRoom**, RcaConsole, Approvals, Knowledge.
- Dry-run the inject once, then `/reset-all` before going live.
