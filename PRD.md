# Product Requirements Document — Adaptive AIOps + SRE Ops

**Owner:** Sharvari Kulkarni (Dev B, RA-003) · **Status:** Draft v1 · **Last updated:** 2026-05-26

This document defines the product. The [Solution Design pptx](docs/Adaptive_AIOps_Solution_Design.pptx) defines the architecture; the [Agent Catalog xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) is the authoritative agent reference. This PRD anchors the **why**, the **who**, the **what we are shipping in the POC**, and the **what we are explicitly not shipping**. It is the source of scope truth — every other doc in the DOC-* series cross-references it.

---

## 1. Problem statement

IT operations teams in mid-to-large enterprises spend most of their incident time on three problems that compound each other.

**1.1 Alert fatigue.** A typical NOC sees thousands of alerts per day, of which the majority are duplicates, known-noise patterns, or low-severity events that should never have paged a human. Engineers learn to ignore the queue, which means the genuine Sev-1 signal arrives late or not at all. The Solution Design targets a 60–75% noise reduction in pilot scope ([Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12).

**1.2 Slow incident response (MTTA, MTTR).** Even when an alert is seen, the chain from *alert fires* → *human pages* → *correct on-call engages* → *root cause identified* → *fix applied* is mostly manual and serial. The Solution Design targets MTTA under 2 minutes for Sev-1/2 and MTTR reduction of 40–55% versus baseline (slide 12).

**1.3 Toil — the SRE-named enemy.** Repetitive manual work that scales linearly with the system: opening tickets, copying context between tools, running known runbooks, writing the same Slack updates. Toil crowds out engineering work and burns out staff. The Solution Design targets ≥500 toil hours eliminated per quarter by stage 3 (slide 12).

Underneath these three are two systemic issues the product is engineered against:

- **Vendor lock-in on the agent layer.** Once an organisation commits to a single ITSM, observability, or automation vendor's "AIOps module," swapping it out is a multi-quarter project. Every customer we talk to has a different stack.
- **RCA is the unsolved hard problem.** Existing "AIOps" products produce a likely-cause list. They do not produce *executable fix steps with rollback plans*. The audit trail from "alert" to "verified cause" to "applied fix" is fragmented across tools, which is why postmortems take days to write.

The product addresses (1.1–1.3) directly with the Reactive-Active and Proactive phase agents, and addresses the systemic issues with the architecture — vendor-neutral seams everywhere, plus a dedicated **RCA Agent** that produces fix steps and a rollback plan, not a cause list (slide 8; [Agent Catalog](docs/Adaptive_AIOps_Agent_Catalog.xlsx) PRS-008).

---

## 2. Target users

The Solution Design audience is "architects, SRE leads, platform engineering" (slide 1). The product itself has three concrete user personas, in priority order:

**Primary — Site Reliability Engineering lead / on-call incident commander.** Owns Sev-1 and Sev-2 response. Cares about MTTA, MTTR, error budget, and the postmortem trail. Today: spends most of an incident copy-pasting between Splunk/Dynatrace/ServiceNow/Slack windows. Wants: a single console that shows the live signal, the suggested action, and a clear audit trail of who approved what. Will trust automation only when the platform makes the gate non-bypassable (CLAUDE.md non-negotiable #3).

**Primary — NOC operator / Tier-1 incident triager.** Receives the alert flood. Cares about *which alert matters right now*. Today: spends most of a shift acknowledging duplicates and routing tickets. Wants: noise suppressed, severity scored, ticket pre-filled, on-call paged automatically, and a stable mental model of which agent did what.

**Secondary — Platform / IT-ops engineering manager.** Owns the toolchain and the budget. Cares about vendor risk, integration cost, and being able to swap any single layer (ITSM, observability, on-call, automation, LLM) without a re-platform. Wants: every integration point to have at least two documented alternatives (slide 4; CLAUDE.md non-negotiable #1).

**Secondary — Enterprise architect evaluating the platform.** Cares about governance — policy-as-code coverage, HITL enforcement at the platform layer (not at the agent layer), audit-trail completeness. Will not approve a procurement without a written threat model (tracked in [DOC-7](https://github.com/UbiquotousPanda/AIops/issues/120)).

The POC demo is rehearsed for the primary personas. The 6-minute script ([DEMO_PLAN.md](DEMO_PLAN.md)) puts an SRE lead in front of a real Slack approval and a real PagerDuty page — that audience is the proxy for the primary buying decision.

---

## 3. Success metrics

The full KPI specification with measurement windows, owners, and dashboards belongs in [DOC-3 KPI.md](https://github.com/UbiquotousPanda/AIops/issues/116). The PRD names the load-bearing targets only. All numbers are from [Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12 unless noted.

**Operational (Reactive-Active value).**
- MTTA: < 2 minutes for Sev-1/2.
- MTTR including RCA Agent: −40 to −55% versus baseline.
- Alert noise: −60 to −75%.
- Auto-remediation success rate: > 85% on enabled actions.

**Predictive / RCA quality.**
- Top-3 RCA accuracy: > 75% (RCA Agent).
- RCA fix-step acceptance rate: > 70%.
- SLO-breach prediction precision: > 65%.
- Prevented Sev-1 per month: ≥ 2 post-stage-2.

**SRE reliability.**
- Toil hours eliminated per quarter: ≥ 500 by stage 3.
- Reliability-forecast accuracy: > 70%.
- Chaos coverage: ≥ 80% of Tier-1 services.
- Unintended chaos impact: 0 (hard target).

**AI quality & safety.**
- Hallucination rate: < 2%.
- Guardrail violations: 0 (hard target).
- HITL override rate: downward trend quarter over quarter.
- Model drift alarms: < 1 per model per quarter.

For the POC specifically, the demo-day acceptance metric is narrower: the 6-minute demo script ([DEMO_PLAN.md](DEMO_PLAN.md)) runs cleanly twice in a row, every action the platform takes appears in `demo/audit/chatops.jsonl`, and the eval harness (`uv run python -m evals.harness --ci --min-pass-rate 0.85`) stays green on the shipped agents.

---

## 4. MVP scope

The MVP is the **end-to-end Reactive-Active path** plus the **RCA Agent** plus the **platform-level HITL gate**. Six components, exercised by the rehearsed 6-minute demo, each independently licensable per the modularity principle (CLAUDE.md non-negotiable #2). All agent details below are from the [Agent Catalog xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) Master sheet.

| # | ID | Component | Phase | HITL | Demo-day check |
|---|---|---|---|---|---|
| 1 | RA-001 | Alert Triage Agent | Reactive-Active | Optional | Dedups, scores severity, routes to team within seconds. *Verify:* dashboard Alert Stream panel populates within ~5 s of inject. |
| 2 | RA-002 | Incident Classifier Agent | Reactive-Active | Optional | NLP classifies incident type and confidence. *Verify:* `/api/triage` response includes a `classification` block. |
| 3 | RA-003 | Auto-Ticketing Agent | Reactive-Active | Optional | Creates a ServiceNow incident with full description, assignment group, runbook link. *Verify:* the incident in ServiceNow PDI has more than just `short_description`. |
| 4 | RA-005 | Notification Router Agent | Reactive-Active | None | Routes to Slack channel + PagerDuty per severity policy. *Verify:* `#aiops-poc-alerts` Slack channel + lead's phone both fire. |
| 5 | PRS-008 | **RCA Agent** ★ | Prescriptive-Adaptive | **Required** | Produces a causal chain + prioritized executable fix steps + rollback plans. *Verify:* dashboard verdict panel shows fix steps; Slack approval button posts; tap → action runs → confirmation. |
| 6 | — | **HITL gate** (platform) | All phases | n/a | Required-level actions cannot bypass the gate at the agent layer. *Verify:* `tests/test_smoke.py::test_hitl_gate_blocks_required_without_approver` is green; manual deny in Slack visibly blocks the RCA fix step. |

The HITL gate is not an agent — it is a platform component (`aiops/policy/`) that every agent's destructive action must pass through. It is in the MVP because the **product story collapses without it**: an RCA Agent that executes fixes without a non-bypassable approval is what every existing AIOps tool already offers and which has earned the industry's distrust.

**Adjacent components in the POC tree but not in the MVP demo path.** `agents/auto_ticketing/`, `aiops/tools/chatops/`, `aiops/tools/itsm/` (ServiceNow PDI), `aiops/tools/feature_flags/`, the auto-triage loop (`AIOPS_AUTO_TRIAGE_ENABLED`), the dashboard SPA, and the eval harness all exist to support the six MVP components. They are shipped — they are not the headline.

---

## 5. Non-goals

These are out of scope for the POC. Each is here because someone has asked for it and the answer is "not in this milestone."

**5.1 The other 24 agents in the catalog.** The full 30-agent product is in the [Agent Catalog xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx). The POC does not build Runbook Executor (RA-004), War-Room Assembler (RA-006), Log Correlation (RA-007), Incident Commander SRE (RA-008), the full 7-agent Proactive phase, the full 7-agent Predictive phase, or any Prescriptive agent other than the RCA Agent. They may appear as stubs for narrative continuity in slides; they are not code in this milestone.

**5.2 Multi-tenancy.** Single-tenant only. No tenant isolation, no per-tenant policy bundles, no per-tenant LLM provider routing. Architecture is designed not to preclude it; implementation is deferred.

**5.3 Production HA / availability.** The POC runs on a single-node Rancher Desktop k3s cluster on a developer laptop. No replication, no failover, no autoscaling, no SLA. AKS / GKE / EKS deployment is post-POC ([CLAUDE.md](CLAUDE.md) §"POC scope discipline").

**5.4 Real customer data.** All demo traffic is synthetic, generated by the OpenTelemetry Demo (Astronomy Shop) and k6 load. No PII, no production telemetry, no customer-owned tickets.

**5.5 Polished UI.** The dashboard (`demo/dashboard/`) and classifier SPA (`demo/classifier-ui/`) are demo-quality, not product-quality. No accessibility audit, no responsive breakpoints below desktop, no internationalisation, no SSO.

**5.6 Full vendor matrix.** The Solution Design lists ≥2 alternatives per integration layer (slide 9). The POC implements ServiceNow + Jira (ITSM), Prometheus + Jaeger (observability), Slack + PagerDuty (chatops + on-call), one LLM (Anthropic or OpenAI via stub-by-default), and OPA (policy). The remaining alternatives are documented but not wired.

**5.7 Anything from the post-POC backlog.** Loki + Tempo deployment ([#73](https://github.com/UbiquotousPanda/AIops/issues/73)), orchestrator stub ([#74](https://github.com/UbiquotousPanda/AIops/issues/74)), and the rest of the "out of demo MVP" list in [DEMO_PLAN.md](DEMO_PLAN.md) are tracked, prioritized, and deferred.

---

## 6. Assumptions

These hold for the POC milestone. If any breaks, the MVP scope or timeline shifts.

- **Developer environment is Rancher Desktop k3s (Windows/macOS), 16 GB laptops.** Org policy bans Docker on dev machines; AKS / GKE deferred (CLAUDE.md §"Local environment constraints").
- **Stack is FOSS-first.** OpenTelemetry Demo, Prometheus, Loki, Tempo, Grafana, OPA, k6 — all open-source. Commercial dependencies are limited to ServiceNow PDI (free dev instance), Slack (free workspace), and PagerDuty (free developer account).
- **Every external dependency is wrapped behind an internal seam from day one** — `aiops/llm/` for the LLM, `aiops/tools/itsm/` for ServiceNow, `aiops/tools/chatops/` for Slack, `aiops/tools/feature_flags/` for flagd. Direct vendor-SDK imports outside these seams fail the smoke test (CLAUDE.md non-negotiable #1).
- **HITL is platform-enforced, not agent-enforced.** Required-level actions are gated by `aiops/policy/get_gate().enforce(...)` at the action boundary, not by `if user_confirmed:` inside the agent (CLAUDE.md non-negotiable #3).
- **Every demo scenario ships with a truth file** under `demo/truth_files/` so the eval harness has ground truth (CLAUDE.md non-negotiable #8).
- **Demo data is synthetic.** The Astronomy Shop's built-in feature flags drive failure injection; no real-system telemetry is consumed.
- **LLM is pinned, not "latest."** Model IDs are explicit in `.env` per the onboarding guide reference stack; `claude-sonnet-4-6` or `gpt-5` for the POC.

---

## 7. Open questions

Tracked here so reviewers can take them, not because they're blocking the current milestone.

1. **Production LLM provider strategy.** The POC defaults to Anthropic; Azure OpenAI is wired as an alternative ([DEMO-11 #63](https://github.com/UbiquotousPanda/AIops/issues/63) `.env.example` overhaul). The product story is vendor-neutral, but customer procurement will ask "which model?" Owner: post-POC decision; capture in [DOC-10 MODEL_CARDS.md](https://github.com/UbiquotousPanda/AIops/issues/123) when it exists.
2. **Multi-tenancy model.** The architecture does not preclude it, but the boundary (tenant = ServiceNow instance? tenant = k8s namespace? tenant = LLM API key?) is not chosen. Needed before any second customer.
3. **Licensing model.** The Solution Design says "each agent is individually sellable" (slide 4). The pricing structure, license enforcement, and entitlement check have not been designed. Out of POC, in for post-POC.
4. **PII handling in non-stub mode.** When the LLM provider is a real vendor, telemetry sent to the model may contain PII. The redaction layer is mentioned as a risk mitigation ([Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 13) but not built. Owner: [DOC-12 DATA_HANDLING.md](https://github.com/UbiquotousPanda/AIops/issues/125).
5. **Eval harness sufficiency.** The current hand-rolled JSON test cases (`evals/golden.json` per agent) are adequate for the POC but will not scale to 30 agents. The onboarding guide suggests Ragas / DeepEval / LangSmith for later; the choice is unmade. Owner: [DOC-8 EVAL_METHODOLOGY.md](https://github.com/UbiquotousPanda/AIops/issues/121).
6. **Champion / challenger rollout discipline.** Slide 4 mentions shadow-eval-before-promotion; the actual rollout pipeline does not exist yet. Needed before the first model retrain in production.

---

## References

- [Adaptive_AIOps_Solution_Design.pptx](docs/Adaptive_AIOps_Solution_Design.pptx) — phase decomposition, HITL policy, integration matrix, rollout plan, KPIs, risks
- [Adaptive_AIOps_Agent_Catalog.xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) — authoritative agent reference (description, inputs/outputs, HITL, KPI, sellable-standalone flag)
- [Adaptive_AIOps_Unified_Architecture.pptx](docs/Adaptive_AIOps_Unified_Architecture.pptx) — one-slide master architecture diagram
- [DEMO_PLAN.md](DEMO_PLAN.md) — the 6-minute demo script that informs the MVP scope
- [CLAUDE.md](CLAUDE.md) — non-negotiable design principles and POC scope discipline
- [docs/aiops_onboarding_guide.docx](docs/aiops_onboarding_guide.docx) — concept primer (AIOps, SRE, RCA, agentic AI vocabulary)
- [docs/poc_aiops_onboarding_guide.docx](docs/poc_aiops_onboarding_guide.docx) — POC playbook, reference stack, 12-week roadmap

Cross-references to sibling DOC-* tickets: [DOC-2 DEMO_SCRIPT.md (#115)](https://github.com/UbiquotousPanda/AIops/issues/115) · [DOC-3 KPI.md (#116)](https://github.com/UbiquotousPanda/AIops/issues/116) · [DOC-4 RISK_REGISTER.md (#117)](https://github.com/UbiquotousPanda/AIops/issues/117) · [DOC-5 ADR series (#118)](https://github.com/UbiquotousPanda/AIops/issues/118) · [DOC-6 ARCHITECTURE.md (#119)](https://github.com/UbiquotousPanda/AIops/issues/119).
