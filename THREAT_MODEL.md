# Threat Model

STRIDE analysis per trust boundary. The HITL gate (`aiops/policy/gate.py`) is the platform's
whole safety story (CLAUDE.md non-negotiable #3: HITL is platform-enforced, not
agent-enforced) — so a threat-model gap at a boundary that reaches the gate is an architecture
gap. Even at POC scale this handles real ServiceNow / Slack / PagerDuty credentials, so the
credential boundaries are in scope too.

**Method:** [Microsoft STRIDE](https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats) —
Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of
privilege — one table per boundary. Likelihood / Impact are `H`/`M`/`L`. Status:
`Open` (unmitigated) · `Partial` (control exists, residual risk) · `Mitigated` · `Accepted`.

**Trust boundaries covered:**

1. HITL approval surface + gate (the approval token, web approve/deny, Slack callback)
2. LLM prompt-injection path (untrusted alert text → RCA/triage prompt)
3. ServiceNow basic-auth credentials
4. Slack signing secret + bot token
5. PagerDuty Events API key
6. The chatops audit log
7. The flagd config (cluster-level flag mutation)

> The companion architecture and the seam diagram are in
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (PR #162); decisions referenced here are in
> [`docs/adr/`](docs/adr/) (PR #161).

---

## B1 — HITL approval surface + gate

The boundary between "an agent requested a Required action" and "a human authorized it."
Crossed by three paths: the in-process gate (`gate.check`), the web approve/deny endpoints
(`AIOPS_HITL_APPROVAL_TOKEN`), and the Slack callback (`AIOPS_SLACK_SIGNING_SECRET`).

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | An attacker poses as an approver and resolves a pending Required action. | M | H | Web endpoints require `Authorization: Bearer <AIOPS_HITL_APPROVAL_TOKEN>` (constant-time `hmac.compare_digest`); Slack callback verifies the signing-secret HMAC. **Demo default leaves the token unset → endpoints are open**, with a loud startup warning. | **Partial** |
| Tampering | The action context (blast radius, target) is altered between request and approval. | L | H | The `ApprovalRequest` is created server-side by the gate from the agent's context; the approver decides by id, not by re-submitting context. | Mitigated |
| Repudiation | An approver denies having approved a destructive action. | M | M | Web `approver` is **self-asserted** (free-text body field); Slack identity is HMAC-verified to originate from Slack but the username is Slack-supplied. Every decision is logged to the audit log with id + approver + reason. No per-identity auth until OPA. | **Open** |
| Information disclosure | Pending-approval listing leaks incident internals to an unauthenticated reader. | M | M | `GET /api/approvals` is **not** token-gated today; context dicts may carry service/alert detail. Localhost-only demo binding limits exposure. | **Open** |
| Denial of service | Flooding the approval endpoints, or never approving, stalls the agent thread. | L | M | Gate waits are bounded by `AIOPS_HITL_APPROVAL_TIMEOUT` (fail-closed on expiry); registry uses post-lock dispatch so one slow listener can't serialize all approvals. | Partial |
| Elevation of privilege | A buggy/compromised agent executes a Required action **without** approval. | M | H | The gate is checked inside `ToolRegistry.call()` **before** the provider runs — an agent cannot reach the tool without passing the gate; no approver ⇒ blocked (fail-closed). This is the core platform guarantee. | Mitigated |

---

## B2 — LLM prompt-injection path

Untrusted alert text (service names, error messages, log lines from the monitored system)
flows into the triage summary prompt and the RCA prompt. A crafted payload could try to make
the model emit instructions or a fabricated fix step.

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | Injected text impersonates a system instruction to the model. | M | M | Prompts separate system role from user-supplied content; alert text is data, not instruction. No formal delimiting/escaping of untrusted spans yet. | Partial |
| Tampering | Injection alters the RCA verdict or proposed fix steps. | M | H | RCA fix steps are **recommend-only** in v0 and every step is `requires_hitl=true` — a poisoned fix cannot self-execute; a human reviews before any action. | Partial |
| Repudiation | — (covered by audit log, B6). | L | L | Prompt + response captured in the agent audit trace. | Mitigated |
| Information disclosure | Injection coaxes the model to reveal prior context / secrets in the prompt. | M | M | Prompts carry no credentials (those live in the tool seam, not the prompt); context is incident data only. No PII-redaction guardrail yet (Solution Design names one for production). | Open |
| Denial of service | Pathological alert text inflates tokens / forces timeouts. | M | M | `AIOPS_LLM_MAX_TOKENS_PER_CALL` caps output; provider timeout bounds latency; template/Tier-4 fallback keeps the flow alive. | Mitigated |
| Elevation of privilege | Injection induces a destructive tool call. | L | H | Agents can only reach tools via capabilities, each gated by autonomy level; destructive capabilities are Required-HITL. The model cannot invoke kubectl/ServiceNow directly. | Mitigated |

---

## B3 — ServiceNow basic-auth credentials

`SERVICENOW_USER` / `SERVICENOW_PASSWORD` for the PDI, used by `aiops/tools/itsm`.

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | Stolen creds let an attacker file/alter incidents as the agent. | M | M | Dedicated `aiops_agent` service account with least-privilege `itil` role (issue #10); not the admin login. | Partial |
| Tampering | Creds modified to point at a rogue instance. | L | M | `SERVICENOW_INSTANCE_URL` + creds read from `.env` (gitignored); no integrity check on the URL. | Open |
| Repudiation | Agent-created tickets can't be attributed. | L | L | All incidents carry the service-account identity + the decision trace in the description. | Mitigated |
| Information disclosure | Plaintext creds leak (screen-share, `.env` in IDE). | **H** | **H** | `.env` is gitignored — the **only** control. No vault, no rotation cadence (retro §2.2). | **Open** |
| Denial of service | PDI sleeps/expires/rate-limits mid-demo. | H | M | `AIOPS_USE_MOCK_ITSM=true` fallback; create returns `ToolResult(ok=False)` without breaking the flow. | Mitigated |
| Elevation of privilege | `itil` role broader than needed. | L | M | Scope the role to incident + CMDB tables; revisit before any non-PDI tenant. | Partial |

---

## B4 — Slack signing secret + bot token

`AIOPS_SLACK_SIGNING_SECRET` (verifies inbound callbacks) and `SLACK_BOT_TOKEN` (outbound posts).

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | Forged Slack callback approves an action. | M | H | `_verify_slack_signature`: HMAC-SHA256 over `v0:{ts}:{body}`, constant-time compare, **5-minute replay window**; all failures return an identical 401 (no side channel). | Mitigated |
| Tampering | Replayed/modified interactivity payload. | M | H | Timestamp freshness check rejects payloads >5 min old; HMAC covers the raw body. | Mitigated |
| Repudiation | Who clicked approve in Slack is disputed. | M | M | Approver recorded as `slack:<username>` + `action_id` in the audit log; identity is Slack-supplied (not independently verified beyond HMAC origin). | Partial |
| Information disclosure | Bot token leak lets an attacker post as the bot / read channels. | **H** | **H** | Token in gitignored `.env`; signing secret rotatable without restart (read per-call). No vault. | **Open** |
| Denial of service | Callback endpoint flooded. | L | M | Cheap HMAC reject path; localhost-only demo binding. | Partial |
| Elevation of privilege | Bot scopes broader than needed (`channels:manage`). | M | M | Scopes are pre-positioned for RA-006 War-Room; trim to what RA-005 needs for the POC. | Open |

---

## B5 — PagerDuty Events API key

`PAGERDUTY_ROUTING_KEY` for `aiops/tools/chatops/adapters/pagerduty.py`.

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | Stolen key triggers fake pages. | L | M | Routing key in gitignored `.env`; PD not on the Reactive critical path (paging only). | Partial |
| Tampering | Page payload altered to mis-route. | L | M | Adapter constructs payloads server-side; severity threshold (≥P2) bounds what pages. | Mitigated |
| Repudiation | Source of a page disputed. | L | L | Events carry the incident id + dedup key; audit-logged. | Mitigated |
| Information disclosure | Key leak / incident detail in page text. | M | M | Gitignored `.env`; minimize sensitive detail in page summaries. | Open |
| Denial of service | Trial quota exhausted by a page storm (e.g. RA-005 bug appends `page_oncall`). | M | M | Severity gate (≥P2) limits paging volume; trial-quota risk tracked in the Risk Register. | Partial |
| Elevation of privilege | Routing key grants more than event-create. | L | L | Events API keys are write-only to a single service; no escalation surface. | Mitigated |

---

## B6 — Chatops audit log

`demo/audit/chatops.jsonl`, written by `JsonFileChatOpsAdapter` — every notification and every
approval lifecycle event.

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | Forged audit entries. | L | M | Only the in-process adapter writes; entries are server-generated `ChatMessage`s. | Partial |
| Tampering | Entries altered/deleted to hide an action. | M | H | Adapter opens the file in **append mode** (`"a"`) — no in-process rewrite path. But it's a local file with no signing/WORM, so anyone with filesystem access can edit it. | **Partial** |
| Repudiation | An action's approval can't be proven after the fact. | M | M | Append-only log captures created/approved/denied/expired with id + approver + reason + timestamp — the primary non-repudiation record. | Mitigated |
| Information disclosure | Log carries incident internals; readable by anyone on the box. | M | M | Gitignored (`demo/audit/`), local-only. No encryption at rest. | Open |
| Denial of service | Unbounded growth fills the disk. | L | L | `start.ps1 -Fresh` rotates to `.bak-<ts>`; otherwise grows unbounded. | Partial |
| Elevation of privilege | — | L | L | The log is a sink, not a control surface; no privilege to gain. | Mitigated |

---

## B7 — flagd config (cluster-level)

The `flagd-config` ConfigMap controls which failure scenarios are active. Mutated only through
`aiops/tools/feature_flags` ([ADR-001](docs/adr/0001-feature-flag-mutation-seam.md)).

| Threat | Description | L | I | Mitigation | Status |
|---|---|---|---|---|---|
| Spoofing | An actor flips flags posing as the platform. | L | M | The seam uses the in-cluster kube context; no separate flag identity. | Accepted |
| Tampering | Arbitrary flag flips inject failures mid-demo. | M | M | **Anyone with cluster RBAC can patch the ConfigMap directly** — the seam governs *our* code, not cluster access. SSA `force=True` keeps Helm ownership consistent. | Open |
| Repudiation | Flag changes aren't attributed. | L | L | Seam calls log set/reset; ad-hoc `kubectl` by a cluster admin is not captured. | Partial |
| Information disclosure | ConfigMap reveals scenario catalog. | L | L | Non-sensitive (demo scenario names). | Accepted |
| Denial of service | Flipping every flag on degrades the demo app. | L | M | `feature_flags.reset_all` / `inject.py --clear` restores baseline; blast radius is the demo app only. | Mitigated |
| Elevation of privilege | Cluster access ⇒ flag control. | L | M | Bounded by k3s RBAC on the operator's single-node cluster; not multi-tenant at POC. | Accepted |

---

## Required-HITL action coverage

DoD: **every Required-HITL action has at least one threat row.** The dominant threat for each
is *Elevation of privilege* — the action executing without a valid human approval — which is
mitigated by the platform gate (B1, Elevation row): the gate is checked before the tool runs
and fails closed. This table maps each Required capability to its primary threat + residual.

| Required action (capability) | Primary threat | Residual risk | Status |
|---|---|---|---|
| `rca.fix_step.execute` | EoP — fix step executes unapproved | Recommend-only in v0; gate blocks execution | Mitigated |
| `automation.runbook.execute` | EoP — runbook runs unapproved | Gate-blocked; demo token unset by default (B1) | Partial |
| `remediation.recommend` | Tampering — bad rec acted on | Required-HITL; advisory only | Mitigated |
| `policy.optimize` | EoP — threshold/policy changed unapproved | Gate-blocked; revert via Git | Mitigated |
| `feedback.promote_model` | Tampering — bad model promoted | Shadow-eval + champion/challenger + gate | Mitigated |
| `knowledge.publish` | Tampering — false KB published | Draft-then-approve + gate | Mitigated |
| `chaos.experiment.run` | EoP — chaos runs unapproved | Gate-blocked; out of POC scope | Accepted |
| `capacity.recommend` | Tampering — bad capacity action | Advisory; gate on the acting change | Mitigated |
| `slo.freeze_changes` | EoP — freeze toggled unapproved | Gate-blocked; advisory | Mitigated |
| `change.predict_risk` | Info disclosure — risk score leaks change detail | Advisory; gate on the acting change | Mitigated |

> Of these, only `rca.fix_step.execute` (recommend-only) and `automation.runbook.execute`
> (Auto-Healer-lite) are wired to real agents today; the rest are catalog placeholders in
> `DEFAULT_LEVELS` so the gate is complete-by-design when those agents land.

---

## High/Critical findings → ADR-fodder

Per the DoD, the highest-severity finding flagged for a follow-up ADR:

**🔴 HITL approval identity & authorization (B1 Spoofing/Repudiation, B3/B4 Information
disclosure).** Today the web approval endpoints are a single **shared-secret bearer token**
(and *unauthenticated by default* in the demo), the `approver` identity is **self-asserted**,
and all integration secrets sit **plaintext in `.env`** with gitignore as the only control.
At POC scale this is acceptable; before any shared/multi-tenant deployment it is not — a
shared secret cannot answer "*who* approved this destructive action," which is the entire point
of HITL. **Recommended ADR: "Identity & authorization for HITL approvals"** — replace the
shared token with per-identity auth (OIDC / Slack-verified user → role), wire authorization
through `policies/hitl.rego` (who *may* approve which actions), and move secrets to a vault
with a rotation cadence. This is the natural Phase-2 successor to
[ADR-004](docs/adr/0004-hitl-approval-surfaces.md) and [ADR-005](docs/adr/0005-policy-engine.md).

---

## References

- [`aiops/policy/gate.py`](aiops/policy/gate.py) — the HITL gate this document protects; `DEFAULT_LEVELS` defines the Required actions.
- [`aiops/policy/approvals.py`](aiops/policy/approvals.py) — the approval registry + lifecycle.
- [`demo/ui/server.py`](demo/ui/server.py) — `_require_approval_token`, `_verify_slack_signature`, the approval endpoints.
- [`CLAUDE.md`](CLAUDE.md) — non-negotiable principle #3 (HITL is platform-enforced).
- [`RISK_REGISTER.md`](RISK_REGISTER.md) — operational risks (secret rotation, PD quota, PDI expiry) that overlap B3–B5.
- Microsoft STRIDE — https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats
