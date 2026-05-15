# Architect's retrospective — Phase 1, as it stands the night before the demo

**Author's hat:** the solution architect who scoped this in [Adaptive_AIOps_Solution_Design.pptx](Adaptive_AIOps_Solution_Design.pptx) and signed CLAUDE.md.
**Audience:** the same person — me, rereading this in three weeks when the POC review starts.
**Tone:** honest, not generous. If I write this kindly I'll repeat the mistakes.

---

## 0. What Phase 1 was supposed to deliver vs what we land tomorrow

Per the 12-week roadmap in CLAUDE.md, the **Phase 1 — Reactive backbone (W3–5)** deliverable was:

> Alert Triage v1, Auto-Ticketing v1 (ServiceNow PDI), Notification Router v1 (Slack), **Log Correlation v1**, eval harness, demo UI v0. First internal demo.

What we will actually demo tomorrow:

- ✅ Alert Triage v1 — landed, seam-clean, evals pass against mock.
- ✅ Auto-Ticketing v1 — landed, hits live PDI.
- ⚠️ Notification Router v1 — landed, but writes to a **JSON file**, not Slack. The Slack integration the design slide promised does not exist.
- ❌ **Log Correlation v1 — does not exist on any branch.** The agent the roadmap named is missing entirely. No Loki, no Tempo, no agent.
- ✅ Eval harness — scaffolded, but with a hole that took until v2 of the audit to surface (see §2.4).
- ⚠️ Demo UI v0 — landed, but as a React/Vite/Tailwind app, not "v0". Phase 4 work pulled forward.
- ➕ Incident Classifier — landed as a `NotImplementedError` stub. Not on the Phase 1 roadmap at all.

So Phase 1 missed one of the five named agents, over-built one deliverable, and under-built another to the point of false advertising. That's the headline. Now the why.

---

## 1. Strategic mistakes — things that were architectural choices, not execution slips

### 1.1 We sold "Log Correlation" without provisioning its backend

This is the deepest mistake. Log Correlation's value proposition is correlating logs across traces — it has no meaning without Loki + Tempo. CLAUDE.md and the solution-design pptx **both** list "Prometheus / Loki / Tempo + Grafana" as the stack. The deployed cluster has Prometheus + Jaeger only.

Two valid paths existed:

- (a) Stand up Loki + Tempo in Phase 0 (Helm side-charts, ~30 min on Rancher Desktop). Then Log Correlation has a target to write against.
- (b) Be honest in Phase 0 that Loki/Tempo are out of POC scope, and replace Log Correlation in the Phase 1 list with something the stack can support — e.g. **Trace Correlation** against Jaeger (which the stack actually has).

We took neither path. We left the docs aspirational, didn't add the backends, and didn't build the agent. The bill comes due tomorrow when someone asks "where's the log correlation step?"

**What I should have done:** at the Phase 0 → Phase 1 boundary, **audit the stack against the roadmap** and either deploy what was missing or change the roadmap. A monthly "does the deployed cluster still match the design slide?" check should be a calendar item, not a vibe.

### 1.2 We let Phase-1 work parallelize before any one agent was end-to-end

Branch list says it all: `feat/ra-003-auto-ticketing-agent`, `feat/ra-003-itsm-servicenow`, `feature/notification-router`, `feat/KP/incident-classification`, `feature/alert`. Four people (or four sessions) built four agents in parallel before any one of them was actually walked through end-to-end in a demo.

The consequence is visible in the codebase: every agent reinvents the layout. There is no `agents/_template/`. RA-001 grew evals + a `__main__.py` + a README idiom; RA-002 copied the idiom partially; RA-003 has no `__main__.py` at all. RA-005 has a different `decide()` / `route()` split than RA-001's `triage()`. The platform seams hold (good) but the per-agent shape is divergent. **Future-me will pay the consolidation tax.**

**What I should have done:** insist Alert Triage went **all the way through** — agent + golden set + truth file + demo UI tile + end-to-end CLI invocation in a script — before merging anyone else's agent branch. Then template it. Then parallelize.

### 1.3 We have two sources of CMDB truth and the eval gold set was written against the wrong one

The repo has both a mock CMDB ([aiops/tools/itsm/_demo_cmdb.py](aiops/tools/itsm/_demo_cmdb.py)) and a live ServiceNow PDI integration, with `AIOPS_USE_MOCK_ITSM` as the toggle. The golden eval cases for Alert Triage were authored against the mock. The live PDI has different (sparser) data. **Flipping a single env var silently breaks evals.** That is the v2 audit's biggest finding, and the reason Plan C exists.

This is a classic two-sources-of-truth anti-pattern. We should have decided up-front:

- **Either**: ship a seed script in `infra/` that populates the PDI's `cmdb_ci_*` tables to match the mock CMDB at bring-up time. One source of truth, materialised in two places.
- **Or**: ship only the mock for the POC and document live-PDI integration as an explicit Phase 2 item.

We did neither. We have both, with no reconciliation, and the golden set was written under an unstated assumption.

**What I should have done:** the moment the live PDI integration shipped, run the golden set against it, see the failures, and either seed or scope-cut. Not three weeks later under demo pressure.

### 1.4 We chose to demo against a live external system without an SLO for that system

ServiceNow PDIs sleep after inactivity, expire passwords, reset weekly, and occasionally just go down. We anchored the headline integration in Auto-Ticketing on a system we don't run. There is no fallback path documented for "what if dev195902 is unreachable Friday at 10:30 am during the demo". `AIOPS_USE_MOCK_ITSM=true` is the implicit fallback — but it's not in any runbook, and switching it requires re-reading the env-loading code to know if a restart is needed.

**What I should have done:** treat the PDI as an external dependency with a documented failure mode. The .env file's own comment about issue #43 ("admin password set quirk") is six weeks old. Either fix it or formally accept the risk.

### 1.5 We built a polished React dashboard before two agents composed

[demo/dashboard/](../demo/dashboard/) is React + Vite + Tailwind. CLAUDE.md says Phase 1 is "demo UI v0" and Phase 4 is "polish". We pulled Phase 4 forward by ~7 weeks. The cost: every agent change now also touches dashboard wiring; the npm build is on the critical path of `start.ps1`; first-time setup is "if npm is missing, /dashboard 503s". On a 16 GiB laptop, this is real friction.

This is the silent-killer scope creep that CLAUDE.md literally warns about. It happened anyway because "let's just make it look nice for the demo" is hard to argue against in the moment.

**What I should have done:** ship demo UI v0 as a static HTML page that lists agents and links to their CLI commands, period. The React dashboard is a Phase 4 deliverable; tomorrow we'll be glad we have it but six weeks from now we'll wish we hadn't.

### 1.6 We sold an "agentic runtime" we haven't built

The architecture pptx names six runtime components: Planner, Router, Orchestrator, Memory, Tool Registry, Eval Harness. In the code we have **two** — Tool Registry and Eval Harness. The Reactive flow tomorrow will run by **the human typing four commands in sequence**, not by an orchestrator.

For a Phase 1 demo this is *fine*. But the slide remains in the deck, and every time we present the slide we accrue debt. Either we mark four boxes "post-POC" on the slide tomorrow, or we accept that everyone leaves the room thinking we have a Planner.

**What I should have done:** the day we built the Tool Registry, also commit a `aiops/runtime/orchestrator.py` stub with a docstring saying "Phase 2". The seam matters more than the implementation; an absent seam is what makes future work hard.

### 1.7 Incident Classifier got merged as a stub

RA-002 raises `NotImplementedError`. It has a golden file that's empty. It was merged. The merge happened because someone needed a placeholder to keep the demo narrative continuous between Triage and Ticketing.

This violates CLAUDE.md principle #7 ("when you build an agent, build its eval set the same week") *and* the demo guidance ("end-to-end ugly first, refactor second"). A stub that throws at runtime is *not* end-to-end; it's a hole with a label on it.

**What I should have done:** either land Incident Classifier v1 cheaply (a one-shot LLM prompt with three fixtures would have done it), or **leave it out of the agents/ tree** and put it in a `docs/future_agents/` folder with the contract documented. Half-built code in `agents/` is misleading to teammates and to me three weeks from now.

---

## 2. Tactical mistakes — cheaper individually, but they accumulate

### 2.1 Two ways to bring up port-forwards

[start.ps1](../start.ps1) (correct, robust on Windows) and [infra/port-forward.ps1](../infra/port-forward.ps1) (silently bites the kuberlr wrapper inside `Start-Job`). Both are committed. New devs find the second one first because it lives in `infra/`. The fix is one of two lines: either fold the script into `start.ps1`'s logic, or delete it. We did neither.

### 2.2 Live secrets in `.env` with no rotation plan

AZURE_OPENAI_API_KEY and a ServiceNow admin password sit in plaintext in a file the user opens in their IDE during normal work. `.env` is gitignored — that's the *only* control. There's no `direnv` indirection, no `.env.local` convention for secrets-only, no rotation cadence. Every machine on the team has the key. When (not if) a screen-share leaks it, we will scramble.

### 2.3 The eval harness doesn't actually consume truth files

CLAUDE.md principle #8: "Truth files for every demo scenario… so the eval harness has ground truth." Reality: `evals/harness.py` reads `agents/<name>/evals/golden.json`. It does **not** read `demo/truth_files/*.yaml`. Truth files are human documentation, not test fixtures. The principle is aspirational; the wiring isn't there.

This bit us in v2: the truth files said one thing about what a "good" RCA looks like; the golden set was written independently against the mock CMDB. There's no automated check that they agree.

### 2.4 We never ran the eval harness in v1 of the audit

This is on me as the auditor, not the architect — but it's the same person. v1 was "read-only" and v1 missed the 5/8 pass rate. The lesson is in v2 §8: a read-only audit that doesn't include *one* end-to-end seam probe is paper, not audit. I should have run the harness in v1.

### 2.5 The cluster is over-committed on memory and nobody owns the chart values

7258 Mi committed against 7850 Mi capacity = 94 %. `accounting` OOMKills periodically. The chart's defaults are sized for cloud, not a 16 GiB laptop. There's no `infra/values/otel-demo-poc.yaml` that tunes pod limits or disables services we don't demo (image-provider, fraud-detection, kafka). We accepted upstream defaults; upstream defaults are wrong for our host.

### 2.6 The 30-agent catalog exists before any one agent is "done done"

[Adaptive_AIOps_Agent_Catalog.xlsx](Adaptive_AIOps_Agent_Catalog.xlsx) has 30 agents with KPIs, HITL levels, and integration matrices. CLAUDE.md explicitly says "do not build all 30". But the catalog still anchors every conversation. The expectation gap — 30 named, 4 built, 1 of those a stub — is the most common question after every demo. We invited the question by making the catalog before the agents.

### 2.7 HITL is enforced by a gate that's never triggered in the demo

All four shipped agents are autonomy level None or Optional-with-flag-off. Tomorrow's demo will pass through `aiops/policy/get_gate().enforce(...)` exactly zero times in any blocking way. We will be asked "what does HITL actually look like?" and the honest answer is "we'll show it when RCA lands, which we haven't started". This is the second-biggest expectation gap after the missing agents.

---

## 3. What we got right (worth not over-correcting)

- **The seams hold.** No agent imports `anthropic` or `openai` directly. No agent calls `httpx.post('https://servicenow.com/...')`. The platform principle #1 ("vendor-neutral by default") is the one thing the codebase enforces structurally. Every other principle is aspirational; this one is real. **Do not regress this.**
- **Truth files exist for every scenario.** Three for three. Even though the harness doesn't consume them automatically, they're the right artifact and they're in the right place. Closing the loop in §2.3 is mechanical.
- **Eval harness scaffolding is right.** It runs per-agent, it produces JSON, it has a `--ci --min-pass-rate` gate. The hole in it (no truth-file consumption) is a one-day fix in Phase 2.
- **`start.ps1` is the right shape.** A long-lived window with port-forwards as background jobs, with explicit handling of the kuberlr trap, is the correct Windows + Rancher Desktop pattern. Keep it.

---

## 4. What this means for Phase 2 — the architect's decisions before Monday

In priority order, with explicit verdicts:

1. **Log Correlation v1 in Phase 2 requires Loki + Tempo to be deployed.** Verdict: **add Loki + Tempo to `infra/bootstrap.ps1` in week 1 of Phase 2.** Not negotiable. If we can't fit them in the cluster's memory, cut services from the OTel demo (image-provider, fraud-detection are non-essential to the narrative).
2. **One source of CMDB truth.** Verdict: **seed-script approach.** Ship `infra/seed_servicenow_cmdb.py` that writes the mock CMDB's contents into the PDI on bring-up. Mock stays as fast-path; live PDI gets materialised. Eval gold set is written against the mock and works against the PDI by construction.
3. **Incident Classifier v1 or removal.** Verdict: **remove from `agents/` until v1 is real.** Move scaffolding to a feature branch. The demo narrative skips classification until v1 lands.
4. **Notification Router → Slack.** Verdict: **week 1 of Phase 2.** Either real Slack via webhook, or formally cut "Slack" from the agent description. The jsonfile adapter stays as the default for tests.
5. **Orchestrator stub.** Verdict: **commit `aiops/runtime/orchestrator.py` with a v0 that runs the Reactive flow** (Triage → Classify → Ticket → Notify) as a single CLI invocation. Even if it's literally four function calls in a for-loop, the seam matters more than the impl.
6. **Truth files become eval inputs.** Verdict: **week 2 of Phase 2.** Add `evals/harness.py` support for reading `demo/truth_files/<scenario>.yaml` and matching agent output against `expected_*` fields.
7. **HITL demo scenario.** Verdict: **build one Phase 2 demo case where an action requires approval and the platform blocks it until approved.** Auto-Healer-lite on a sandbox kubectl rollout would suffice. Without this, the HITL story is purely architectural.
8. **Cut the catalog or rebrand it.** Verdict: **add a banner to the xlsx README sheet: "30 agents = product vision; 4 built in POC."** Stop letting the catalog set audience expectations the code can't meet.

The architect's hard rule from CLAUDE.md ("scope creep is the silent killer") cuts both ways: it should kill *new* scope, but it should also kill *old* scope that we haven't delivered on. Pruning is harder than adding. Phase 2 starts with prune.

---

## 5. The honest version of the demo narrative for tomorrow

Given everything above, here is what I'd tell the audience if I were the architect speaking — not the engineer demoing:

> "What you'll see today is the Reactive backbone of a much larger platform. We've built four of the eight Reactive-phase agents that the design calls for: Alert Triage, Auto-Ticketing, Notification Router, and a scaffolded Incident Classifier that we'll fill in next sprint. The seams underneath — vendor-neutral LLM, tool registry, HITL gate — are deliberately bigger than what these four agents need, because they're what the next 26 agents will plug into. We deferred Log Correlation, the RCA Agent, and the SRE-specific agents to later phases — that's where the headline differentiator lives, and we wanted the foundation right first."

That's an honest opener that sets expectations the demo can actually meet. It also pre-empts the three questions the gaps would otherwise generate ("where's RCA?", "what about logs?", "is the classifier real?").

---

## 6. The one thing I'd undo if I could

Of everything in §1, the single decision I'd reverse if rewinding six weeks: **stand up Loki and Tempo in Phase 0**. Not because tomorrow's demo needs them — it doesn't — but because their absence is what makes Log Correlation, the Knowledge Synthesizer, the RCA Agent, and half the Proactive-phase agents *un-buildable* without backfill. We didn't just skip an agent; we removed the foundation the next six agents need. Loki + Tempo is ~30 min of Helm install and ~1 GiB of memory we could find by cutting `image-provider` and `fraud-detection`. It would have been cheap then, and it's the same cost now — but now we're behind, not ahead.

---

*End. This file is not a deliverable. It's a private letter. If anyone other than me reads it, please don't quote §1 at me in a meeting — quote §4 instead, because that's what I'm doing about it.*
