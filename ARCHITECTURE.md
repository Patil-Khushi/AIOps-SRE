# Architecture

Written for engineers integrating with this platform — the prose companion to the one-slide
picture in [`docs/Adaptive_AIOps_Unified_Architecture.pptx`](docs/Adaptive_AIOps_Unified_Architecture.pptx)
(the master diagram) and the phase/integration detail in
[`docs/Adaptive_AIOps_Solution_Design.pptx`](docs/Adaptive_AIOps_Solution_Design.pptx). When this
document and the deck disagree, this document describes what the code actually does today; the
deck describes the product vision. The repository layout this maps onto is in
[`CLAUDE.md`](CLAUDE.md).

> **Scope note.** This is a POC. Of the 30 catalog agents, six are built (all Reactive-Active
> plus the headline RCA Agent v0 and a HITL-demo Auto-Healer-lite). The platform *seams* are
> deliberately larger than these six agents need, because they are what the remaining agents
> plug into.

---

## 1. System overview

Adaptive AIOps is a vendor-neutral, multi-agent platform that turns an incoming alert into a
triaged, classified, ticketed, and (where safe) remediated incident. Agents are plain Python
functions; everything they touch the outside world through goes via one of four **platform
seams** under `aiops/` — the LLM gateway, the tool registry, the HITL policy gate, and the
state repository. The seams enforce the non-negotiables: no agent imports a vendor SDK, and no
agent self-gates a destructive action. The reference picture is the single architecture slide
in `docs/Adaptive_AIOps_Unified_Architecture.pptx`; everything below is that picture in words.

The runtime today is the four seams plus the agents. The six-component "Agentic Runtime" the
deck names (Planner, Router, Orchestrator, Memory, Tool Registry, Eval Harness) is partially
realized: **Tool Registry** (`aiops/tools`) and **Eval Harness** (`evals/`) exist; orchestration
is hand-wired through the demo server's `/api/triage` route rather than an orchestrator object
(see [ADR-002](docs/adr/0002-agent-framework-choice.md)).

---

## 2. Component contracts

### 2.1 Platform packages (`aiops/`)

**`aiops/llm` — LLM gateway.** The only place a model is called. Agents call `complete()` /
`acomplete()` with provider-agnostic `LLMRequest`/`LLMResponse` types; the active backend is
chosen by `AIOPS_LLM_PROVIDER` (anthropic / openai / ollama / stub) and pinned by
`AIOPS_LLM_MODEL`. *Inputs:* messages + model params. *Outputs:* `LLMResponse` (text, token
counts, provider). *Side effects:* a network call to the provider; `ping()` issues a cached
1-token health probe. *Error modes:* missing SDK or bad key → provider raises; callers
(RA-001 summary, RA-002 classify) catch and fall back to template / keyword output. See
[ADR-003](docs/adr/0003-default-llm-provider.md).

**`aiops/tools` — tool registry.** Agents reference *capabilities* (e.g. `itsm.incident.create`),
not vendors. `get_registry().call(capability, **kwargs)` dispatches to the registered provider
and **also invokes the HITL gate before the tool runs**. *Inputs:* capability id + kwargs.
*Outputs:* `ToolResult(ok, data, error, metadata)`. *Side effects:* whatever the provider does
(HTTP, kubectl, file write). *Error modes:* unknown capability → error; provider failure →
`ToolResult(ok=False, error=...)` (the registry does not raise for provider faults).

**`aiops/policy` — HITL gate + approval registry.** `get_gate().check(action, ctx)` returns a
`Decision(allowed, level, approval)` based on `DEFAULT_LEVELS` (None / Optional / Required).
For Required actions the gate calls an approver; the `ApprovalRegistry` opens a pending
`ApprovalRequest`, posts it across chatops surfaces, blocks until a human decides, and returns
the approver id (or `None` on deny/expire). *Inputs:* action id + context. *Outputs:* a
`Decision`. *Side effects:* chatops posts, an in-memory request store, audit-log lines. *Error
modes:* no approver / timeout → Required stays blocked (fail-closed). See
[ADR-004](docs/adr/0004-hitl-approval-surfaces.md) and [ADR-005](docs/adr/0005-policy-engine.md).

**`aiops/state` — persistence.** The only importer of SQLModel/SQLAlchemy. Agents and the
server read/write through `aiops.state.repository` (`save_verdict`, `save_classification`, …).
*Inputs:* domain objects (verdicts, classifications). *Outputs:* row ids / queried rows.
*Side effects:* writes to `AIOPS_STATE_DB_URL` (default `sqlite:///./data/state.db`,
auto-created). *Error modes:* DB unreachable → SQLAlchemy raises to the caller; swapping to
Postgres is a URL change, no agent edits.

### 2.2 Tool subpackages (`aiops/tools/*`)

- **`alerts/`** — normalizes raw monitoring payloads into a canonical `Alert` (Prometheus
  today; Datadog/CloudWatch/Alertmanager adapters are the planned multi-source surface).
- **`observability/`** — read-only queries to Prometheus (metrics/alerts) and Jaeger
  (traces/services). All `observability.*` capabilities are autonomy `NONE`.
- **`itsm/`** — ServiceNow provider for `itsm.incident.create`/`update`/`cmdb.lookup`; mock by
  default, real PDI when `AIOPS_USE_MOCK_ITSM=false`.
- **`chatops/`** — vendor-neutral `ChatOpsClient` with JSON-file, WebSocket, and Slack
  adapters; the delivery layer for notifications and HITL approval prompts.
- **`feature_flags/`** — flagd ConfigMap adapter (`feature_flags.set_variant`/`get_variant`/
  `reset_all`/`list_variants`); the only sanctioned path to mutate scenarios. See
  [ADR-001](docs/adr/0001-feature-flag-mutation-seam.md).

### 2.3 Agents (`agents/*`)

All Reactive-Active agents consume RA-001's `TriageVerdict`; none performs its own HITL check.

- **`alert_triage` (RA-001)** — `triage(alert) → TriageVerdict`. Steps 1–8: validate, normalize,
  dedup (embeddings or rule-based), correlate, severity, ownership (CMDB + on-call), summary
  (LLM or template). *Side effect:* persists the verdict. *Error modes:* LLM down → template
  summary; CMDB miss → Platform On-Call default; no `embeddings` extra → rule-based dedup.
- **`incident_classifier` (RA-002)** — `classify(alert, triage_verdict) → Classification` into
  one of five `IncidentType`s. Rule-based first pass, LLM consult only on no-match. *Side
  effect:* persists a classification row. *Error mode:* LLM error → Tier-4 keyword fallback.
- **`auto_ticketing` (RA-003)** — `ticket(verdict) → TicketRecord`. Skips Suppressed verdicts;
  maps severity → urgency; calls `itsm.incident.create` (OPTIONAL) and `notify.send`. *Error
  modes:* ServiceNow down → `ToolResult(ok=False)` captured in the trace, flow continues.
- **`notification_router` (RA-005)** — `decide(verdict) → RoutingDecision`, `route(...) →
  RoutingOutcome`. Chooses a channel by severity / time-of-day / ownership and posts via the
  chatops seam (autonomy `NONE`). *Error mode:* Slack down → JSON-file adapter still records.
- **`rca_agent` (PRS-008 ★)** — `analyze(RCAInput) → RCAVerdict` with ranked fix steps, each
  carrying `BlastRadius` + rollback and tagged `requires_hitl=true`. v0 is single-scenario
  (`slow-product-catalog`), pinned to Anthropic. *Side effect:* persists the verdict; **does
  not execute** fix steps. *Error mode:* LLM unavailable → low-confidence verdict.
- **`auto_healer_lite`** — `recommend_restart(...) → RestartRecommendation`. The HITL demo: it
  requests `automation.runbook.execute` (REQUIRED), so the gate blocks, chatops prompts, a
  human approves, then the restart runs. Exists to make platform-enforced HITL runnable.

---

## 3. Data flows

The canonical Reactive→Prescriptive path, and which seam each hop crosses:

```
Alert (monitoring)
  └─ alerts.normalize.* ───────────────► canonical Alert            [tools/alerts]
     └─ RA-001 triage() ───────────────► TriageVerdict              [llm + tools(cmdb/obs) + state]
        ├─ RA-002 classify() ──────────► Classification             [llm + state]
        ├─ RA-003 ticket() ────────────► TicketRecord               [tools: itsm.incident.create]
        │     └─ notify.send ──────────► chat message               [tools/chatops]
        └─ RA-005 route() ─────────────► RoutingOutcome              [tools/chatops]
  (Prescriptive)
  RCA analyze() ───────────────────────► RCAVerdict + fix steps     [llm; each step requires_hitl]
     └─ auto_heal/runbook execute ─────► gate.check() == REQUIRED   [policy]
            └─ ApprovalRegistry ───────► chatops prompt → human ──► approve/deny  [policy + chatops]
                  └─ action runs ──────► ToolResult                 [tools]
                        └─ audit ──────► chatops.jsonl + state.db    [chatops audit adapter + state]
```

Two persistence sinks: **`data/state.db`** (verdicts, classifications, runtime rows) and
**`demo/audit/chatops.jsonl`** (every notification + approval lifecycle event). Ground-truth
specs for scenarios live separately as YAML (see [ADR-007](docs/adr/0007-truth-files-vs-db.md)).

---

## 4. Failure modes (external integrations)

| Integration | Seam | When it's down / slow |
|---|---|---|
| **LLM provider** | `aiops/llm` | RA-001 falls back to a template summary; RA-002 to Tier-4 keyword classification; RCA returns a low-confidence verdict. `/api/health`'s `llm_ok` should flag it *before* the demo. Slow → `AIOPS_LLM_TIMEOUT` bounds the wait. |
| **ServiceNow (ITSM)** | `aiops/tools/itsm` | `itsm.incident.create` returns `ToolResult(ok=False)`; the trace records it and the flow continues. Set `AIOPS_USE_MOCK_ITSM=true` as the documented fallback (PDIs sleep/expire). CMDB miss → Platform On-Call default. |
| **Slack** | `aiops/tools/chatops` | The JSON-file adapter still records every message, so notifications and approvals are never lost — only the Slack surface goes dark. HITL approval can still be given via the dashboard. |
| **PagerDuty** | `aiops/tools/chatops` (planned) | Not on the Reactive critical path; a quota/down PD degrades on-call paging only. Be ready to skip in a live demo. |
| **flagd** | `aiops/tools/feature_flags` | Scenario inject/reset fail loudly via the registry; the running demo is unaffected (flags only change *which* failure is active). SSA conflicts are designed out (ADR-001). |
| **Prometheus / Jaeger** | `aiops/tools/observability` | Read-only queries return `ToolResult(ok=False)`; triage degrades to alert-payload-only (no metric/trace correlation). `AIOPS_PROMETHEUS_TIMEOUT` / `AIOPS_JAEGER_TIMEOUT` bound the wait. |

---

## 5. Scaling assumptions

**Demo load (what's built for):** one presenter laptop, Rancher Desktop k3s, a single FastAPI
process, SQLite, in-memory dedup, an **in-memory** approval registry, and synchronous
agent flows driven one request at a time. This is correct for a 6-minute walkthrough and a
handful of concurrent operators.

**Production load (what the seams allow, not yet built):** Postgres behind the same
`aiops.state` interface (URL swap); a persistent vector store for cross-incident recall
([ADR-006](docs/adr/0006-vector-store-choice.md)); OPA as the policy authority (ADR-005); an
orchestrator object replacing the hand-wired route (ADR-002); horizontal FastAPI replicas.

**Where the bottlenecks sit:** the in-memory `ApprovalRegistry` is single-process — multiple
server replicas would not share pending approvals (needs a shared store first); SQLite
serializes writes (the `verdict_id` counter and classification FK assume one writer);
synchronous LLM calls hold the request for the model's latency; the OTel-demo cluster runs
~94% committed on a 16 GiB host (the practical demo ceiling). None block the POC; all are
flagged in the [Risk Register](RISK_REGISTER.md) and the architect retrospectives.

---

## 6. Blast-radius caps and rollback paths

Required-HITL actions cannot run until a human approves through `aiops/policy` — that approval
gate **is** the primary blast-radius cap (fail-closed: no approver → blocked). Per-action caps
and rollback, from `DEFAULT_LEVELS` in `aiops/policy/gate.py`:

| Action (capability) | Level | Blast-radius cap | Rollback path |
|---|---|---|---|
| `rca.fix_step.execute` | REQUIRED | Each `RankedFixStep` carries an explicit `BlastRadius`; approval is per-step. | Each step ships a rollback in the verdict; **reverse must be tested once** (principle #5). v0 does not execute — recommend-only. |
| `automation.runbook.execute` | REQUIRED | Auto-Healer-lite scopes to a single named deployment restart. | k8s rollout restart is reversible via `kubectl rollout undo`; the runbook records the prior revision. |
| `auto_heal.execute` | OPTIONAL | Blast-radius caps + circuit breakers (catalog); tenant can require HITL. | Healing actions are dry-run-first and reversible by contract. |
| `chaos.experiment.run` | REQUIRED | Blast-radius caps, safe-mode library, auto-abort on adverse signals. | Experiment auto-aborts and reverts; out of POC scope. |
| `policy.optimize` | REQUIRED | Guardrailed A/B; change is config, not data. | Revert to prior threshold/policy version (Git). |
| `feedback.promote_model` | REQUIRED | Shadow-eval before promotion; champion/challenger. | Auto-rollback on regression to the prior champion. |
| `knowledge.publish` | REQUIRED | Draft-then-approve; no silent KB writes. | Unpublish / revert the KB article version. |
| `capacity.recommend` / `slo.freeze_changes` / `change.predict_risk` | REQUIRED | Advisory/predictive — approval gates the *acting* on them. | N/A (recommendation only); the acted-on change carries its own rollback. |

Actions at `NONE` (all `observability.*`, `notify.send`, `itsm.cmdb.lookup`,
`oncall.schedule.lookup`) are read-only or non-destructive and need no rollback. `itsm.incident.*`
are OPTIONAL — a created ticket is corrected by an update/close, not a destructive rollback.

> **POC reality:** of the Required actions above, only `rca.fix_step.execute` (recommend-only)
> and `automation.runbook.execute` (Auto-Healer-lite) are wired to real agents today. The rest
> are catalog placeholders in the level map so the gate is complete by design when those agents
> land — they are not yet callable.

---

## References

- [`docs/Adaptive_AIOps_Unified_Architecture.pptx`](docs/Adaptive_AIOps_Unified_Architecture.pptx) — the master picture.
- [`docs/Adaptive_AIOps_Solution_Design.pptx`](docs/Adaptive_AIOps_Solution_Design.pptx) — phase decomposition, integration matrix, HITL slide.
- [`CLAUDE.md`](CLAUDE.md) — repository layout, seams, non-negotiable principles.
- [`docs/adr/`](docs/adr/) — the decisions behind the choices described here.
- [`RISK_REGISTER.md`](RISK_REGISTER.md) — the operational and platform risks referenced in §5.
