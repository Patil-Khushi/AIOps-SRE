# Architect's retrospective — Phase 1, grounded in the GitHub issue tracker

**Author's hat:** the same solution architect who wrote [architect_retrospective_phase1.md](architect_retrospective_phase1.md) — but now with the issue tracker open and the v2 audit findings in hand.
**Audience:** still me, three weeks from now. Plus the lead reading this before approving Phase 2 scope.
**Purpose:** the first retrospective was a private letter written from memory. This one is grounded in receipts — every closed PR, every open issue, every label — and surfaces things the first retro missed because memory is generous and the issue tracker isn't.

**Companion documents:**
- [architect_retrospective_phase1.md](architect_retrospective_phase1.md) — the from-memory version. Read first; this one builds on it.
- [demo_readiness_audit_v2.md](demo_readiness_audit_v2.md) — the live-cluster audit that fixed v1's mis-diagnoses.
- [demo_fix_plan_tomorrow.md](demo_fix_plan_tomorrow.md) — the tactical fix list for the demo.

---

## 0. What this doc adds

The first retro had seven strategic mistakes and seven tactical ones. It was thorough but ungrounded — written from gut memory of what we decided, not from what the tracker actually records. This pass replays the same question (*what went wrong, what fix it*) against the receipts and finds **four new patterns, two corrections to the original retro, and one uncomfortable team-shape signal** the original missed because it's only visible at the issue level, not the code level.

The forward fix plan in §5 is keyed to **open issue numbers** so the work plugs into the existing backlog rather than inventing a parallel one.

---

## 1. The issue tracker as a forensic record

### 1.1 Volume + cadence

| Metric | Value | What it tells me |
|---|---|---|
| First issue created | 2026-05-12 | Tracker was opened **two days before the demo audit (today, 2026-05-14)**, not at project start. |
| Total issues | 58 | All in 3 days. |
| Closed | 35 | 60 % closure rate in 3 days — high. |
| Open | 23 | Mostly low/medium priority + "DEMO-N" eve-of-demo discoveries. |
| Merged PRs | 11 | Nine of them merged on 2026-05-13 alone. |
| **Issues opened by `UbiquotousPanda`** | **58 / 58** | **All of them.** No teammate has opened a single issue. |
| Issue authors other than UbiquotousPanda | 0 | — |
| Distinct PR authors | 4 (UbiquotousPanda, Patil-Khushi, Gaurav-Patil-1695, shrvri-not-yet) | — |

### 1.2 Throughput by priority

| Priority | Open | Closed | Closure % |
|---|---|---|---|
| high | 6 | 20 | **77 %** |
| medium | 10 | 8 | 44 % |
| low | 7 | 6 | 46 % |
| unset | 0 | 1 | — |

### 1.3 Throughput by area-label

| Label | Issues | Notes |
|---|---|---|
| `dev-a` | 35 | UbiquotousPanda's own work. **60 % of all issues.** |
| `dev-d` (Gaurav, scenarios + notification-router) | 10 | All 10 are notification-router + scenarios. |
| `dev-c` (Khushi, incident-classifier) | 7 | RA-002 + 5-case golden + UI wiring. |
| `dev-b` | **0** | **No issues labeled for dev-b.** |
| `demo-readiness` (DEMO-N suffix) | 16 | **All opened 2026-05-14 — the day before the demo.** Only 2 closed. |

### 1.4 The "DEMO-N" pattern (eve-of-demo discovery)

Issues #54 → #68 carry the `demo-readiness` label and were all opened on 2026-05-14. They map to the team's last-day audit of the repo:

- **2 closed same-day** (#53, #54): live CMDB fallback + LLM gateway health-ping. Real bugs found, fixed, merged via PR #69.
- **14 still open the night before the demo.** Most are medium/low priority hygiene (e.g. #66 docstring drift, #67 FastAPI lifespan migration, #68 server.py decomposition).

Read positively: this is *a deliberate flush of "post-demo backlog" issues into the tracker so they don't get forgotten*. Read negatively: it's a wave of last-mile work the team is consciously **not** finishing — and the audience won't know which is which.

---

## 2. Four new findings the issue data surfaces

### 2.1 The team is one person at the issue level — but four people at the PR level

58/58 issues opened by one user. **Three other engineers ship PRs but never open issues.** Possible reasons:

- Issues are a planning artifact for one person; the others get scope verbally and ship.
- Or: the team has not adopted the issue tracker; tickets live in chat or in a head somewhere.
- Or: dev-b is disengaged and the other two are heads-down on Patil-Khushi (RA-002) and Gaurav (RA-005 + scenarios).

Whichever it is, the **review trail for what's shipping is one person's** — UbiquotousPanda authored 8 of the 11 merged PRs, and the other 3 PRs (RA-002, RA-005, the alert branch) appear to have been merged without independent review listed. The original retro §1.2 called this "parallelize before any one agent is end-to-end". The issue data says it's **also a single-reviewer bus-factor risk**.

**This is the first retro's biggest blind spot.** A retrospective written from memory naturally focuses on architectural choices; the tracker shows the team-shape choice that enabled (or caused) those architectural choices.

### 2.2 Dev-b has no labeled work, and there is no `dev-b` label on any issue

`gh issue list` finds zero issues with `dev-b`. The team's labels (`dev-a`, `dev-c`, `dev-d`) imply a `dev-b` slot. PR authors include `shrvri` but no PR from `shrvri` appears in the 11-PR merge log. **Either dev-b is reserved-but-unstaffed, or dev-b has done un-trackable work.** Surfacing this matters because the existing retro's §1.2 ("parallelize before end-to-end") assumes 4 productive devs; if dev-b is silent, the parallelization wasn't even 4-wide — it was 3 wide. That's a different mistake.

**Action item:** before Phase 2 kickoff, get an answer to "what was/is dev-b building?" If the answer is "nothing", reassign Phase 2 work assuming 3 devs, not 4.

### 2.3 The original roadmap and the tracker disagree on what Phase 1 contained

The 12-week roadmap (CLAUDE.md + onboarding doc) named **Log Correlation v1** as a Phase 1 (W3-5) deliverable. The issue tracker tells a different story:

| Phase 1 roadmap agent | Issue # | State | Priority |
|---|---|---|---|
| Alert Triage | (A1-A12 closed) | ✅ shipped | high |
| Auto-Ticketing | #34 | ✅ closed via PR #46 | high |
| Notification Router | #35 + D1-D5 | ✅ closed via PR #47 | high |
| **Log Correlation** | **#36** | **OPEN** | **medium** |
| Eval harness | (#7 closed) | ✅ shipped | low (!) |
| Incident Commander (SRE) | #38 | OPEN | medium |
| RCA Agent ★ | #37 | OPEN | high |

Two things this surfaces:

1. **Log Correlation was actively de-prioritised** from "Phase 1 deliverable" (the roadmap) to "open, medium" (the tracker). It wasn't forgotten — it was a deliberate, undocumented re-scope. The first retro's §1.1 ("we sold Log Correlation without provisioning its backend") read the omission as architectural blindness. The tracker says it was more like *quiet attrition*: someone made a call to defer it, didn't update the roadmap, and the rest of the team accepted it.
2. **The eval harness was tagged `priority:low`** (issue #7), but CLAUDE.md principle #7 says "evaluation harness from day one". The tracker priority and the architectural principle don't agree. That's exactly how an aspirational principle stops being load-bearing in practice.

**This means the answer to "why didn't we ship Log Correlation" isn't a code or architecture answer — it's a planning-discipline answer.** The roadmap stopped being the source of truth somewhere between W0 and W3, and nobody updated it.

### 2.4 Open-issue inventory tells you what the team would build with one more sprint

The 23 open issues are not random. Group them and a pattern emerges:

| Bucket | Issue count | What they imply |
|---|---|---|
| **Real Phase 1 gaps** (Log Correlation #36, Incident Commander #38, RCA #37) | 3 | The agents the roadmap said we'd ship but didn't. Headline gaps. |
| **ITSM hardening** (#10 service account, #43 password rotation) | 2 | The branch name says "real PDI end-to-end" but operational hygiene is open. |
| **Scenarios / truth files** (#25, #26, #27, #52, #64) | 5 | Source-of-truth consolidation. The original retro §2.3 nailed this. |
| **Demo bring-up hygiene** (#56 -Fresh, #62 classifier-ui build, #57 dashboard clarity, #63 .env.example) | 4 | DX work that bites on a clean machine. Maps onto v2 audit's "diagnose before fixing" rule. |
| **Doc + structure drift** (#65 Phase-0→1 refresh, #66 docstring, #67 FastAPI lifespan, #68 server.py decomposition) | 4 | Pure tech debt. Honest "post-POC backlog" item. |
| **Demo polish** (#55, #58, #59, #60, #61) | 5 | One-PR-each polish. None block. |

**Read of the open queue:** ~10 of the 23 open issues are real Phase-1-completion work (gaps + hardening + scenarios). The other ~13 are post-demo backlog. The team's *immediate* fix list is half what the open count suggests.

---

## 3. Two corrections to the first retrospective

### 3.1 §1.1 (Log Correlation) — closer to "quiet attrition" than "architectural blindness"

The first retro framed Log Correlation's absence as an architecture mistake — *we sold a thing whose backend we didn't deploy*. The issue tracker shows it's more nuanced: **the team did the right architectural thing for the agents they actually built** (Loki/Tempo aren't needed for Triage/Classify/Ticket/Notify), and the Log Correlation work was *re-prioritised mid-flight*, not architecturally botched. Issue #36 exists, has the right scope, and is appropriately labeled `priority:medium`. The mistake isn't "we didn't see this coming" — it's "we saw it, demoted it, and didn't tell the roadmap."

The correction matters for Phase 2: the fix is **a roadmap update rhythm**, not a Loki/Tempo install. (Though Loki/Tempo still need to land — see §5.)

### 3.2 §1.2 (parallelisation) — undersells the team-shape signal

The first retro said "we let Phase 1 work parallelize before any one agent was end-to-end." True. The tracker says something sharper: **work was parallelised across people who never opened issues against each other or against UbiquotousPanda's work.** That's the real reason the per-agent code shape diverged (no `__main__.py` in RA-003, different function names in RA-005) — no one was reviewing for consistency at the *agent shape* level because there was no agent-shape ticket against which to review. The retro called the symptom; the tracker reveals the mechanism.

---

## 4. The uncomfortable team-shape signal

Strip away the architecture and read the data flat: **one person planned, opened tickets for, code-reviewed, and merged 60 % of the work**, with three other contributors building specific agents in narrow lanes. That's not a team running a 12-week POC — that's an architect-developer with three contractors.

For a 4-person POC, this is functional but fragile. The risk it carries forward to Phase 2:

- **Velocity caps at one person's bandwidth.** Phase 2's RCA Agent is the headline differentiator. If RCA gets built the way RA-001 was built (architect-developer ships v1, three others ship adjacent work), it will be one person's accuracy bar, one person's prompt, one person's eval set. That's the highest-stakes single-author piece of code in the POC.
- **Knowledge concentration.** When dev-a is OOO or sick during demo week, no one else has the muscle memory for `start.ps1`, the kuberlr trap, `AIOPS_USE_MOCK_ITSM`, the dashboard build, or the chatops audit log. The CLAUDE.md gotcha list isn't documentation — it's one person's lived memory.
- **Issue-tracker = personal todo list, not team contract.** If the team treats the tracker as "the architect's notes", then commitments live in chat and the tracker's metrics (closure rate, priority distribution) are decorative. That's how planning discipline erodes.

**This is the finding that wouldn't have surfaced without the tracker data**, and the one most worth acting on. The fix isn't process for its own sake; it's distributing the load so dev-a isn't on every code-path at every stage.

---

## 5. Forward fix plan — keyed to open issues

Each row maps a problem to an existing open issue (or a new one to file). Phase 2 should start by triaging this list, **not** by writing a new plan.

### 5.1 Demo-eve work that should actually close before the demo

| # | Issue | Why it matters tomorrow |
|---|---|---|
| 1 | #62 (start.ps1 builds classifier-ui) | If the classifier dashboard 503s mid-demo, the audience sees a broken UI. **Verify on a clean checkout tonight.** |
| 2 | #56 (-Fresh flag clears scenarios + state.db + chatops.jsonl) | Stale state from rehearsals is the most common demo failure mode. Even without this flag, run `inject.py --clear && rm demo/chatops.jsonl` before bring-up. |
| 3 | #57 (dashboard "Active scenarios vs Active alerts" labels) | The dashboard's stat cards say one thing; the data means another. Confusion sink during the live walkthrough. |

### 5.2 Phase 2 week 1 — close the real Phase 1 gaps

| # | Issue | Action | Tracker home |
|---|---|---|---|
| 4 | **Log Correlation backend** | Stand up Loki + Tempo via side charts in `infra/`. Cut `image-provider`, `fraud-detection` from the OTel demo to free ~600 Mi. Trade-off documented in chart values. | New issue: `[INFRA-1] Deploy Loki + Tempo, cut non-essential OTel-demo services` |
| 5 | #36 (Log Correlation agent) | Promote to `priority:high`. Build against the now-deployed Loki/Tempo. Eval against the existing 3 truth files. | #36, escalate label. |
| 6 | #37 (RCA Agent ★) | Decision: scope v0 to **single-scenario, single-prompt** RCA on `slow-product-catalog`. Real eval cases. Required-HITL gate exercised live. Do NOT try to ship a generalised RCA. | #37, scope-cut comment. |
| 7 | #38 (Incident Commander) | De-scope from Phase 2; move to Phase 3 candidates. Add label `phase:post-poc`. | #38, label + comment. |
| 8 | #25/#26/#52/#64 (scenarios single source of truth) | These are the *same problem* split across four issues. Close 3 as dup, finish one. | dedup + close. |

### 5.3 Phase 2 week 1 — close the team-shape gaps

| # | Action | Tracker home |
|---|---|---|
| 9 | Assign dev-b real work or formally drop the slot. | New issue: `[TEAM-1] Reconcile dev-b assignment for Phase 2`. |
| 10 | Adopt the issue tracker as the **only** source of work. Every PR references an issue; every issue has an assignee that isn't always dev-a. | New issue: `[TEAM-2] Lightweight issue-discipline contract for Phase 2`. |
| 11 | Distribute the RCA Agent build: prompt + eval = one person, integration into the orchestrator = another, HITL gate exercise = a third. Single-author RCA is the biggest concentration risk. | Encode this in #37's task list. |
| 12 | Add a weekly "roadmap-versus-tracker" 15-minute review. If `priority:high` Phase-N items are still open at the end of the phase, decide explicitly: ship, defer, or cut. **Do not let them drift unlabeled.** | Calendar item, not an issue. |

### 5.4 Phase 2 week 1 — close the hygiene gaps that v2 audit and the open queue both surface

| # | Issue | Action |
|---|---|---|
| 13 | #10 + #43 (ServiceNow least-privilege migration) | Finish the service-account swap. Rotate the admin password the same day. |
| 14 | #63 (.env.example overhaul) | Add `AIOPS_USE_MOCK_ITSM`, `AIOPS_PROMETHEUS_URL`, `AIOPS_JAEGER_URL`, `AIOPS_JAEGER_API_PREFIX`, Azure block. Mark the file as the contract. |
| 15 | New: eval harness must consume truth files | Resolves first retro §2.3 + this retro §2.3. One-day fix in week 2 of Phase 2. |
| 16 | P2 from v2 audit: chart values for the cluster | New issue: `[INFRA-2] infra/values/otel-demo-poc.yaml: bump accounting, cut non-essential pods`. |
| 17 | First retro §1.5 (premature React dashboard) | Don't unwind. But **freeze** dashboard scope in Phase 2 — every dashboard PR needs a one-line justification against a Phase-2 agent demo. |
| 18 | First retro §1.6 (Planner/Router/Orchestrator/Memory stubs missing) | Land `aiops/runtime/orchestrator.py` with a v0 Reactive flow function. The other three remain `# Phase 3`. |

---

## 6. What I'd change about how we run the tracker

This is the meta-fix and is the only thing here that's free.

1. **Every commit references an issue.** Today, 11 PRs map cleanly; the remainder of the work (the dashboard scope creep, the eval harness wiring) has no tracker artifact. If it was worth doing, it was worth a one-line issue first.
2. **Issues get assignees that aren't always `UbiquotousPanda`.** This is the single change with the highest leverage. It forces the team-shape problem to the surface every time the architect tries to take everything.
3. **The roadmap is updated in the same PR that re-scopes work.** Log Correlation should have gone from "P1 deliverable" to "P2 carry-over" *in the PR that decided that*, not after the fact in a retro.
4. **Demo-eve audits become a recurring item, not a one-shot.** The 16 DEMO-N issues opened today suggest the team has a working method for this. Make it a calendared sweep two weeks out from every customer demo, not the night before.
5. **`priority:` labels mean something.** Today, `priority:low` includes #7 (the eval harness — a CLAUDE.md non-negotiable) and `priority:high` includes #54 (LLM gateway health-ping — useful, not architectural). The label is currently aspirational; tighten it.

---

## 7. The single sentence I want next phase's lead to read

> Phase 1 shipped what one person could review in three days; Phase 2's RCA Agent is what the four of us have to build together, and the issue tracker is the only way I'll know if that actually happened.

---

*End. This document is a deliverable. Quote it freely. Specifically: quote §4 if asked about team shape, quote §5 if asked about Phase 2 scope, and quote the first retrospective's §6 ("the one thing I'd undo") if asked what the deepest single mistake was.*
