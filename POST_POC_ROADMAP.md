# Post-POC Roadmap

> What happens after the POC (target end **2026-06-14**). One page, on purpose. The intent is to have an answer ready in the demo room instead of improvising it.
>
> **Where we are:** the POC shipped ~14 agents end-to-end (it outgrew its planned 6) across the full Reactive→Prescriptive path, on synthetic OpenTelemetry-Demo data. This roadmap maps those forward and aligns with the 20-week production rollout in `docs/Adaptive_AIOps_Solution_Design.pptx` (slide 11).
>
> **Definition of "production" for this POC's outputs:** runs on a customer's real observability source (not the OTel demo), every action is policy-gated + audited, every prompt/model is pinned and eval-gated before promotion, and the agent meets its catalog KPI on the customer's own truth files — not just the demo set.

---

## Phase 2 — Keep + harden (Jun 15 – Aug 31, 2026)

Goal: take the Reactive backbone from "works in the demo" to "runnable on one real customer signal," per the rollout plan's **Foundation** exit criterion (≥40% noise reduction in pilot scope).

**Becomes production-grade (harden in place — contracts are stable):**
- `alert_triage`, `incident_classifier`, `auto_ticketing`, `log_correlation` — the Reactive backbone. Harden: real Prometheus/Alertmanager ingest, ServiceNow PDI → a real PDI/tenant, dedup tuning against customer alert volume.
- `notification_router` + `notification_assembler` + `war_room_assembler` — the combined notify path. Harden: wire a real Slack/Teams sink behind the existing chatops seam (today: JSON-file + WebSocket).
- `incident_commander` (SRE) — keep; it runs Sev-1/2 coordination.

**Hardened carefully, stays Required-HITL (do not loosen the gate):**
- `rca_agent`, `remediation_recommender`, `runbook_executor`, `auto_healer_lite` — the prescriptive loop. Keep every fix step behind `aiops.policy` HITL + dry-run + rollback. No autonomy increase this phase.

**Rewritten vs left alone:**
- *Left alone:* the platform seams (`aiops/llm`, `aiops/tools`, `aiops/policy`) and agent input/output contracts — they did their job.
- *Rewritten:* `data/state.db` SQLite → Postgres (concurrent access); the demo FastAPI UI hardened into an auth'd app; chatops sinks moved from file/WS to a real broker.

**Headcount ask:** current team is 3 (dev-a, dev-b, owner). Phase 2 as scoped needs **+1 backend** (Postgres/seam hardening, real-source ingest) and **part-time SRE/design review**. Hold net new agents until Phase 3.

---

## Phase 3 — Add (Sep 1 – Nov 30, 2026)

Goal: light up the Proactive layer and the first real predictions — the rollout plan's **Proactive** (≥2 prevented Sev-1/month, toil ROI dashboard) and entry into **Predictive**.

**Recommended next agents — the remaining SRE-specific agents + 2 predictive:**
1. **Toil Detector** (Proactive, SRE) — finds automation wins; produces the toil-ROI dashboard the rollout plan gates on.
2. **Reliability Forecaster** (Predictive, SRE) — multi-signal reliability trajectory, weeks ahead; the headline SRE forecast.
3. **Chaos Orchestrator** (Prescriptive, SRE) — validates resilience; the last of the 4 SRE agents. Blast-radius-capped, on-call-approved.
4. **SLO Breach Predictor** (Predictive) — error-budget burn + freeze hints; concrete, demo-friendly "wow."
5. **Failure Forecaster** (Predictive) — component/service failure probability + time; pairs naturally with RCA.

**Why these, not others:** they complete the SRE story (1 SRE agent per phase) that differentiates us, and the two predictive picks are the most self-contained and the easiest to ground in synthetic SLOs. Deferred: Capacity Planner, Change Impact Predictor, Seasonality Learner, Root-Cause Predictor — higher integration cost (CAB, procurement, graph ML) for less demo value now.

**Effort estimate (range, not a point):** **8–13 engineer-weeks** for the five — Toil Detector & SLO Breach Predictor on the light end (existing metrics seam), Reliability Forecaster & Chaos Orchestrator on the heavy end (new model + safe-execution surface).

---

## Phase 4 — Productionize (Dec 1, 2026+)

Goal: the rollout plan's **Adaptive + RCA** stage — platform is multi-customer and self-improving.

- **Multi-tenancy + HA:** per-tenant isolation (data, policy, secrets), no shared `state.db`; HA for the gateway, gate, and state layer.
- **Observability of the platform itself:** the AIOps platform emits its own metrics/traces/logs; SLOs on the agents (latency, HITL override rate trend, hallucination rate <2%, guardrail violations = 0).
- **Closed-loop learning live:** champion/challenger by default, shadow eval before promotion, auto-rollback on regression (design principle #6). Policy Optimizer moves shadow → live.
- **Customer onboarding playbook:** connect 1–2 observability sources → baseline MTTA/MTTR/noise → enable agents license-by-license → first truth files written with the customer.
- **Pricing model decisions:** per-agent license (catalog is built for it) vs. platform + agent bundles vs. outcome-based (e.g. on noise reduction / MTTR). Decide before the first paid deal; the modular contract keeps all three options open.

---

*References: `docs/Adaptive_AIOps_Solution_Design.pptx` (rollout slide 11, KPI slide 12) · `docs/Adaptive_AIOps_Agent_Catalog.xlsx` (Phase-Summary sheet) · `CLAUDE.md` (phases, design principles). Review: dev-a (Chinmay) + project owner.*
