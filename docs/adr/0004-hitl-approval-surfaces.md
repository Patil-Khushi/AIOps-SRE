# ADR-004: HITL approval surfaces

## Status

Accepted — HITL UI v1 (issues #77, #105).

## Context

CLAUDE.md non-negotiable #3: HITL is **platform-enforced, not agent-enforced**. A Required
action (e.g. a runbook execution, every RCA fix step) must block until a human approves, and
a buggy or compromised agent must be physically unable to bypass the gate.

The humans who approve are not in one place. The on-call engineer lives in chat and may
approve from a phone; the demo operator / SRE lives in the dashboard. So a single approval
needs to reach **both** an asynchronous chat surface and a synchronous web surface, and a
decision from either must resolve the same pending request. We also need an audit trail of
every request and decision.

## Decision

A Required action opens one `ApprovalRequest` in the `ApprovalRegistry`
(`aiops/policy/approvals.py`), which is the single source of truth. The registry fans every
lifecycle event (created / approved / denied / expired) to listeners that deliver it across
**multiple surfaces simultaneously** through the vendor-neutral chatops seam:

- **Web dashboard** — operator approves via an HTTP POST.
- **Slack interactive buttons** — on-call approves via the Slack callback.
- **WebSocket** — live push to any open dashboard.
- **JSON audit log** — durable record of every transition.

The HITL gate's approver (`ApprovalRequester`) blocks on the registry and returns the
approver id (or `None` on deny/expire). Agents never know an approval was requested — they
just see `ToolResult(ok=False, ...)` if denied. Decisions are idempotent, so two surfaces
racing to approve the same request is safe. *Why both web and Slack:* neither alone covers
both the remote on-call and the live demo operator; the seam makes adding a third surface a
listener, not a code change.

## Consequences

- **Easier:** add an approval surface = register a listener, with no change to agents or the
  gate; the same JSON-serializable `ApprovalRequest` renders on every surface; audit is free.
- **Harder:** multiple surfaces and timeouts introduce concurrency the registry must handle
  (idempotent `decide`, post-lock listener dispatch, expiry sweeps) — real complexity in
  `approvals.py`.
- **We now can't:** in v1, *authorize* who may approve — the registry trusts whoever posts the
  callback. Authorization (who *can* approve) is deferred to the Rego policy in Phase 2 (see
  [ADR-005](0005-policy-engine.md)).
