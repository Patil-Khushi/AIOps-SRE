# Adaptive AIOps — Codebase Internals (Engineer's Deep Reference)

> **Companion to [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md).** That doc explains *what* the project is.
> **This** doc explains *how the code works inside* — how each agent is built, what it contains, what it calls,
> the complete backend flow, every tool and where it's used, and the cross-cutting machinery.
> File:line references are accurate to the current tree (cite to navigate, not to memorize).

## How to read this
- **Mental model:** *Agents* (in `agents/`) are thin, single-purpose units. They never touch a vendor SDK. They call **capabilities** through the **platform seams** (`aiops/`). The **demo server** (`demo/ui/`) is the HTTP/WebSocket boundary that drives agents and hosts the HITL flow.
- **The golden rule (enforced by a smoke test):** agent code imports only `aiops.llm`, `aiops.tools`, `aiops.policy`, `aiops.state`, `aiops.runtime`. Vendor SDKs (`anthropic`, `openai`, `httpx` to ServiceNow, k8s client) live **only** inside `aiops/`.

## Contents
1. [Repository anatomy](#1-repository-anatomy)
2. [Platform seam: LLM gateway](#2-platform-seam-llm-gateway-aiopsllm)
3. [Platform seam: Tool Registry (the HITL boundary)](#3-platform-seam-tool-registry-aiopstoolsregistrypy)
4. [Platform seam: HITL gate + approvals](#4-platform-seam-hitl-gate--approvals-aiopspolicy)
5. [Platform seam: Orchestrator](#5-platform-seam-orchestrator-aiopsruntimeorchestratorpy)
6. [Platform seam: State / Memory](#6-platform-seam-state--memory-aiopsstate)
7. [Platform seam: Runbook store](#7-platform-seam-runbook-store-aiopsrunbooks)
8. [Every tool capability (real / mock / seam)](#8-every-tool-capability-real--mock--seam)
9. [Chatops fan-out + adapters](#9-chatops-fan-out--adapters-aiopstoolschatops)
10. [The 12 agents — internals](#10-the-12-agents--internals)
11. [The demo server wiring](#11-the-demo-server-wiring-demouiserverpy)
12. [Cross-cutting flows (sequence walk-throughs)](#12-cross-cutting-flows)
13. [How an agent is built (the recipe)](#13-how-an-agent-is-built-the-recipe)
14. [Test internals (hermetic fixtures)](#14-test-internals-hermetic-fixtures)

---

## 1. Repository anatomy

```
aiops/                     # PLATFORM SEAMS — the only place vendor SDKs may be imported
├── llm/                   # provider-agnostic LLM gateway (complete/acomplete/ping)
│   ├── base.py            #   Message, LLMRequest/Response, get_provider, @register_provider, _REGISTRY
│   ├── gateway.py         #   complete()/acomplete() public API
│   ├── health.py          #   ping() with TTL cache
│   └── *_provider.py      #   anthropic / openai / ollama / stub
├── tools/                 # TOOL REGISTRY — every external action is a "capability"
│   ├── registry.py        #   @tool decorator, ToolRegistry, ToolResult, get_registry, call()
│   ├── mock_providers.py   #   mock itsm/oncall/notify/runbook/dependencies (CI/demo)
│   ├── oncall.py          #   db-backed oncall.schedule.lookup
│   ├── knowledge.py / itsm_close.py / rca_remediation.py   # REQUIRED-HITL seam executors
│   ├── observability/     #   prometheus.py, jaeger.py, grafana.py
│   ├── itsm/              #   servicenow.py + _demo_cmdb.py fallback
│   ├── feature_flags/     #   flagd adapter (K8s server-side apply)
│   └── chatops/           #   ChatOpsClient + models + adapters/* + war_room_bridge.py
├── policy/                # HITL gate + approval registry
│   ├── gate.py            #   HITLGate, AutonomyLevel, DEFAULT_LEVELS, Decision, GateError, check/enforce
│   └── approvals.py       #   ApprovalRegistry, ApprovalRequest, ApprovalRequester, listeners
├── runtime/orchestrator.py# run_reactive_flow(alert) → ReactiveFlowResult
├── state/                 # SQLite (SQLModel) — memory + RAG store
│   ├── __init__.py        #   get_engine, init_db, migrations, reset_engine_for_tests
│   ├── models.py          #   all SQLModel row classes
│   ├── repository.py      #   ~40 query functions
│   └── oncall_repository.py#  sticky + load-aware + expertise on-call selection
├── runbooks/              # file-backed runbook library (store.py + models.py)
└── auth/                  # token/middleware (bytecode-only in tree)
agents/<name>/             # each agent: agent.py, models.py, prompts.py?, evals/golden.json, helpers
demo/ui/                   # FastAPI server (server.py, knowledge_routes.py, chatops_ws.py, _alert_hub.py)
demo/dashboard/            # React SPA
evals/                     # eval harness + reports
tests/                     # 57 test files + conftest hermetic fixtures
```

---

## 2. Platform seam: LLM gateway (`aiops/llm`)

**Public API** (callers use only these):
- `complete(messages, model=None, max_tokens=1024, temperature=0.2, provider=None) -> LLMResponse` (`gateway.py:21`)
- `acomplete(...)` — async variant (`gateway.py:43`)
- `Message(role, content)` — frozen dataclass; role ∈ system/user/assistant/tool (`base.py:17`)
- `LLMResponse(text, model, provider, input_tokens, output_tokens, stop_reason, raw)` (`base.py:33`)
- `ping(force=False) -> dict` — health probe, 60 s success / 10 s failure TTL; never raises (`health.py:46`); backs `/api/health`.

**Provider selection** (`base.py:70` `get_provider`): explicit arg → `AIOPS_LLM_PROVIDER` env → default `anthropic`. Providers self-register via `@register_provider(name)` into `_REGISTRY`; lazy-imported (missing SDK → `RuntimeError`).

**Providers:**
| Name | Class | Backend | Notes |
|---|---|---|---|
| `anthropic` | `AnthropicProvider` | api.anthropic.com **or Azure AI Foundry** (if `ANTHROPIC_BASE_URL` contains `.azure.com`) | system messages passed via `system=` kwarg; RCA uses this |
| `openai` | `OpenAIProvider` | OpenAI **or Azure OpenAI** | reasoning models (gpt-5/o-series) use `max_completion_tokens`, no temperature |
| `ollama` | `OllamaProvider` | `OLLAMA_HOST` (local) | offline/local fallback |
| `stub` | `StubProvider` | none | deterministic echo; CI/tests |

**Caller pattern:** `await acomplete([Message("user","...")], max_tokens=..., temperature=...)`. The `max_tokens` is capped by `AIOPS_LLM_MAX_TOKENS_PER_CALL`. Per-agent override: RCA sets `AIOPS_RCA_LLM_PROVIDER=anthropic`, `AIOPS_RCA_LLM_MODEL=claude-sonnet-4-6`.

---

## 3. Platform seam: Tool Registry (`aiops/tools/registry.py`)

**This is the single HITL enforcement boundary.** Everything external goes through it.

- **`@tool(name, capability, provider, description="")`** (`registry.py:112`) — registers a function. `name` is globally unique; `capability` is the logical action (e.g. `itsm.incident.create`) that may have several providers (servicenow/mock/jira).
- **`ToolResult(ok, data=None, error=None, metadata={})`** (`registry.py:27`) — the universal return. HITL blocks come back as `ok=False, error="blocked by HITL gate: ...", metadata={"blocked_by":"hitl_gate","level":...}`.
- **`get_registry().call(capability, hitl_context=None, **kwargs) -> ToolResult`** (`registry.py:75`) — exact order:
  1. Resolve the **active** Tool for `capability` (`KeyError` if none).
  2. **`decision = get_gate().check(capability, hitl_context)`** ← the gate runs here.
  3. If not allowed → return blocked `ToolResult` **without calling the function**.
  4. **Filter kwargs** to the function's signature via `inspect.signature` (extra context like `incident_id` is silently dropped if the tool doesn't declare it).
  5. Call the function; wrap non-`ToolResult` returns; catch exceptions → `ok=False`.
- **`select_provider(capability, tool_name)`** (`registry.py:56`) — swap the active provider at runtime (e.g. mock → db on-call at startup). This is the vendor-neutrality lever.

**Why kwargs filtering matters:** callers pass a fat context (`incident_id`, `verdict_id`, …) to every capability; each tool function only receives the parameters it declares. No bloated signatures, no breakage when callers add context.

---

## 4. Platform seam: HITL gate + approvals (`aiops/policy`)

### `gate.py`
- **`AutonomyLevel`** = NONE / OPTIONAL / REQUIRED (`gate.py:27`).
- **`DEFAULT_LEVELS`** (`gate.py:110`) — the 25-capability map (mirrored in `policies/hitl.rego`). Fallback `AIOPS_HITL_DEFAULT` (default `optional`).
- **`Decision(allowed, level, reason, approver=None, approval=None)`** (`gate.py:78`) + **`ApprovalSummary(id,status,approver,reason)`** (`gate.py:39`).
- **`check(action, context) -> Decision`** (`gate.py:255`):
  - NONE → allowed immediately.
  - OPTIONAL → allowed unless `context["tenant_requires_hitl"]` → then call approver.
  - REQUIRED → always call the approver.
- **`enforce(action, context)`** (`gate.py:295`) — calls `check`, raises **`GateError(decision=d)`** if blocked. `GateError` carries `.decision` so the caller can report BLOCKED/PENDING without a second approver round-trip.
- **`set_approver` / `reset_approver`** (`gate.py:221`/`233`); default is **`_no_approver`** (fail-closed → blocks every REQUIRED action). Tests reset to this between cases.

### `approvals.py`
- **`ApprovalRegistry`** (singleton via `get_approval_registry()`, `:398`): `create()` (`:196`), `decide(approved, approver, reason)` (`:224`), `get()`, `list_pending/list_all`, `wait_for(id, timeout)` (`:323` — blocks until out of PENDING or timeout; never raises), `expire()`. Timestamps: `requested_at`, `expires_at`, `decided_at`. Statuses PENDING/APPROVED/DENIED/EXPIRED. Listeners fire **outside the lock** so chatops I/O doesn't serialize approvals.
- **`ApprovalRequester`** (`:417`) — the real `ApproverFn`. On a REQUIRED check it: honours `skip_approval` (eval) / `pre_authorized_by`, else `create()`s a request, writes `pending_approval_id` back into the caller's context, `wait_for()`s, and returns an `ApproverResult`.
- **Startup wiring:** `install_default_approver()` (`:508`) swaps `_no_approver` → `ApprovalRequester`. `install_chatops_listener()` (`:588`) posts approval prompts/outcomes through the chatops seam (interactive Slack buttons on "created").

**Timeout:** `AIOPS_HITL_APPROVAL_TIMEOUT` (default 600 s). Demo calls shorten it per-request.

---

## 5. Platform seam: Orchestrator (`aiops/runtime/orchestrator.py`)

**`run_reactive_flow(alert) -> ReactiveFlowResult`** (`:100`) — never raises (routing is non-fatal). Order:
1. `verdict, verdict_id = triage(alert)` (RA-001) — persists the verdict; `verdict_id` is None if persistence failed.
2. `classification = classify(ClassificationInput(alert, triage_verdict=verdict))` (RA-002).
3. **FK guard:** persist classification only if `verdict_id is not None`.
4. `ticket = auto_ticket(verdict, classification, alert_name=alert.metric)` (RA-003).
5. **try:** `outcome = route_notification(verdict)` (RA-005) → persist notification (FK-guarded, nested try) → **except: log & continue** (routing failure doesn't break the chain).
6. Return `ReactiveFlowResult(verdict, verdict_id, classification, classification_id, ticket, routing, deliveries, notification_id)`.

**`ReactiveFlowResult.to_api_dict()`** (`:73`) reproduces the legacy `/api/triage` JSON: `{verdict, ticket, classification, notifications, deliveries, persisted:{verdict_id,classification_id,notification_id}}`. State encoding: `routing=None` → RA-005 raised; `deliveries={}` → Suppressed verdict.

---

## 6. Platform seam: State / Memory (`aiops/state`)

- **Engine:** `get_engine()` (lazy singleton; `AIOPS_STATE_DB_URL`, default `sqlite:///./data/state.db`), `init_db()` (idempotent + additive migrations), `reset_engine_for_tests()`.
- **Tables** (`models.py`): see [PROJECT_OVERVIEW §12]. Embedding columns on `clusters`, `historical_incidents`, `kb_articles`.

**Repository function catalog** (`repository.py`):
- **Verdicts:** `save_verdict`, `find_recent_verdict_by_alert_id` (idempotency), `list_verdicts`, `get_verdict`.
- **Clusters (dedup):** `find_active_cluster`, `upsert_cluster` (append alert_id + persist centroid), `list_active_clusters`, `evict_expired_clusters`, `delete_all_clusters`, `clear_clusters_for_service` (scenario reset).
- **Assignment (on-call):** `find_last_assigned_engineer` (sticky), `count_recent_assignments` (load-aware tie-break).
- **Classification:** `save_classification` (FK to verdict, nullable), `get/list/count`, `average_classification_confidence`.
- **Historical incidents (RA-002 RAG):** `save_historical_incident` (L2-normalized embedding), **`nearest_historical_incidents(embedding, k=5, min_similarity=0.6)`** (brute-force cosine top-K), `delete_live_historical_incidents` (eval hook).
- **KB (PRS-007):** `save_kb_article`, `find_kb_by_incident_id` (idempotency), `update_kb_status`, **`nearest_kb_articles(...)`** (cosine, status-filtered, exclude self), `tag_kb_article_source`.
- **Executions (PRS-002):** `save_execution` (unique `request_id`), `list_executions`.
- **Notifications/Tickets/RCA:** `save_notification`, `save_ticket`, `save_rca_result`/`get_rca_result`.

**On-call selection** (`oncall_repository.py`): `find_oncall_for_team` (sticky → primary → secondary → manager → global wildcard, least-loaded within bucket) and `find_best_for_team_and_category` (expertise-weighted by matched failure sub-domain). See [PROJECT_OVERVIEW §14].

---

## 7. Platform seam: Runbook store (`aiops/runbooks`)

- **`Runbook`** model (`models.py`): id, title, service, version, tags, severity, source (seed/live), status (`ReviewStatus`: DRAFT/PENDING_REVIEW/PUBLISHED/REJECTED), related_kb, body (markdown).
- **Store API** (`store.py`): `list_runbooks`, `get_runbook`, `save_runbook(rb, bump_version=False)`, `search_runbooks(service, query, status)`, `seed_from_dir(overwrite=False)`, `ensure_seeded` (seed only if empty), `delete_*`. One markdown file per runbook (`<id>.md`, YAML frontmatter + body). Dir from `AIOPS_RUNBOOKS_DIR` (default `data/runbooks`).

---

## 8. Every tool capability (real / mock / seam)

`R` = hits a real external system · `M` = mock/in-memory · `S` = HITL-gated seam executor.

| Capability | Provider(s) | R/M/S | What it does |
|---|---|---|---|
| `observability.metrics.query` | prometheus | R | `GET {PROM_URL}/api/v1/query` (instant PromQL) |
| `observability.metrics.alerts` | prometheus | R | `GET /api/v1/alerts` (firing/pending) |
| `observability.metrics.render_panel` | grafana | R | render dashboard panel → PNG bytes (ticket attachment) |
| `observability.traces.services` | jaeger | R | list services (circuit-breaker 30 s on failure) |
| `observability.traces.search` | jaeger | R | recent traces for a service |
| `itsm.incident.create/update/get/query` | servicenow / mock | R/M | ServiceNow incident CRUD (`/api/now/table/incident`) |
| `itsm.incident.attachment.add` | servicenow | R | attach Grafana PNG to incident |
| `itsm.cmdb.lookup` | servicenow / mock | R/M | service → team + runbook; **falls back to `_demo_cmdb`** on PDI miss / empty team |
| `itsm.cmdb.dependencies` | mock | M | service → downstream deps (OTel call graph) |
| `oncall.schedule.lookup` | **db** / mock | R/M | team → on-call engineer (DB activated at startup; mock = `oncall@<team>.example.com`) |
| `notify.send` | mock | M | no-op (RA-005 uses chatops seam instead) |
| `feature_flags.set_variant` | flagd | R | **K8s server-side apply** to `flagd-config` ConfigMap (the only sanctioned flag mutation) |
| `feature_flags.get_variant/list_variants/reset_all` | flagd | R | read / batch-reset flags |
| `automation.runbook.simulate` | mock | M | dry-run preview (NONE) |
| `automation.runbook.apply` | mock | M | non-destructive step (NONE) — never mutates |
| `automation.runbook.execute` | mock | M+S | destructive step (REQUIRED); **hybrid** — `reset_feature_flag` action calls the real `feature_flags.set_variant` after approval |
| `rca.fix_step.execute` | seam | S | REQUIRED — `set_flag` → real flag flip; `rollback_deploy`/`manual` → "no executor" |
| `knowledge.publish` | seam | S | REQUIRED — set KB status PUBLISHED + write runbook |
| `itsm.ticket.close` | seam | S | REQUIRED — two PATCHes (Resolved → Closed) |
| `auto_heal.lite.execute` | (gate action) | S | REQUIRED — PRS-002 enforces this before dispatching the option's tool |
| `chatops.war_room.create` | slack | R | create Slack channel + invite + Jitsi link (simulates without token) |

**Provider switch:** `AIOPS_USE_MOCK_ITSM=true` (CI default) registers the mock ITSM/oncall/notify set; the real providers register otherwise. `_activate_db_oncall_provider()` flips on-call to the DB provider at startup if the roster is non-empty (auto-seeds it now).

**Key env vars:** `AIOPS_PROMETHEUS_URL`, `AIOPS_JAEGER_URL`(+prefix/timeouts), `AIOPS_GRAFANA_URL`/`_API_KEY`, `AIOPS_SERVICENOW_INSTANCE_URL`/`_USER`/`_PASSWORD`/`_RUNBOOK_FIELD`/`_RESOLVED_STATE`/`_CLOSED_STATE`, `AIOPS_FLAGD_NAMESPACE`, `AIOPS_SLACK_WEBHOOK_URL`, `AIOPS_SLACK_BOT_TOKEN`, `AIOPS_SLACK_USER_MAP_JSON`, `AIOPS_PAGERDUTY_INTEGRATION_KEY`, `AIOPS_JITSI_BASE`.

---

## 9. Chatops fan-out + adapters (`aiops/tools/chatops`)

**`ChatOpsClient.send(msg) -> dict[name, DeliveryResult]`** (`client.py:54`) — fans one `ChatMessage` to **every** registered adapter; per-adapter exceptions are caught (one broken sink never blocks others); each `DeliveryResult` carries `ok/error/latency_ms`. Singleton via `get_client()`.

**`ChatMessage`** (`models.py:51`): channel, severity (INFO/P3/P2/P1/P0), title, body, incident_id, service, category_display, mentions, **actions** (e.g. `page_oncall`), **response_mode** (page/notify/log), assignee/assignee_name/assignee_email, optional `interactive` (approval prompt). `to_record()` (`:116`) is the canonical wire shape (used by every adapter + the WS feed + the audit log; its key-set is contract-tested).

**Adapters (`adapters/`):**
| Adapter | Fires when | Backend / env |
|---|---|---|
| `jsonfile` | every message | append JSONL → `demo/audit/chatops.jsonl` (audit) |
| `websocket` | every message | pushes to `/ws/chatops` (dashboard Notifications feed) |
| `slack` (webhook) | every message | `AIOPS_SLACK_WEBHOOK_URL`; Block Kit, severity colors, **mention rewrite** `@handle`→`<@U…>` via `slack_users.json`, **interactive approve/deny buttons** |
| `slack_bot` (DM) | `page_oncall` in actions or `response_mode=page` (DM all); `response_mode=notify` (DM assignee only) | `AIOPS_SLACK_BOT_TOKEN`; `chat.postMessage` to user DM |
| `pagerduty` | `page_oncall` **and** severity ≥ P2 | `AIOPS_PAGERDUTY_INTEGRATION_KEY`; Events API v2; **non-blocking daemon thread**, dedup by incident_id, 1 retry |

**War-room bridge** (`war_room_bridge.py`, capability `chatops.war_room.create`): `conversations.create` → `conversations.invite` → `chat.postMessage` (context pack + Jitsi link); degrades to a *simulated* room without a token. Mention resolution shared via `_slack_user_map.py` (`load_slack_user_map` merges committed `slack_users.json` + `AIOPS_SLACK_USER_MAP_JSON` env, env wins). Secrets are redacted in every adapter `__repr__`.

**Registration:** `register_env_adapters(audit_path=...)` reads the env at startup and registers whichever sinks are configured (audit always on).

---

## 10. The 12 agents — internals

Each agent: **files · public entrypoint · key helpers · seams/capabilities · LLM/embeddings · fallback.**

### RA-001 Alert Triage (`agents/alert_triage/`)
- **Files:** agent.py, models.py, prompts.py.
- **Entry:** `triage(alert: Alert) -> tuple[TriageVerdict, int|None]` (`agent.py:~736`).
- **Helpers:** `_dedup` (cluster_key match → embedding cosine ≥0.85 → new cluster; EMA centroid α=0.2), `_fetch_metric_context`/`_fetch_trace_context` (ThreadPoolExecutor → Prometheus/Jaeger), `_classify_severity_rule_based` then `_classify_severity_llm`, `_resolve_team` (CMDB), `_resolve_on_call`, `_render_summary` (LLM/template), idempotency via `find_recent_verdict_by_alert_id` (30 s).
- **Seams:** llm.complete (severity, summary); registry: `observability.metrics.query`, `observability.traces.search`, `itsm.cmdb.lookup`, `oncall.schedule.lookup`; state: cluster + verdict persistence.
- **LLM/emb:** SentenceTransformer `all-MiniLM-L6-v2` for dedup (lazy, optional); Claude for severity/summary. **Fallback:** rule-based severity + template summary.

### RA-002 Incident Classifier (`agents/incident_classifier/`)
- **Files:** agent.py, models.py, prompts.py, `_seed.py`.
- **Entry:** `classify(payload: ClassificationInput) -> Classification`.
- **Pipeline:** seed-if-empty → embed → `nearest_historical_incidents(k=5, min_sim=0.6)` → **4-tier decide** (Tier-1 sim≥0.85 + top-3 agree, no LLM; Tier-2 LLM+evidence; Tier-3 LLM cold; Tier-4 keyword) → re-query CMDB itself → persist new incident (learns).
- **Seams:** llm.complete; registry: cmdb.lookup, oncall.schedule.lookup, cmdb.dependencies; state: nearest/save historical.
- **LLM/emb:** embeddings + Claude. **Fallback:** keyword rule (Tier-4).

### RA-003 Auto-Ticketing (`agents/auto_ticketing/`)
- **Files:** agent.py, models.py, `grafana_panels.json`.
- **Entry:** `ticket(verdict, classification=None, *, alert_name=None) -> TicketRecord`.
- **Flow:** suppressed → skip; severity→urgency (1/2/3); build payload; `itsm.incident.create` (continue on error); optional Grafana panel attach (`render_panel` + `attachment.add`); `notify.send`.
- **Helpers:** `_panel_for` (lazy panel map), `_safe_attachment_filename`. **LLM/emb:** none.

### RA-004 Runbook Executor (`agents/runbook_executor/`)
- **Files:** agent.py, models.py, `selector.py`, `library.py`, `runbooks/*.md`.
- **Entry:** `execute_runbook(incident, runbooks_dir=None, hitl_context=None)`; also `select(incident)`, `run_plan(...)`.
- **Flow:** `select_runbook(service→tags→severity)` → simulate each step → per step: destructive→`automation.runbook.execute` (REQUIRED), else `automation.runbook.apply` (NONE) → gate-blocked → status `denied`, stop → tool fail → roll back prior steps → status `rolled_back/failed`; else `resolved`. `_blocked_by_gate` checks `metadata.blocked_by=="hitl_gate"`. **LLM/emb:** none.

### RA-005 Notification Router (`agents/notification_router/`)
- **Files:** agent.py, models.py.
- **Entry:** `decide(verdict, now=None) -> RoutingDecision` (pure); `route(verdict) -> RoutingOutcome` (emits).
- **Logic:** business hours (UTC 9–18) + severity → page/notify/log; `_resolve_oncall` (one `oncall.schedule.lookup`, with category keywords for sub-domain); channel `team-<slug>`; `_render_body`. **LLM/emb:** none (rule-based). **Fallback:** silent on oncall miss; still routes to team channel.

### RA-006 War-Room Assembler (`agents/war_room_assembler/`)
- **Files:** agent.py, models.py.
- **Entry:** `decide(verdict)` (pure); `assemble(verdict) -> WarRoomOutcome` (real Slack).
- **Flow (Sev-1/2 only):** on-call SME → channel `war-room-<id>` → context pack (parallel metrics+traces) → invite → opening post + Jitsi → seed timeline. **Fallback:** context items degrade to "unavailable"; simulates room without a token. **LLM/emb:** none.

### RA-007 Log Correlation (`agents/log_correlation/`)
- **Files:** agent.py, models.py, prompts.py.
- **Entry:** `correlate(payload: CorrelationInput, force_synthetic=False) -> CorrelationResult`.
- **Pipeline:** resolve topology (payload or `itsm.cmdb.dependencies`) → parallel fetch logs/traces/metrics → synthetic fallback if empty → rule-based correlate (`_fingerprint` masks ids; timeline; first-error; `_error_rate_spike` threshold 3; topology-aware suspects) → LLM summary (template fallback). **Seams:** llm + observability + cmdb.dependencies. **Provenance** flagged live/synthetic/mixed.

### RA-008 Incident Commander (`agents/incident_commander/`)
- **Files:** agent.py, models.py.
- **Entry:** `command(alert, scenario_id=None, emit_comms=True) -> IncidentCommandResult`.
- **Flow:** `run_reactive_flow(alert)` → **correlate = traced placeholder (RA-007 not yet wired here)** → severity gate (engage only Sev-1/2) → `rca_analyze(verdict)` (read-only) → `_emit_coordination` (context pack + human-IC handoff to `#incidents`) → postmortem seed. Never executes a fix. **LLM/emb:** none directly (RCA's LLM downstream).

### PRS-008 RCA Agent ★ (`agents/rca_agent/`)
- **Files:** agent.py, models.py, prompts.py, `remediation_map.py`.
- **Entry:** `analyze(triage_verdict, scenario_id=None, correlation=None) -> RCAVerdict`.
- **Pipeline:** render prompt (verdict + optional correlation) → LLM JSON-mode (Claude Sonnet 4.6 via `AIOPS_RCA_LLM_PROVIDER`) → `_extract_json_object` → validate → **force `requires_hitl=True` on every step** → `_fallback_verdict` for locked scenario `slow-product-catalog` / unparseable. `flag_for_service` maps service→flag. **Only `set_flag` is auto-executable** (via `rca.fix_step.execute`).

### PRS-001 Remediation Recommender (`agents/remediation_recommender/`)
- **Files:** agent.py, models.py, `remediation_catalog.py`.
- **Entry:** `recommend(input: RemediationInput) -> RemediationVerdict`. **Pure, no LLM, no I/O.**
- **Pipeline:** options from RCA steps (`_option_from_rca_step`) + catalog mitigations (`patterns_for_cause`) → composite score `(6−blast_score)*10 + confidence*5 + rollback_bonus + env_bonus` → sort → `recommended_option_id = options[0]`; `auto_pick_eligible=False`. Tool capability inferred from action (`set_flag`→`feature_flags.set_variant`, etc.).

### PRS-002 Auto-Healer Lite (`agents/auto_healer_lite/`)
- **Files:** agent.py, models.py.
- **Entries:** `execute(request: ExecutionRequest) -> ExecutionVerdict` (generic); `recommend_restart(...)` (legacy HITL-1 path).
- **Flow:** `_validate_option` (requires_hitl, option_id, tool_capability) → REFUSED if bad → `get_gate().enforce("auto_heal.lite.execute", ctx)` → GateError → PENDING/BLOCKED → if allowed + dry_run → DRY_RUN_OK (would_execute) → if allowed + live → `registry.call(tool_capability, **tool_args)` → EXECUTED/EXECUTION_FAILED → `_finalise` persists `ExecutionRow`. **Only flag-flips truly execute today.**

### PRS-007 Knowledge Synthesizer (`agents/knowledge_synthesizer/`)
- **Files:** agent.py, models.py, prompts.py, `redaction.py`, `snow_watcher.py`, `seed_runbooks/`.
- **Entry:** `synthesize(bundle, scenario_id=None) -> SynthesisResult`.
- **Pipeline:** idempotency (`find_kb_by_incident_id`) → timeline → postmortem (LLM/`_deterministic_postmortem`) → runbook suggestion (new/update) → KB article → **redact PII/secrets** → quality score + dedup (`_dedup_check`: cosine or signature overlap) → `save_kb_article(status=pending_review)`. Publication gated by `knowledge.publish` (REQUIRED). `snow_watcher.start_watcher()` polls ServiceNow for resolved tickets → auto-synthesize.

---

## 11. The demo server wiring (`demo/ui/server.py`)

### Lifespan startup (10 ordered hooks, `lifespan` ~`:248`)
1. `init_db()` — schema + migrations.
2. `_warn_if_approval_token_unset()` — HITL-2 security check.
3. `_activate_db_oncall_provider()` — **auto-seeds roster if empty**, then flips on-call to the DB provider.
4. `install_chatops_listener()` — approval events → chatops.
5. `install_default_approver()` — fail-closed HITL approver.
6. `_register_chatops_adapters()` — JSONL audit + Slack + PagerDuty.
7. `bootstrap_websocket_adapter()` — attach the asyncio loop to the chatops WS hub.
8. `_ensure_hitl_agent_pool()` — recreate the daemon pool if a prior shutdown closed it.
9. `_start_auto_triage()` — background poller (gated by `AIOPS_AUTO_TRIAGE_ENABLED`).
10. `start_watcher()` — SNOW resolved-ticket watcher (gated by `SNOW_WATCHER_ENABLED`).

### Background machinery
- **`_AutoTriageLoop`** (~`:668`): every `AIOPS_AUTO_TRIAGE_INTERVAL_SECONDS` (default 3 s) calls `live_alerts()`, dedups by `alert_id`, triages fresh ones with `asyncio.gather`. `mark_seen(id)` (inject calls this so it doesn't double-fire), `forget_all()` (reset-all calls this).
- **`_DaemonThreadPoolExecutor`** (~`:1202`, 8 workers): daemon threads so the process can exit even while a worker blocks on the HITL gate (up to 900 s).
- **`_BoundedOutcomeStore`** (`_HITL_OUTCOMES`, ~`:1163`): LRU dict (max 100, thread-locked) holding async agent outcomes for polling.

### Endpoint → agent/orchestrator map
| Endpoint | Calls | Agent(s) |
|---|---|---|
| `POST /api/triage` | `run_reactive_flow(alert)` | RA-001→002→003→005 |
| `POST /api/triage-full` | reactive + `rca_analyze` + `remediate` | + PRS-008 + PRS-001 |
| `POST /api/rca` | `rca_analyze(verdict)` | PRS-008 |
| `POST /api/remediation` | `remediate(input)` | PRS-001 |
| `POST /api/execute` | `auto_heal_execute(req)` (sync) | PRS-002 |
| `POST /api/demo/auto-heal/execute` | `auto_heal_execute` on pool → `_HITL_OUTCOMES` | PRS-002 (async, non-blocking) |
| `POST /api/demo/auto-heal/restart` | `recommend_restart` on pool | PRS-002 (legacy) |
| `POST /api/incident-commander` | `command(alert)` | RA-008 |
| `POST /api/demo/runbook-executor/run` | `select` + simulate sync, `execute_runbook` on pool | RA-004 |
| `POST /api/war-room/assemble` | `decide`/`assemble` | RA-006 |
| `POST /api/synthesize`, `/api/kb/*` | `synthesize`, KB CRUD/publish | PRS-007 |
| `GET /api/live-alerts` | Prometheus alerts + synthetic merge | (pipeline) |
| `POST /api/scenarios/{id}/inject` | flip flag + background triage chain | (entry) |
| `GET /api/demo/auto-heal/outcome/{id}` | poll `_HITL_OUTCOMES` | (HITL poll) |

### The demo HITL async pattern (used by runbook-exec, auto-heal)
```python
approval_id = _uuid_hex()
ctx = {"approval_id": approval_id, "approval_timeout_seconds": req.timeout_seconds}
def _run(): _HITL_OUTCOMES[approval_id] = agent(..., hitl_context=ctx).model_dump(mode="json")
_HITL_AGENT_POOL.submit(_run)
return {"approval_id": approval_id, "status": "pending"}   # browser polls /outcome/{id}
```
This is why the browser never blocks for the (up to 600 s) approval window.

---

## 12. Cross-cutting flows

### A. A tool call through the HITL boundary
```
agent → get_registry().call(cap, hitl_context, **kw)
  → by_capability(cap) → Tool
  → get_gate().check(cap, ctx)
       REQUIRED → ApprovalRequester → registry.create() → listeners post Slack/WS prompt
                → wait_for() blocks → human approve/deny → ApproverResult
  → not allowed? ToolResult(ok=False, blocked_by=hitl_gate)   [function NEVER called]
  → allowed? filter kwargs → fn(**accepted) → ToolResult
```

### B. Reactive flow → persistence (the `/api/triage` path)
```
POST /api/triage → run_reactive_flow(alert)
  triage → (verdict, verdict_id)  [save VerdictRow]
  classify → Classification        [save if verdict_id]
  auto_ticket → TicketRecord
  try route_notification → RoutingOutcome [save NotificationRow if verdict_id] except: log+continue
  → ReactiveFlowResult.to_api_dict()
```

### C. RCA → fix (the headline path)
```
/api/rca → rca_analyze → RCAVerdict (steps requires_hitl=True)
operator → /api/demo/rca/apply-fix (or Auto-Healer page)
  → enforce(rca.fix_step.execute | auto_heal.lite.execute) → approval prompt
  → approve → feature_flags.set_variant (real flag flip) → service heals
  → Resolution Verifier re-checks → enforce(itsm.ticket.close) → approve → close
```

### D. Inject → alert on screen
See [PROJECT_OVERVIEW §10]: flag flip → (real Prometheus alert *or* UI-synthesized alert) → merged in `/api/live-alerts` → pushed via `/ws/alerts` every 5 s; inject also fires the triage chain in the background.

---

## 13. How an agent is built (the recipe)

Every agent follows the same contract — copy this when adding one:

1. **Package:** `agents/<name>/` with `agent.py`, `models.py`, optional `prompts.py`/helpers, and **`evals/golden.json`** (the eval harness auto-discovers it).
2. **Models:** Pydantic input + output. Outputs carry an `audit_metadata.decision_trace` (a list of human-readable "why" lines appended per stage). Invariants are enforced at the type level (e.g. `requires_hitl: Literal[True]`).
3. **Entrypoint:** a pure-ish function `do(input) -> output` (+ a `run(input: dict) -> dict` for the eval harness).
4. **Only import seams:** `aiops.llm`, `aiops.tools.get_registry()`, `aiops.policy.get_gate()`, `aiops.state`, `aiops.runtime`. **Never** a vendor SDK (smoke test fails the build otherwise).
5. **No self-gating:** never write `if approved:` in agent code. Call a REQUIRED capability and let the platform gate enforce it.
6. **Graceful degradation:** every external call can fail — provide a rule-based/template/deterministic fallback so the agent always returns a valid output.
7. **Couple only by schema:** read upstream agents' outputs as **dicts** (their JSON contract), not by importing their Python classes — keeps each agent independently sellable.
8. **Build the eval set the same week** (principle #7). A prompt change = a model change → re-run evals.

---

## 14. Test internals (hermetic fixtures)

`tests/conftest.py` has **7 autouse fixtures** that isolate process-global state so 57 test files don't bleed into each other:
- `_hermetic_state_db` — fresh temp SQLite per test (+ reset engine + dedup cache).
- `_hermetic_gate_approver` — reset gate to `_no_approver` at both ends.
- `_hermetic_llm_provider` — pin `stub` (defeats `.env` leak).
- `_hermetic_jaeger_circuit` — reset the Jaeger circuit breaker.
- `_hermetic_chatops_hub` — clear the WS history ring.
- `_hermetic_slack_user_map_env` — clear `AIOPS_SLACK_USER_MAP_JSON` / `AIOPS_ONCALL_ROSTER_JSON`.
- `_disable_auto_triage` — set `AIOPS_AUTO_TRIAGE_ENABLED=false`, `SNOW_WATCHER_ENABLED=false`, `AIOPS_ONCALL_AUTOSEED=false`.
- At import: observability URLs pinned to `127.0.0.1:1` (fail-fast) and embeddings disabled (replace `_get_embed_model` with `None`).

**CI** (`.github/workflows/ci.yml`): `ruff check` + `ruff format --check` + `pytest` + eval gate `--min-pass-rate 0.85` + `opa fmt/check`.

---

*Companion to [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md). When the code changes, update both. The `docs/` design files remain the authoritative source for contracts; this file documents the implementation as built.*
