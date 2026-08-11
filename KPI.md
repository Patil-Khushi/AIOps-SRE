# KPI — Success Metrics Definition

**Owner:** Sharvari Kulkarni (Dev B, RA-003) · **Status:** Draft v1 · **Last updated:** 2026-08-11

[PRD.md §3](PRD.md) cross-references this document for the full KPI specification. The PRD names the load-bearing targets; this document defines each metric precisely (Definition / Baseline / Target / How measured) so a reviewer can ask *"is the demo actually moving this number?"* and get a defensible answer.

All targets are sourced from [Adaptive_AIOps_Solution_Design.pptx slide 12](docs/Adaptive_AIOps_Solution_Design.pptx). Per-agent KPIs are from the [Agent Catalog xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) Master sheet.

**Glossary.** **SLI** = measurable indicator. **SLO** = internal target. **SLA** = customer-facing contract. **MTTA / MTTR / MTTD / MTBF** = Mean Time To Acknowledge / Resolve / Detect / Between Failures. See [CLAUDE.md "Concept cheat sheet"](CLAUDE.md) for the broader vocabulary.

---

## 1. MTTA — Mean Time To Acknowledge

| | |
|---|---|
| **Definition** | Elapsed time from *alert fires in the source system* (Prometheus rule transitions to `firing`) to *a human acknowledges it* (clicks Ack in PagerDuty, posts in the Slack thread, or claims the ticket in ServiceNow). Measured per incident, then averaged over a rolling window. |
| **Baseline** | Typical NOC dwell time on Sev-1/2 is 5–15 minutes — the alert sits in a queue until the on-call notices the page, opens Slack, finds the right channel, and confirms ownership. The demo's pre-RA-005 baseline is "alert appears in Prometheus → no human action until the dashboard is manually refreshed." |
| **Target** | **< 2 minutes for Sev-1/2** ([Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12). The auto-triage loop (#130) closes this further by triggering the pipeline within ~3 s of the alert appearing in `/api/live-alerts`. |
| **How measured** | RA-001 emits `created_at` on the `TriageVerdict` ([agents/alert_triage/](agents/alert_triage/)). the Notification Assembler (RA-005+006) emits a `decided_at` on `NotificationOutcome.decision` ([agents/notification_assembler/models.py](agents/notification_assembler/models.py)). PagerDuty webhook posts the `acknowledged_at` back. MTTA = `pd.acknowledged_at − verdict.created_at`. For the demo, the audit log at `demo/audit/chatops.jsonl` is the source of truth — every send + ack has a timestamp. Post-POC: surface on a Grafana panel against the live Prometheus alert-state series. |

---

## 2. MTTR — Mean Time To Resolve

| | |
|---|---|
| **Definition** | Elapsed time from *alert fires* to *incident is closed* (ServiceNow incident state = Resolved AND the underlying alert has returned to `inactive` for ≥ N minutes). Includes triage + classify + page + investigate + fix + verify. |
| **Baseline** | Industry medians: minutes-to-hours for Sev-1 with a known runbook; hours-to-days for Sev-2/3 ([SRE workbook](https://sre.google/workbook/), Atlassian incident reports). The demo's pre-RCA-Agent baseline is "human reads the dashboard → opens the matching runbook URL → executes manually → confirms." |
| **Target** | **−40 to −55% versus baseline** ([Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12) when the RCA Agent produces executable fix steps that the operator can approve in one tap. |
| **How measured** | RA-003 records ticket `created_at` ([aiops/tools/itsm/servicenow.py](aiops/tools/itsm/servicenow.py)) and ticket `resolved_at` (set when ServiceNow marks the incident Resolved). MTTR = `resolved_at − verdict.created_at`. For the POC demo: the audit log + the dashboard verdict timeline pane already show start/end. Post-POC: a Grafana panel joining alert state with ServiceNow webhooks. |

---

## 3. MTTD — Mean Time To Detect

| | |
|---|---|
| **Definition** | Elapsed time from *failure begins in the system under observation* (the failure-injection flag is flipped, or the real-world fault starts) to *the alert fires*. Measures the observability layer, not the agents. |
| **Baseline** | Bound by the alert rule's `for:` window. Rules in [demo/otel-demo/values.yaml](demo/otel-demo/values.yaml) (the Helm values block patched onto the Prometheus configmap) all currently use `for: 15s` after [#4 [A4] tightening](https://github.com/UbiquotousPanda/AIops/issues/4) landed; that is the MTTD floor for the demo today. Real-world NOCs typically see 30 s – 5 min depending on rule design. |
| **Target** | < 90 s for every demo scenario — the 15 s `for:` window plus Prometheus scrape interval plus normalisation pipeline. Hard requirement: the rehearsed demo flow must fire on the speaker's beat without the audience waiting on a `for:` window. |
| **How measured** | `inject_at = $(date)` is captured by the failure-injection runner ([demo/failure_injection/inject.py](demo/failure_injection/inject.py)). Prometheus `ALERTS{alertstate="firing"}` timestamp is the detect time. MTTD = `firing_at − inject_at`. The dashboard's "Active scenarios → Firing alerts" pane already exposes both; a small additional cell could derive the delta. |

---

## 4. RCA pass rate

| | |
|---|---|
| **Definition** | Fraction of eval-harness cases for the RCA Agent (PRS-008) whose scored output `passed=True`. Two flavours: **agent-level pass rate** (across all goldens in `agents/rca_agent/evals/golden.json`) and **per-scenario pass rate** (a single golden case scored in isolation, useful for "the demo scenario specifically works"). |
| **Baseline** | 0%. There is no automated RCA in the demo stack today; human postmortems are the only source. The CI gate today is `--min-pass-rate 0.85` for any shipped agent (see [`evals/harness.py`](evals/harness.py)), so the RCA Agent inherits the same bar at the platform level even if its absolute first target is lower. |
| **Target** | **≥ 0.6 for v0**, climbing to **≥ 0.75 (top-3 RCA accuracy)** in steady-state per [Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12. The DOC-1 PRD §3 also names **fix-step acceptance ≥ 70%** as the closely related second-order metric. |
| **How measured** | `AgentRun.pass_rate` at [evals/harness.py:88-92](evals/harness.py#L88-L92): `sum(1 for r in self.results if r.passed) / len(self.results)`. Run with `uv run python -m evals.harness --agent rca_agent`. CI gate: `--ci --min-pass-rate 0.85` exits non-zero on regression. Per-scenario breakdown: `--ci` emits a JSON summary with the full `results: [...]` list per agent — read the per-case `passed` field. Truth files (`demo/truth_files/<scenario>.yaml`) provide the ground-truth `expected` payload that `score_case` compares the agent's output against. |

---

## 5. HITL approval-time SLO

| | |
|---|---|
| **Definition** | Elapsed time from *the platform creates an approval* (`HITLGate.enforce(...)` registers a pending decision and the chatops sink posts the approve/deny prompt) to *a reviewer responds* (Slack button click, API POST to `/api/approvals/{id}/approve\|deny`). Measured per approval, reported as **median** and **p95** over a rolling window. |
| **Baseline** | None — there is no platform HITL gate in the comparison stack. Manual change-approval flows in ServiceNow CAB or Jira typically run hours-to-days; that is not directly comparable because most are batch rather than per-action gates. Document the target only. |
| **Target** | **Median < 60 s, p95 < 5 min** for demo-tier Required-HITL actions (RCA fix steps, runbook execution, capacity/policy changes). Numbers tuned for "presenter taps Approve on stage and the action runs" — post-POC SLAs will differ per action class. |
| **How measured** | `HITLGate.enforce(...)` records `created_at` on the pending approval ([aiops/policy/gate.py](aiops/policy/gate.py)). Approval listeners receive a `decided_at` on the resolution event. Approval latency = `decided_at − created_at`. Per-action breakdown lands in the audit log (`demo/audit/chatops.jsonl` — `interactive.approval_id` ties prompt to outcome). Post-POC: a percentile aggregator (Prometheus histogram + Grafana panel). |

---

## 6. Notification deliverability

| | |
|---|---|
| **Definition** | Per-routing-decision percentage of selected sinks where the delivery succeeded. A routing decision can fan out to N adapters (Slack channel, PagerDuty page, JSONL audit log, future Teams); deliverability is the fraction where `DeliveryResult.ok == True` for the adapters RA-005 decided to send to. The metric matches the DOC-3 issue wording: *"% of routing decisions where every selected sink succeeded."* |
| **Baseline** | Pre-PR #127, RA-005 did not expose per-adapter delivery outcomes — a Slack post-failure was silently swallowed. So the baseline is "we cannot tell." With PR #127's `RoutingOutcome.deliveries: dict[str, DeliveryResult]`, the metric becomes observable for the first time. |
| **Target** | **≥ 99% all-sinks-OK rate**, measured over rolling 7-day windows in steady state. PagerDuty and Slack vendor SLAs are both ≥ 99.95% — the practical headroom is dominated by transient network failures and adapter-side rate limits. For the demo, "every demo run delivers to all configured sinks" is the acceptance bar. |
| **How measured** | `ChatOpsClient.send()` returns `dict[str, DeliveryResult]` ([aiops/tools/chatops/client.py:54](aiops/tools/chatops/client.py#L54)). Each `DeliveryResult` has `adapter`, `ok`, `error`, `latency_ms` ([aiops/tools/chatops/models.py:77-81](aiops/tools/chatops/models.py#L77-L81)). the Notification Assembler's `notify()` returns `NotificationOutcome.deliveries` containing the same map ([agents/notification_assembler/models.py](agents/notification_assembler/models.py)). Compute: `all_ok = all(d.ok for d in outcome.deliveries.values())` per routing, then aggregate. Currently surfaced in the route return value and the audit log; not yet on a dashboard. |

---

## 7. Alert noise reduction

| | |
|---|---|
| **Definition** | Fraction of *raw alerts* (Prometheus alert-firing events arriving at `/api/triage` or `/api/live-alerts`) that get *suppressed or correlated* before a human is notified. Two sub-components: **dedup-suppressed** (RA-001 collapses N firing instances of the same rule into one verdict) and **policy-suppressed** (RA-005 chooses to send to no sink — e.g., Sev-4 alert outside business hours when the policy says noise channel is disabled). Both are observable in the verdict and the routing outcome. |
| **Baseline** | Industry-quoted "80–90% of alerts are noise" is the rough starting point. The OpenTelemetry Demo with no agents in front of it produces ~5–20 firing-rule events per minute under steady k6 load — 1:1 with the rules; no dedup, no correlation. |
| **Target** | **−60 to −75% (suppression rate)** ([Solution Design](docs/Adaptive_AIOps_Solution_Design.pptx) slide 12). For the demo specifically: if 4 scenarios are injected and ≥ 3 share an upstream cause, the dashboard should show 1 incident rather than 4 (this is already partially exercised by [DEMO-5](https://github.com/UbiquotousPanda/AIops/issues/57)). |
| **How measured** | RA-001 emits `duplicate_alert_count` on each `TriageVerdict` (number of source alerts collapsed into this verdict). Total suppression = `1 - (unique_verdicts / raw_alert_events)` over a window. Today: countable from the SQLite `verdicts` table + `source_alerts` list. Post-POC: a Prometheus counter incremented at every triage call (raw and verdict-emitted) → a Grafana panel computing the ratio. |

---

## 8. Context Engineering Layer (in progress)

`aiops/context/` collapses retrieval that RCA, Notification Assembler, and (partially) Alert Triage / Log Correlation currently do independently — see [CLAUDE.md "Context Engineering Layer"](CLAUDE.md) for the architecture. It is gated by `AIOPS_CONTEXT_LAYER` (`off` / `shadow` / `on`, default `off`) and **is not yet influencing any of the seven metrics above** — every agent still runs its pre-existing retrieval path until its own shadow comparison proves parity. The rows below are engineering leading-indicators for the migration itself, not customer-facing value metrics — they do not belong on a business-facing KPI slide until the shadow-parity gate is satisfied.

| Metric | Definition | Baseline (2026-08-11) | Target | How measured |
|---|---|---|---|---|
| Retrieval de-duplication | Count of direct capability call sites per incident for a fact that shouldn't be fetched twice per incident (e.g. `oncall.schedule.lookup`) | 3–4 call sites still tracked in the migration ledger (`agents/alert_triage/agent.py`, `alert_triage/classifier.py` ×1 deliberate, `notification_assembler/agent.py` ×2) | 1 shared fetch per incident per capability, once an agent fully migrates | `tests/test_retrieval_call_sites.py` — a ratchet: `RETRIEVAL_LEDGER` counts must go *down*, never up, as each agent migrates |
| Shadow-mode parity | Mismatches between shadow-mode context output and the legacy retrieval path it will replace | Not yet measured over a continuous rehearsal window | 0 mismatches sustained over a full rehearsal window — the literal go/no-go gate for flipping `AIOPS_CONTEXT_LAYER` to `on` | `aiops.context.shadow.stats()`, exercised by `tests/test_context_shadow.py` |
| Token-budget adherence | % of assembled `IncidentContext`s that fit the requesting consumer's token profile without silently dropping a required section | Not yet load-bearing — budgeting is opt-in per request today | 100% — an over-budget context should visibly rank down or truncate, never silently drop a required section | `TokenBudget` / `estimate_context_tokens` in `aiops/context/pack.py` |
| Redaction completeness | Secrets / PII observed in any assembled context section | Not yet measured — **no dedicated audit test exists today** | 0 leaked secrets/PII, always | Gap, not yet closed. Flagged here rather than in §10 because it is a rollout blocker for `on`, not a scope decision |

**Before `AIOPS_CONTEXT_LAYER=on` ships, re-run these existing metrics rather than assuming they still hold:**

- **§4 RCA pass rate** — the context layer changes what evidence reaches the RCA agent. This document's own rule ("a prompt change is a model change — re-run evals") applies equally to an evidence-source change. Re-run `agents/rca_agent/evals/golden.json` with the context layer on before trusting the pass-rate number against the new path.
- **§2 MTTR** — indirectly, if RCA quality shifts once its evidence source changes.

---

## 9. Per-agent KPIs (the catalog mapping)

This table mirrors the KPI column of the [Agent Catalog xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) Master sheet. The metrics above are the **platform-level** KPIs that aggregate across agents and the integration layer; the per-agent KPIs are what each agent's eval set + dashboard panel must surface individually.

| ID | Agent | Phase | HITL | KPI |
|---|---|---|---|---|
| RA-001 | Alert Triage Agent | Reactive-Active | Optional | MTTA reduction %, noise-suppression rate |
| RA-002 | Incident Classifier Agent | Reactive-Active | Optional | Classification accuracy %, misroute rate |
| RA-003 | Auto-Ticketing Agent | Reactive-Active | Optional | Ticket automation %, ticket accuracy score |
| RA-004 | Runbook Executor Agent | Reactive-Active | Required | Auto-remediation success %, rollback incidents |
| RA-005+006 | Notification Assembler Agent (merged) | Reactive-Active | Optional | Acknowledgement latency, escalation rate, time-to-bridge, SME coverage % |
| RA-007 | Log Correlation Agent | Reactive-Active | None | MTTI reduction, evidence completeness |
| RA-008 | Incident Commander Agent (SRE) | Reactive-Active | Optional | Incident-communication compliance %, postmortem cycle time |
| PRO-001 | Anomaly Detector Agent | Proactive | Optional | Early-warning lead time, false-positive rate |
| PRO-002 | Drift Monitor Agent | Proactive | Optional | Drift detection lead time, high-risk drift count |
| PRO-003 | Dependency Mapper Agent | Proactive | None | Graph freshness, CMDB accuracy % |
| PRO-004 | Noise Reducer Agent | Proactive | Optional | Event compression ratio, signal-to-noise |
| PRO-005 | Early Warning Agent | Proactive | Optional | Warning precision, prevented-incident count |
| PRO-006 | Topology Discovery Agent | Proactive | None | CMDB accuracy, orphan CI count |
| PRO-007 | Toil Detector Agent (SRE) | Proactive | Optional | Toil hours eliminated per quarter, automation acceptance rate |
| PRE-001 | Failure Forecaster Agent | Predictive | Optional | Precision@k on predicted failures, prevented outages |
| PRE-002 | Capacity Planner Agent | Predictive | Required | Forecast accuracy (MAPE), cost savings realized |
| PRE-003 | SLO Breach Predictor Agent | Predictive | Required | Breach-prediction precision, budget-save rate |
| PRE-004 | Seasonality Learner Agent | Predictive | None | Baseline stability, downstream false-positive reduction |
| PRE-005 | Root-Cause Predictor Agent | Predictive | Optional | Top-1/Top-3 RCA accuracy, MTTR reduction |
| PRE-006 | Change Impact Predictor Agent | Predictive | Required | Change-failure rate, CAB cycle-time |
| PRE-007 | Reliability Forecaster Agent (SRE) | Predictive | Optional | Reliability-forecast accuracy, services saved from SLO breach |
| PRS-001 | Remediation Recommender Agent | Prescriptive-Adaptive | Required | Recommendation acceptance %, success rate |
| PRS-002 | Auto-Healer Agent | Prescriptive-Adaptive | Optional | Auto-heal success rate, unintended impact events |
| PRS-003 | Policy Optimizer Agent | Prescriptive-Adaptive | Required | Policy-improvement rate, guardrail violations (0 target) |
| PRS-004 | Feedback Learner Agent | Prescriptive-Adaptive | Required | Model-improvement cadence, regression incidents |
| PRS-005 | Cost-Aware Scaler Agent | Prescriptive-Adaptive | Optional | $ saved per month, SLO-preserving scale-downs |
| PRS-006 | Knowledge Synthesizer Agent | Prescriptive-Adaptive | Required | KB coverage %, agent-answer grounding rate |
| PRS-007 | Chaos Engineering Orchestrator Agent (SRE) | Prescriptive-Adaptive | Required | Chaos coverage %, unintended impact (target: 0), insights/quarter |
| PRS-008 | RCA Agent ★ | Prescriptive-Adaptive | Required | RCA accuracy vs. verified cause, fix-step acceptance rate, MTTR reduction attributable to RCA |

★ = headline differentiator. Required-HITL ★ items are platform-gated, never bypassable from agent code (CLAUDE.md non-negotiable #3).

---

## 10. What this document does not cover

- **Per-metric dashboards.** The "How measured" column names the data source and the code that emits it. Building the Grafana panels (or the equivalent dashboard cards) is operational follow-up, not part of this doc.
- **Alerting on the metrics themselves.** When MTTR regresses by 50% week-over-week, *who gets paged?* Belongs in a runbook + the [DOC-9 POST_POC_ROADMAP.md](https://github.com/UbiquotousPanda/AIops/issues/122).
- **SLO-vs-SLA distinction for customer commitments.** All targets here are internal SLOs. Customer-facing SLAs require legal review and live in a separate contract document, out of POC scope.
- **Cost metrics.** $/incident, $/ticket, LLM-cost-per-triage, infrastructure cost-per-tenant. Mentioned in PRD §3 (PRS-005 KPI) but not defined here because the cost-accounting plumbing is post-POC.
- **Eval methodology in depth.** *How* a golden case is judged "passed" (`score_case` returns `{passed: bool, score: float, details: {...}}`) belongs in [DOC-8 EVAL_METHODOLOGY.md](https://github.com/UbiquotousPanda/AIops/issues/121).

---

## References

- [PRD.md §3 — Success metrics](PRD.md) — the prose summary this document expands.
- [Adaptive_AIOps_Solution_Design.pptx slide 12](docs/Adaptive_AIOps_Solution_Design.pptx) — all platform-level numerical targets.
- [Adaptive_AIOps_Agent_Catalog.xlsx](docs/Adaptive_AIOps_Agent_Catalog.xlsx) Master sheet KPI column — authoritative per-agent metric list.
- [evals/harness.py](evals/harness.py) — `AgentRun.pass_rate`, `TruthFileRun.pass_rate`, CI gate.
- [aiops/tools/chatops/models.py:77-81](aiops/tools/chatops/models.py#L77-L81) — `DeliveryResult` model.
- [agents/notification_assembler/models.py](agents/notification_assembler/models.py) — `NotificationOutcome.deliveries`.
- [CLAUDE.md "Concept cheat sheet"](CLAUDE.md) — SLI/SLO/SLA/MTTA/MTTR/MTTD/MTBF/toil/blast-radius vocabulary.
- [CLAUDE.md "Context Engineering Layer"](CLAUDE.md) — `aiops/context/` architecture, rollout gate, and adapter pattern behind §8.
- [tests/test_retrieval_call_sites.py](tests/test_retrieval_call_sites.py) — the retrieval-deduplication ratchet.
- [tests/test_context_shadow.py](tests/test_context_shadow.py) — the shadow-parity gate for `AIOPS_CONTEXT_LAYER=on`.

Cross-references to sibling DOC-* tickets: [DOC-1 PRD.md (#114)](https://github.com/UbiquotousPanda/AIops/issues/114) · [DOC-4 RISK_REGISTER.md (#117)](https://github.com/UbiquotousPanda/AIops/issues/117) · [DOC-8 EVAL_METHODOLOGY.md (#121)](https://github.com/UbiquotousPanda/AIops/issues/121) · [DOC-9 POST_POC_ROADMAP.md (#122)](https://github.com/UbiquotousPanda/AIops/issues/122).
