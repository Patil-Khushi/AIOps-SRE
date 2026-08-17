## Update: addressing review feedback

- **Disclosure** — added the "Microsoft Teams / runbook-publishing integration" section below, describing the previously-undescribed Teams feature bundled into this PR (see that section for the full security review response).
- **CI fix** — `agents/rca_agent/investigation/facts.py` and `tests/test_rca_chat.py` now pass `ruff format --check`.
- **Security hardening** — `teams_meeting.py` and `scripts/publish_runbooks.py` now enforce the same `is_teams_webhook_url()` allowlist `teams.py`/`teams_dm.py` already did.
- **Consistency** — `investigation/pipeline.py`'s topology-resolver fallback now logs at `debug` on failure, matching its sibling catch block a few lines away (previously a silent `pass`).

## Summary

This PR covers a full pass on the RCA Agent's evidence accuracy, adds a read-only Historical Incident RAG capability, redesigns the RCA chatbot to be conversational, wires up real resolution verification, and rebuilds the Incident Command Center frontend (incident list, workspace, HITL remediation, chat dock).

## Backend

### Evidence accuracy fixes
- **Pod-restart/CPU/memory evidence scoping** — `evidence.py`'s Prometheus queries for pod restarts and resource saturation were namespace-wide, so an unrelated pod's stale crash could outrank the actually-affected service's own evidence. Added `pod_belongs_to_service()` filtering so only the affected service's own pods are ever cited.
- **Cluster-level datastore readiness** — added a `kube_statefulset_status_replicas_ready` check (`datastore_health`) that's independent of the affected service's own dependency gauge. This matters when the affected service itself is crash-looping and can't report its own gauge (e.g. `user-service` down because MySQL is down — `user-service` can't say "MySQL unreachable" if it's not running at all).
- **Log evidence (Loki)** — fixed a `service_name` label mismatch (`ecommerce-<service>` vs the bare service name the agent uses internally) that silently made every log query return nothing. Added `otel_service_name()` normalizer shared with the trace fix below.
- **Trace evidence (Jaeger)** — added `trace_health()`: error-status detection (`has_error` per span) and latency, wired into the `application_error` and `latency_regression` catalog rules as a third, independent evidence source (alongside metrics and logs).
- **Topology** — the topology provider chain (`cmdb → mock`, with `otel`/`snow`/`k8s` tiers available) was fully built but never called from the live RCA pipeline, only from the opt-in Context Engineering Layer. Wired a live fallback into `build_scope()` so blast radius now resolves real dependencies instead of always reporting `topology_available: false`.
- **Root-cause narrative fix** — the LLM was citing test-artifact-shaped evidence labels (e.g. `reason=injected_500`) faithfully, but then narrating the cause as "an active fault injection" — technically true of this demo, but not how a real incident would ever be described, and inconsistent with the platform's own "never tell the model how faults come about" design. Added a prompt clause: quote the label as evidence, but describe the underlying cause in genuine engineering terms (e.g. "an application-level fault, consistent with an unhandled exception").
- **Severity question** — the chatbot's "what severity is this incident?" was answered as if unanswerable (severity lives on `Investigation.scope`, which was never rendered into the model's context). Added it to the always-on context header plus a deterministic `severity` intent as a fast-path fallback.
- **Conversational abstention phrasing** — `_abstain()` used to return an empty `answer` string and let the UI paper over it with a technical "Not answerable from this investigation" banner + a raw internal `missing` reason. Abstention now always returns a real sentence ("I don't have that information available for this incident."), and the UI no longer needs (or shows) a robotic banner for ordinary Q&A.

### Historical Incident RAG (`agents/rca_agent/incident_rag.py`, `investigation_context.py`, `chat.py`)
- Read-only, embedding-based similarity search over **persisted RCA verdicts** (real past incidents this deployment has actually seen), not the eval truth-file corpus — keeps the demo narrative honest.
- Eligibility filter: only `confirmed`/`probable` outcomes are searchable; similarity threshold (`AIOPS_RCA_RAG_MIN_SIMILARITY`, default 0.55) — no blind top-K, an honest "no sufficiently similar incident was found" when nothing clears the bar.
- Results are always labeled `HISTORICAL — NOT CURRENT EVIDENCE` and structurally cannot influence confidence/root-cause status — `HistoricalIncidentRef` has no such field, and it's always server-attached post-hoc, never parsed from the model's own JSON output.
- AST boundary tests enforce the chat surface can import only this one narrow read-only accessor from `aiops.tools.*` — verified against the existing `test_rca_chat_boundary.py` ratchet, including a deliberate carve-out for the Teams-share endpoint (which lives in its own module, `demo/ui/rca_share_routes.py`, outside the restricted boundary, since sharing a message isn't remediation or a boundary risk).

### Resolution verification lifecycle
- `resolution_verifier.Verifier` gained `status_for(incident_id)` (in-progress / not-triggered / pass / fail) and a module-level `get_status()`, plus a new `GET /api/rca/verify-status/{incident_id}` endpoint — previously there was no way for anything to ask "is verification still running?"
- This closes a real gap: the Incident Workspace's lifecycle bar had `Verifying`/`Resolved` stages that were structurally unreachable (nothing ever told `deriveLifecycle()` about them), and separately, HITL "success" was being treated as "resolved" with no actual verification step surfaced anywhere.

### Real-time progress + chat session infra
- `agents/rca_agent/progress.py` + `demo/ui/rca_progress.py` — SSE progress streaming for a live RCA run, per-`run_id` isolated channels.
- `demo/ui/rca_chat_routes.py` + `rca_sessions.py` — the RCA chat HTTP surface (send/history/by-incident/delete), session store with turn caps and length limits.
- `demo/ui/rca_share_routes.py` — Teams webhook sharing for a chat answer/postmortem draft, reusing the existing `TeamsWebhookAdapter` (no new HTTP client).

## Frontend (`demo/dashboard/`)

### Incident Command Center
- New incident list, KPI strip, and expandable incident workspace (7 tabs: Hypotheses, Evidence, Timeline, Blast Radius, Changes, History, Verification).
- **Blast radius** now renders as an actual tree diagram (root → dependency nodes, colored/dashed by state) instead of a flat text list.
- **Incident list fixes**: dedupe by normalized service name (`order-service` and `ecommerce-order-service` are the same real service and were defeating dedup before), and only show incidents whose alert is **currently firing** — a resolved/recovered fault no longer lingers in the list forever.
- **Lifecycle bar**: wired `Verifying`/`Resolved` stages to the new verification-status polling; resets correctly on re-run.

### RCA Chat Dock
- Full visual redesign: header with severity/summary context, confidence + "pattern match" badges on historical answers, Copy/Teams/Postmortem action row per answer, slash-command shortcuts (`/cause` `/verify` `/similar`).
- **UX**: floating chat-bubble launcher (bottom-right, like a normal website chat widget) and a smooth pop-open transition anchored to the launcher, replacing the old instant full-height sidebar.
- **Bug fixes**: composer now auto-grows instead of clipping long auto-typed prompts behind a scrollbar; chat panel is now a fixed-height scrollable region so asking questions no longer grows the whole page; fixed a race condition where switching incidents quickly could let a stale response overwrite the new incident's chat thread.

## Microsoft Teams / runbook-publishing integration (scope disclosure)

This PR also bundles a separate, previously-undescribed feature that landed via a `wip`-titled commit: a full Teams integration used by the Notification Router (RA-005+006) and Knowledge Synthesizer's runbook publishing. Disclosing it properly here rather than splitting it into its own PR, since the commit is already pushed and under review:

- **`aiops/tools/chatops/adapters/teams.py`** — channel notification via Adaptive Card, posted to a Power Automate/Logic Apps webhook.
- **`aiops/tools/chatops/adapters/teams_dm.py`** — the same, but a 1:1 DM to a specific person (used for direct on-call paging rather than a channel post).
- **`aiops/tools/chatops/adapters/_teams_common.py`** — shared helpers: webhook host allowlisting, placeholder-email detection, Adaptive Card mention builder.
- **`aiops/tools/chatops/teams_meeting.py`** — creates a real Teams meeting (calendar invite + join link) for Sev-1/2 war rooms, via the same Power Automate flow pattern.
- **`aiops/tools/chatops/runbook_attachment.py`** — attaches a runbook's rendered steps to a Teams message.
- **`scripts/publish_runbooks.py`** — a one-off CLI to push the runbook library to wherever `AIOPS_RUNBOOK_PUBLISHER_URL` points, so runbook links resolve to something real instead of plain text.
- Wiring into `notification_assembler` (so RA-005+006 can route through Teams) and `aiops/tools/chatops/__init__.py` (adapter registration), plus `docs/dev_env_no_wsl_options.md`.

**Security posture** (addressing the review's specific findings):
- Webhook URLs are read from env vars only (`AIOPS_TEAMS_WEBHOOK_URL`, `AIOPS_TEAMS_MEETING_WEBHOOK_URL`, `AIOPS_RUNBOOK_PUBLISHER_URL`), never hardcoded or logged.
- `teams.py`/`teams_dm.py` already sanitized `HTTPStatusError` (the raw exception embeds the signed URL) — confirmed correct.
- **Fixed in this update**: `teams_meeting.py` and `scripts/publish_runbooks.py` now also enforce `is_teams_webhook_url()`'s hostname-suffix allowlist (`.logic.azure.com`, `.api.powerplatform.com`, `.webhook.office.com`) before posting — previously only `teams.py`/`teams_dm.py` did, an inconsistency the review correctly flagged.
- **Documented in this update**: `demo/ui/rca_share_routes.py`'s `/api/rca/chat/share-teams` endpoint has no auth of its own — this is now stated explicitly in its module docstring as matching this app's existing unauthenticated same-origin POC posture (`server.py::_warn_if_approval_token_unset`, HITL-2/#102), not an oversight, with an explicit note that a hardened deployment needs real auth in front of `demo/ui/` before exposing it beyond localhost.
- Card payloads are built as dicts passed to `httpx.post(json=...)`, never string-templated — no injection surface.
- The DM/meeting/runbook-publish paths only ever notify or publish reference material; none of them touch the tool registry, HITL gate, or remediation execution.

**Test coverage**: `tests/test_chatops_teams_adapter.py` (32 tests), `tests/test_chatops_teams_dm_adapter.py` (24), `tests/test_war_room_teams_meeting.py` (11), `tests/test_chatops_runbook_attachment.py` (12) — all passing.

## Testing

- Full backend test suite (`uv run pytest`, `AIOPS_LLM_PROVIDER=stub`) passes clean after every change in this PR, including new coverage for: incident RAG (retrieval, thresholding, eligibility, boundary enforcement), resolution-verifier status tracking, RCA chat boundary/prompt ratchets, and investigation-context severity rendering.
- Frontend: `npm run build` (tsc + vite) passes clean.
- Verified live against real injected failures (`user_service.mysql_down`, `order_service.http_500`, `payment_service.high_cpu`) through the actual running cluster — not just unit tests — including before/after evidence-matrix and confidence comparisons.

## Known limitations / follow-ups

- The new evidence sources (logs, traces, datastore health) feed the scoring rules' `supporting`/`contradicting` lists correctly, but bypass the declarative gap/absence system that powers `negative_corroboration`/`critical_gap` — those two scoring rules can't yet benefit from "0 of 20 traces show an error" as a checked-absent signal. Documented, not fixed here (would touch scoring machinery intentionally left alone this round).
- Historical Incident RAG's corpus is whatever this deployment has actually persisted via `save_rca_result` — sparse on a fresh install until a few real incidents have run through the pipeline.
