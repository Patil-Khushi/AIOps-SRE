# Risk Register

A trackable, living list of what could go wrong with the Adaptive AIOps + SRE Ops POC —
both on demo day and in the platform we're building toward. Unlike the one-slide risks
view in [`docs/Adaptive_AIOps_Solution_Design.pptx`](docs/Adaptive_AIOps_Solution_Design.pptx)
(section 8), this file is meant to be **re-read and updated**, not read once and forgotten.

## How to use this

- **Reviewed at every Monday standup.** Walk the table top to bottom; update the **Status**
  column and add a dated line to the [Review log](#review-log).
- **Re-read before every dry run / customer demo.** The Section A risks are the ones that
  bite during a live walkthrough — confirm each is still green before you present.
- **Owner is a person, not a team.** If a risk has no owner it has no one watching it.
- When a risk fires (or is permanently retired), don't delete the row — move its Status to
  `Mitigated`/`Accepted`/`Closed` and note why in the log. The history is the point.

**Legend** — Likelihood / Impact: `H` High · `M` Medium · `L` Low.
Status: `Open` (live, unmitigated) · `Mitigated` (control in place, residual risk low) ·
`Accepted` (acknowledged, no further action this phase) · `Closed` (no longer applicable).

Owners by stream: **Chinmay** (dev-a — platform, alert infra, cluster, LLM gateway, CI) ·
**Sharvari** (dev-b — docs) · **Khushi** (dev-c — RA-002, HITL UI) ·
**Gaurav** (dev-d — RA-005, Slack, scenarios, PagerDuty).

---

## Section A — Demo-day & POC operational risks

The immediate, trackable list. These are what fail a live 6-minute walkthrough. Re-check
before every dry run (target demo day **~2026-06-14**).

| Risk | Likelihood | Impact | Owner | Mitigation | Status |
|---|---|---|---|---|---|
| Slack signing-secret rotation breaks `/api/approvals/slack/callback` mid-demo | M | H | Gaurav | Pin the signing secret in `.env` and verify the callback in the dry run the morning of; keep the JSON-file chatops adapter as the default fallback so an approval can still be recorded if Slack is down. | Open |
| PagerDuty trial quota / developer account exhausts before demo day | M | M | Gaurav | Confirm trial quota and event-API limits the week before; have a second developer account ready; PD is not on the critical path of the Reactive walkthrough, so be ready to skip it. | Open |
| ServiceNow PDI session expires / instance sleeps during the walkthrough | H | H | Chinmay | PDIs sleep on inactivity and reset weekly — wake and log into the instance immediately before the demo; set `AIOPS_USE_MOCK_ITSM=true` as the documented fallback (restart the UI after flipping). Document the wake step in the demo runbook. | Open |
| Anthropic / LLM-provider rate limit (or 5xx) during the RCA / triage call | M | H | Chinmay | Pin model versions; cap calls per incident; pre-warm the path in the dry run; `aiops/llm` template/Tier-4 fallbacks keep the flow alive (degraded) if the API throttles. Surface `llm_ok` in `/api/health` before presenting. | Open |
| Real-LLM eval pass rate drops below the stubbed 1.0 number | M | M | Chinmay | Golden sets were authored against the mock CMDB; flipping to the live PDI changes the data and silently breaks evals. Run `evals.harness --ci --min-pass-rate 0.85` against the *actual* demo config the day before; seed the PDI CMDB or keep the mock for graded paths. | Open |
| Rancher Desktop VM runs out of memory on the presenter's laptop (cluster OOM) | H | H | Chinmay | Cluster sits ~94% committed on a 16 GiB host; `accounting` OOMKills periodically. Allocate ≥6 GiB to the VM; cut non-essential OTel-demo pods (`image-provider`, `fraud-detection`) via chart values; restart the cluster fresh before the demo. | Open |
| Wi-Fi flakes during the live demo (cloud LLM + PDI both need network) | M | H | Chinmay | Have a phone hotspot as backup; pre-record a fallback screen capture of the full flow; know which steps degrade gracefully offline (mock ITSM, template LLM summary). | Open |
| CI gating unreliable because the pytest suite hangs (#113) | L | M | Chinmay | Root cause was `Event.wait()` with no per-test timeout — **fixed and closed in #113**. Keep the per-test timeout in place; if a hang recurs, treat as a regression of #113. | Mitigated |
| Sharvari (dev-b) single-threaded on the DOC-* series while bug fixes pile up | M | M | Lead (Chinmay) | dev-b owns the full documentation backlog (#117–#126) alone; if a demo-blocking bug needs her, docs slip. Sequence docs by priority (RISK_REGISTER, THREAT_MODEL first); explicitly defer low-priority docs (openapi.json, model cards) past demo day if needed. | Open |

---

## Section B — Platform / production risks

Carried verbatim from the Solution Design risks slide (section 8). These are standing
architectural risks for the product, not demo-day issues. Status reflects current POC reality.

| Risk | Likelihood | Impact | Owner | Mitigation | Status |
|---|---|---|---|---|---|
| Hallucinated agent actions (incl. RCA fix steps) | M | H | Chinmay | RCA Agent requires HITL approval; policy-as-code gates; dry-run simulation; shadow eval before any promotion. *POC note: the HITL gate exists (`aiops/policy`) but is never exercised in a blocking way in the current demo — gap closes when RCA lands (Phase 2).* | Open |
| Vendor lock-in on the agent layer | M | H | Chinmay | Abstracted tool registry; ≥2 alternatives per integration; open contracts (MCP / REST+OpenAPI). *POC note: structurally enforced today — no agent imports a vendor SDK directly (smoke test gates it).* | Mitigated |
| Chaos experiment causes a real incident | L | H | Chinmay | Chaos Orchestrator uses blast-radius caps, safe-mode library, on-call approval, auto-abort on adverse signals. *POC note: Chaos Orchestrator is out of POC scope — accepted until Phase 4.* | Accepted |
| Data leakage via prompts / telemetry | L | H | Chinmay | PII-redaction guardrail, tenant isolation, secrets vault, deterministic retention, encryption at rest + in transit. *POC note: see DOC-12 DATA_HANDLING.md; live secrets currently sit plaintext in `.env` with gitignore as the only control — hardening tracked post-POC.* | Open |
| Model drift degrades outcomes silently | H | M | Chinmay | Continuous eval harness; drift-triggered retraining; champion/challenger rollouts; canary + automatic rollback. *POC note: eval harness scaffolded; truth-file consumption + automatic rollback are Phase 2.* | Open |
| Over-automation erodes SRE skills | M | M | Chinmay | Training-mode; visible RCA reasoning on every action; scheduled manual exercises; Toil Detector prioritizes judgment work. *POC note: long-horizon product risk; no action this phase.* | Accepted |

---

## Review log

Newest first. One line per review or status change.

- **2026-06-09** — Register created (DOC-4, #117). Seeded 9 operational risks + 6 platform
  risks from the Solution Design slide and the two architect retrospectives
  ([`docs/architect_retrospective_phase1.md`](docs/architect_retrospective_phase1.md),
  [`docs/architect_retrospective_issues_grounded.md`](docs/architect_retrospective_issues_grounded.md)).
  #113 (pytest hang) confirmed closed → marked Mitigated. Vendor lock-in marked Mitigated
  (seams structurally enforced). Owner: Sharvari.

---

## References

- [`docs/Adaptive_AIOps_Solution_Design.pptx`](docs/Adaptive_AIOps_Solution_Design.pptx) — section 8, Risks & mitigations slide (Section B above).
- [`docs/architect_retrospective_phase1.md`](docs/architect_retrospective_phase1.md) — recurring risks from memory (cluster OOM, two-CMDB-sources, PDI as unmanaged dependency, secrets, premature dashboard).
- [`docs/architect_retrospective_issues_grounded.md`](docs/architect_retrospective_issues_grounded.md) — team-shape risks (single-reviewer bus-factor, dev-b staffing, roadmap-vs-tracker drift).
