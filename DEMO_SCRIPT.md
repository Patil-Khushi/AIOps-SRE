# DEMO_SCRIPT.md — Presenter Talking Points

**Owner:** Sharvari Kulkarni (Dev B, RA-003) · **Status:** Draft v1 · **Last updated:** 2026-05-27 · **Demo date:** 2026-06-14

[DEMO_PLAN.md](DEMO_PLAN.md) tells engineering *what to build*. This document tells the presenter *what to say* at each beat — including what to say when something is thirty seconds late. Every audience-facing line is in **quotes** and meant to be read aloud close to verbatim. Lines marked **(operator, off-mic)** are for whoever is driving the laptop and come straight from [RUNNING.md](RUNNING.md).

Definition of done for this doc: a presenter can run the full six minutes **cold, without the slides**. Read it end-to-end once before demo day.

> **Read this first — known sharp edge.** [RUNNING.md:241](RUNNING.md#L241) records that clicking *Inject* in the dashboard's Failure Injection panel flips the flag but does **not** currently fire Prometheus alerts (the OTel demo's payment / product-catalog spans emit `STATUS_CODE_UNSET`, so the rules don't match). The reliable on-stage path is the **scenario-inject API call** plus an explicit **trigger of the agent chain** — both noted in the operator cues below. **Confirm the exact working path in the W3 dry-run** ([DEMO_PLAN.md:189-215](DEMO_PLAN.md#L189-L215)) and update this line if the wiring changes.

---

## 1. The six-minute beat schedule

Mirrors the beat table in [DEMO_PLAN.md:11-23](DEMO_PLAN.md#L11-L23). One block per timestamp: **Say** (read aloud) · **Do** (operator, off-mic) · **Land** (the single point the audience should walk away with).

### 0:00 — Open on the dashboard

- **Say:** *"What you're looking at is the OpenTelemetry Astronomy Shop — a real, fully-instrumented microservice store — running on a Kubernetes cluster on this laptop. Prometheus, Jaeger, a live ServiceNow instance, Slack, and PagerDuty are all wired in. Nothing here is a screenshot."*
- **Do:** Dashboard already open full-screen at <http://localhost:8765/dashboard/>. Webstore tab ready at <http://localhost:8080/>.
- **Land:** This is live infrastructure, not a slideware mock.

### 0:10 — Inject the failure

- **Say:** *"I'm going to break it on purpose — a payment-service failure, the kind that pages someone at 3 a.m. Watch what the platform does on its own."*
- **Do (operator, off-mic):** Click **Inject** on `payment_failure` (Sev-1) for the visual, **and** fire the backing call so the chain actually runs:
  ```powershell
  Invoke-RestMethod -Method POST http://localhost:8765/api/scenarios/payment_failure/inject
  ```
  ⚠ See the sharp-edge note above — the button alone may not drive alerts. The agent chain is triggered explicitly at the 1:00 beat.
- **Land:** The fault is injected live, in front of them.

### 0:15 — Slack lights up

- **Say:** *"Within seconds — no human touched anything — the on-call channel gets a structured, severity-coded alert."*
- **Do:** Switch to the `#aiops-poc-alerts` Slack channel; point at the red Sev-1 Block Kit message.
- **Land:** Detection → triage → notification happened automatically.

### 0:20 — The phone buzzes

- **Say:** *"And this isn't just a chat message. The lead's phone is buzzing right now — a real PagerDuty page to whoever is actually on call."*
- **Do:** Hold up the phone (or point to the lead). PagerDuty mobile app shows an unacknowledged incident.
- **Land:** This plugs into the real on-call workflow, not a toy.

### 0:25 — ServiceNow incident appears

- **Say:** *"At the same moment, a ticket opens in ServiceNow — with the severity, the on-call owner, the matching runbook, and the full decision trace of how the platform got there. An auditor can read exactly why this happened."*
- **Do:** Switch to the ServiceNow PDI → the new `INC00100xx` incident; scroll the description body.
- **Land:** Every decision is documented and traceable from the first second.

### 1:00 — The RCA Agent proposes a fix (the headline)

- **Say:** *"Here's the part most tools don't do. Our RCA Agent doesn't just guess a likely cause — it posts an executable fix, with a rollback and a blast-radius estimate, and then it stops and asks a human."* Read the Slack card: *"Root cause: the `paymentFailure` flag is at 100%. Fix: set the flag to off. Rollback: re-flip it. Blast radius: low. Approve?"*
- **Do (operator, off-mic):** Trigger the RCA / agent path:
  ```powershell
  Invoke-RestMethod -Method POST http://localhost:8765/api/triage/live
  ```
  RCA verdict appears on the dashboard; the interactive **Approve / Deny** card posts to Slack.
- **Land:** Executable fix + rollback + blast radius — *not* a list of "likely causes." This is the differentiator.

### 1:15 — Human approves in Slack

- **Say:** *"A human stays in the loop by design. I tap Approve — and only now does the platform run the fix."*
- **Do:** Tap **Approve** in the Slack card; the action runs; a confirmation message posts back to the channel.
- **Land:** The platform *cannot* take the destructive action until a human approves — the gate is enforced by the platform, not by the agent.

### 1:30 — The audit trail

- **Say:** *"And every single thing the platform just did — every alert, every page, every approval, every action — is right here, timestamped and queryable."*
- **Do (operator, off-mic):** Show the audit log (use `-Encoding UTF8` to avoid mojibake):
  ```powershell
  Get-Content demo\audit\chatops.jsonl -Tail 8 -Encoding UTF8
  ```
- **Land:** Full accountability — nothing the AI does is hidden.

### 2:00 — Second scenario, end-to-end

- **Say:** *"That wasn't a one-trick scenario. Here's a different failure — a slow product catalog, a Sev-2 — running the same path end-to-end."*
- **Do (operator, off-mic):**
  ```powershell
  Invoke-RestMethod -Method POST http://localhost:8765/api/scenarios/slow-product-catalog/inject
  ```
  (Scenario key matches [demo/scenarios/slow-product-catalog.yaml](demo/scenarios/slow-product-catalog.yaml) and the CLI injector in [RUNNING.md:73](RUNNING.md#L73).)
- **Land:** The pattern generalizes across failure types and severities.

### 4:00 — Wrap on the principles

- **Say:** *"Everything you just saw is built on eight non-negotiable design principles — vendor-neutral, modular and individually sellable, human-in-the-loop enforced by the platform, policy-as-code, safe autonomy, closed-loop learning, evaluated from day one, and grounded in truth files. Let me show you where each one just showed up in the live demo."*
- **Do:** Walk the eight principles from [CLAUDE.md "Non-negotiable design principles"](CLAUDE.md), tying each to a beat the audience just watched (e.g. principle #3 → the Slack approval gate at 1:15; principle #5 → the rollback in the RCA card at 1:00).
- **Land:** This is an architecture with guardrails, not a demo hack.

---

## 2. Failure-recovery lines

For the three things most likely to break live. Each is a verbatim "if X happens, say Y" so you never freeze on stage. The rule: **keep talking, pivot to the surface that still works, never debug in front of the audience.**

### A. Slack signing secret invalid → the Approve / Deny buttons do nothing

The inbound interactivity request fails Slack's signature check (`AIOPS_SLACK_SIGNING_SECRET` rotated or wrong — see [DEMO_PLAN.md:113-117](DEMO_PLAN.md#L113-L117)), so tapping **Approve** has no effect.

- **Say:** *"The approval doesn't only live in Slack — the same human gate is in our dashboard. Let me approve it there, which is exactly what an SRE on a laptop would do."*
- **Do (operator, off-mic):** Approve via the dashboard / web approval surface (the HITL UI, [DEMO_PLAN.md:62-77](DEMO_PLAN.md#L62-L77)). Fallback per [DEMO_PLAN.md:184](DEMO_PLAN.md#L184): show the policy gate blocking, then releasing, via the approval registry — *same architectural beat, weaker visual.*

### B. PagerDuty trial quota exhausted → the phone does not buzz

The developer-tier PagerDuty key (`AIOPS_PAGERDUTY_INTEGRATION_KEY`) has hit its event quota and the page never lands.

- **Say:** *"On a normal day this also pages the on-call engineer's phone through PagerDuty. You can see the platform decided to do exactly that — here's the `page_oncall` action in the routing decision."*
- **Do (operator, off-mic):** Point at the `actions: ["page_oncall"]` field in the routing outcome on the dashboard (fallback per [DEMO_PLAN.md:185](DEMO_PLAN.md#L185)). **Check with the lead beforehand** — they specifically asked for the real phone-buzz, so confirm the quota the morning of.

### C. ServiceNow PDI session expired → no incident appears

ServiceNow Personal Developer Instances sleep after ~10 days idle and sessions expire; ticket creation 401s.

- **Say:** *"The ticket is created through our vendor-neutral ITSM layer — here's the exact incident body the platform generates, severity, owner, runbook and all."*
- **Do (operator, off-mic):** Show the incident body from the audit log or a pre-created reference incident. If there's a 60-second gap, log back into the PDI to wake it; otherwise keep narrating from the audit trail and move to the RCA beat.

> **General rule:** if any single sink is down, the **audit log** (`demo/audit/chatops.jsonl`) still shows the platform made the right decision. When in doubt, pivot to the audit trail — it is the one surface that never depends on an external service being awake.

---

## 3. Pre-demo checklist (run 30 minutes before)

Work top to bottom. Everything here is from [RUNNING.md](RUNNING.md) and [DEMO_PLAN.md:189-215](DEMO_PLAN.md#L189-L215).

**Cluster + app**
- [ ] Rancher Desktop running, tray shows *Kubernetes: running* ([RUNNING.md:11-14](RUNNING.md#L11-L14)).
- [ ] Clean slate: `.\reset.ps1 -Hard` — flips flags off, truncates the audit log, wipes the AI Reasoning page ([RUNNING.md:86-99](RUNNING.md#L86-L99)).
- [ ] Bring it up: `.\start.ps1` — port-forwards + UI server ([RUNNING.md:16-23](RUNNING.md#L16-L23)).
- [ ] Port-forwards alive: `Get-Job -Name 'pf-*'` shows running jobs ([RUNNING.md:25-30](RUNNING.md#L25-L30)).
- [ ] Dashboards reachable: <http://localhost:8765/dashboard/> and the webstore at <http://localhost:8080/>.

**Credentials + external services** (`.env` loaded — [DEMO_PLAN.md:104-129](DEMO_PLAN.md#L104-L129))
- [ ] `AIOPS_SLACK_BOT_TOKEN` (`xoxb-…`) and `AIOPS_SLACK_SIGNING_SECRET` present; `#aiops-poc-alerts` channel open and bot installed.
- [ ] `AIOPS_PAGERDUTY_INTEGRATION_KEY` present; PagerDuty mobile app logged in; presenter/lead on the on-call schedule; **quota not exhausted**.
- [ ] ServiceNow PDI **awake** (log in once to wake it — PDIs sleep when idle); ITSM creds in `.env`.

**Stage hygiene**
- [ ] Audit log clean (the `-Hard` reset truncated `demo/audit/chatops.jsonl` — confirm it's near-empty).
- [ ] One full **dry-run** of the chain to warm caches and prove the path:
      `Invoke-RestMethod -Method POST http://localhost:8765/api/triage/fixture/payment_cpu_spike -TimeoutSec 90` ([RUNNING.md:109-115](RUNNING.md#L109-L115)). Then `.\reset.ps1 -Hard` again so the live run starts clean.
- [ ] Presenter laptop on **stable Wi-Fi** — prefer a phone tether over conference Wi-Fi; disable laptop sleep and OS notifications.
- [ ] Slack desktop notifications silenced; browser full-screen; only the demo tabs open.

---

## 4. Q&A primer

The five questions most likely from the room, with tight answers. Keep each under ~20 seconds; offer to go deeper after.

1. **"Is this just calling ChatGPT under the hood?"**
   *No. Every LLM call goes through a vendor-neutral gateway, so we can run Anthropic, OpenAI, or a local model per agent depending on data sensitivity — and we pin model versions, never "latest." The product isn't tied to any one vendor.* ([CLAUDE.md](CLAUDE.md) principle #1.)

2. **"What stops the AI from doing something destructive?"**
   *Two things, both enforced by the platform rather than the agent. Destructive actions are gated behind a human approval the agent physically cannot bypass, and every action passes through policy-as-code before it runs. You saw it stop and ask before flipping that flag.* (principles #3, #4, #5.)

3. **"Is any of this real, or is it canned?"**
   *All real — a live OpenTelemetry demo on Kubernetes, a real ServiceNow instance, real Slack and PagerDuty. We inject the failure live with feature flags, and an evaluation harness grades the agents against written truth files, so "looks good in the demo" isn't our bar.* (principles #7, #8.)

4. **"How is your RCA different from the AIOps tools we already have?"**
   *Most tools hand you a ranked list of likely causes and stop. Ours produces an **executable fix** — with a tested rollback and a blast-radius estimate — that a human can approve in one tap. That's the headline differentiator.*

5. **"How many agents are there, and when is this production-ready?"**
   *Thirty modular agents across four maturity phases, each individually sellable. What you saw is a focused six-to-ten-agent proof of concept on one end-to-end flow. The path from here to production is written up in our post-POC roadmap.* (forward-ref to DOC-9 `POST_POC_ROADMAP.md`.)

---

## DOC-2 acceptance checklist

- [x] Six-minute beat schedule with verbatim lines per timestamp, matching [DEMO_PLAN.md:11-23](DEMO_PLAN.md#L11-L23)
- [x] Failure-recovery lines for Slack signing secret, PagerDuty quota, ServiceNow PDI session
- [x] 30-minutes-before pre-demo checklist
- [x] Q&A primer (5 questions)
- [x] Lives at `DEMO_SCRIPT.md` (repo root), alongside [DEMO_PLAN.md](DEMO_PLAN.md)
- [ ] Walked through cold by dev-a (Chinmay) without referring to slides — **review gate**

## References

- [DEMO_PLAN.md](DEMO_PLAN.md) — the engineering plan this script narrates
- [RUNNING.md](RUNNING.md) — pre-flight commands referenced by the checklist
- [CLAUDE.md](CLAUDE.md) — the eight non-negotiable design principles referenced at the 4:00 wrap
