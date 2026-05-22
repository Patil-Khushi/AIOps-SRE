"""Approval registry — the platform side of HITL UI v1 (issue #77).

The HITL gate at :mod:`aiops.policy.gate` already blocks Required actions
unless an approver is present, but Phase 0 wired ``_no_approver`` into the
gate so Required actions just fail closed.  This module fills the gap.

Lifecycle::

    agent → registry.call("automation.runbook.execute", hitl_context={...})
          → gate.check(...)               (sees level == REQUIRED)
          → approver(action, ctx)         (= ApprovalRequester.__call__)
              → ApprovalRegistry.create(action, ctx)   ── posts chatops prompt
              → ApprovalRequest._event.wait(timeout)   ── blocks
              ← human approves via Slack callback / web POST
                  → ApprovalRegistry.decide(approval_id, ...)
                     ── sets status, signals waiter
              ← request returned
          ← approver id (or None on deny/timeout)
       ← Decision(allowed=...)

Design rules (CLAUDE.md principle #3):

* The approver logic lives here, not in agent code.  Agents never know an
  approval was requested; they just see ``ToolResult(ok=False, ...)`` if it
  was denied.
* The chatops surface is vendor-neutral — the registry sends a
  :class:`~aiops.tools.chatops.models.ChatMessage` and lets the configured
  adapters (Slack, web dashboard, JSON audit log) deliver it.
* Authorization (who *can* approve) is policy-as-code in
  ``policies/hitl.rego``.  v1 trusts whoever posts the callback; v2 will
  consult OPA before accepting the decision.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# Default timeout window in seconds.  Override per-process via
# AIOPS_HITL_APPROVAL_TIMEOUT, per-call via ``timeout_seconds`` kwarg.
_DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AIOPS_HITL_APPROVAL_TIMEOUT", "600"))

# Sentinel kept short so tests that exercise expiry don't sleep forever.
_MIN_TIMEOUT_SECONDS = 1


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class ApprovalRequest:
    """A pending or resolved request for human approval of a Required action.

    Fields are JSON-serializable on purpose so the web / Slack / WebSocket
    surfaces can return the same object without per-adapter conversion.
    """

    id: str
    action: str
    context: dict[str, Any]
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    approver: str | None = None
    reason: str = ""
    # Private to the registry: tests must not depend on this.
    _event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "context": dict(self.context),
            "status": str(self.status),
            "requested_at": self.requested_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "approver": self.approver,
            "reason": self.reason,
        }


class ApprovalError(RuntimeError):
    """Raised when an approval lookup or decision fails for a non-policy reason
    (missing id, already-decided request, malformed callback, etc.)."""


# Listener protocol: anything callable(event, request) -> None.  The registry
# fans every lifecycle transition (created / approved / denied / expired) to
# every registered listener.  The chatops-bridge listener turns these into
# ChatMessage posts; the WebSocket-bridge listener pushes them to live UIs.
#
# We use a list of plain callables rather than the chatops client directly so
# the registry has no compile-time dependency on the chatops package, and tests
# can attach a fake listener without monkey-patching.


class ApprovalRegistry:
    """In-memory store of approval requests + thread-safe wait/decide.

    Singleton in production via :func:`get_approval_registry`.  Tests use
    fresh instances to keep state isolated.
    """

    def __init__(self, *, default_timeout_seconds: int | None = None) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._listeners: list = []
        self._default_timeout = max(
            _MIN_TIMEOUT_SECONDS,
            int(default_timeout_seconds or _DEFAULT_TIMEOUT_SECONDS),
        )

    # ─── listener registration ──────────────────────────────────────────

    def add_listener(self, fn) -> None:
        """Register ``fn(event_name: str, request: ApprovalRequest) -> None``.

        Listeners are dispatched *outside* the registry lock so a listener
        that does network I/O (the chatops bridge can post to Slack) does
        not serialize concurrent approvals.  Listeners may safely call back
        into the registry, but each listener is otherwise responsible for
        its own thread-safety.
        """
        with self._lock:
            self._listeners.append(fn)

    def clear_listeners(self) -> None:
        with self._lock:
            self._listeners.clear()

    def _queue_event_locked(
        self,
        pending: list[tuple[Any, str, ApprovalRequest]],
        event_name: str,
        req: ApprovalRequest,
    ) -> None:
        """Snapshot the current listeners and append (fn, event, req) tuples to
        ``pending``.  Must be called with ``self._lock`` held.  The caller
        flushes ``pending`` via :meth:`_dispatch` after releasing the lock —
        keeping listener I/O off the critical section."""
        for fn in self._listeners:
            pending.append((fn, event_name, req))

    @staticmethod
    def _dispatch(pending: list[tuple[Any, str, ApprovalRequest]]) -> None:
        """Invoke queued listeners.  Never called under ``self._lock``."""
        for fn, event_name, req in pending:
            try:
                fn(event_name, req)
            except Exception:
                logger.exception("approval listener %r raised on event %r", fn, event_name)

    # ─── lifecycle ──────────────────────────────────────────────────────

    def create(
        self,
        action: str,
        context: dict[str, Any] | None = None,
        *,
        timeout_seconds: int | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Open a new pending approval request and notify listeners."""
        ttl = max(_MIN_TIMEOUT_SECONDS, int(timeout_seconds or self._default_timeout))
        now = datetime.now(UTC)
        req = ApprovalRequest(
            id=request_id or uuid.uuid4().hex,
            action=action,
            context=dict(context or {}),
            status=ApprovalStatus.PENDING,
            requested_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        pending: list[tuple[Any, str, ApprovalRequest]] = []
        with self._lock:
            if req.id in self._requests:
                raise ApprovalError(f"approval id collision: {req.id!r}")
            self._requests[req.id] = req
            self._queue_event_locked(pending, "created", req)
        self._dispatch(pending)
        return req

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        approver: str,
        reason: str = "",
    ) -> ApprovalRequest:
        """Resolve a pending request.  Idempotent only for the *same* decision —
        re-deciding with a different verdict raises ``ApprovalError``.

        ``approver`` is the chatops-supplied identity (Slack user id, web
        session login, etc.).  Authorization of who CAN approve happens in
        the rego policy, not here.
        """
        expired: list[ApprovalRequest] = []
        pending: list[tuple[Any, str, ApprovalRequest]] = []
        try:
            with self._lock:
                req = self._requests.get(request_id)
                if req is None:
                    raise ApprovalError(f"unknown approval id: {request_id!r}")
                if self._refresh_expiry_locked(req, pending):
                    expired.append(req)
                if req.status is not ApprovalStatus.PENDING:
                    # Allow idempotent re-decision when it agrees with current state.
                    target = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                    if req.status is target and req.approver == approver:
                        return req
                    raise ApprovalError(
                        f"approval {request_id!r} already {req.status.value}; "
                        f"cannot change to {target.value}"
                    )
                req.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
                req.approver = approver
                req.reason = reason
                req.decided_at = datetime.now(UTC)
                event_name = "approved" if approved else "denied"
                self._queue_event_locked(pending, event_name, req)
            # Wake any waiters AFTER releasing the lock — wait_for grabs the
            # lock to read the request, so signalling under the lock can
            # deadlock under asyncio.to_thread on some platforms.
            req._event.set()
            return req
        finally:
            self._signal_expired(expired)
            self._dispatch(pending)

    def expire(self, request_id: str) -> ApprovalRequest:
        """Force-expire a pending request (used by tests + the sweeper)."""
        pending: list[tuple[Any, str, ApprovalRequest]] = []
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ApprovalError(f"unknown approval id: {request_id!r}")
            if req.status is ApprovalStatus.PENDING:
                req.status = ApprovalStatus.EXPIRED
                req.decided_at = datetime.now(UTC)
                req.reason = req.reason or "expired"
                self._queue_event_locked(pending, "expired", req)
        req._event.set()
        self._dispatch(pending)
        return req

    # ─── lookup ─────────────────────────────────────────────────────────

    def get(self, request_id: str) -> ApprovalRequest:
        expired: list[ApprovalRequest] = []
        pending: list[tuple[Any, str, ApprovalRequest]] = []
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ApprovalError(f"unknown approval id: {request_id!r}")
            if self._refresh_expiry_locked(req, pending):
                expired.append(req)
        self._signal_expired(expired)
        self._dispatch(pending)
        return req

    def list_pending(self) -> list[ApprovalRequest]:
        pending_events: list[tuple[Any, str, ApprovalRequest]] = []
        with self._lock:
            expired = self._sweep_expired_locked(pending_events)
            pending = [r for r in self._requests.values() if r.status is ApprovalStatus.PENDING]
        self._signal_expired(expired)
        self._dispatch(pending_events)
        return pending

    def list_all(self) -> list[ApprovalRequest]:
        pending_events: list[tuple[Any, str, ApprovalRequest]] = []
        with self._lock:
            expired = self._sweep_expired_locked(pending_events)
            snapshot = list(self._requests.values())
        self._signal_expired(expired)
        self._dispatch(pending_events)
        return snapshot

    # ─── waiting ────────────────────────────────────────────────────────

    def wait_for(
        self,
        request_id: str,
        *,
        timeout: float | None = None,
    ) -> ApprovalRequest:
        """Block until the request transitions out of PENDING (or the optional
        wall-clock ``timeout`` elapses).

        If ``timeout`` is omitted, waits until the request's own ``expires_at``.
        Returns the (possibly EXPIRED) request — never raises on timeout, so
        callers can branch on ``status``.
        """
        req = self.get(request_id)
        if req.status is not ApprovalStatus.PENDING:
            return req
        # Compute the wait budget.  We always cap at expires_at so an
        # in-flight request that nobody resolves still wakes the waiter.
        budget = timeout
        ttl_remaining = max(0.0, (req.expires_at - datetime.now(UTC)).total_seconds())
        if budget is None or budget > ttl_remaining:
            budget = ttl_remaining
        if budget > 0:
            req._event.wait(budget)
        # Re-read state under lock; expiry may have flipped it.
        return self.get(request_id)

    # ─── housekeeping ───────────────────────────────────────────────────

    def _refresh_expiry_locked(
        self,
        req: ApprovalRequest,
        pending: list[tuple[Any, str, ApprovalRequest]],
    ) -> bool:
        """If ``req`` should be expired now, flip it and queue the listener
        event onto ``pending`` for post-lock dispatch.  Returns True when a
        transition happened so the caller can signal waiters outside the lock."""
        if req.status is ApprovalStatus.PENDING and datetime.now(UTC) >= req.expires_at:
            req.status = ApprovalStatus.EXPIRED
            req.decided_at = datetime.now(UTC)
            req.reason = req.reason or "expired"
            self._queue_event_locked(pending, "expired", req)
            return True
        return False

    def _sweep_expired_locked(
        self,
        pending: list[tuple[Any, str, ApprovalRequest]],
    ) -> list[ApprovalRequest]:
        expired: list[ApprovalRequest] = []
        for req in self._requests.values():
            if self._refresh_expiry_locked(req, pending):
                expired.append(req)
        return expired

    def _signal_expired(self, expired: list[ApprovalRequest]) -> None:
        # Threading.Event.set() is cheap and safe outside the lock; doing it
        # under the lock can deadlock if a waiter callback re-enters the
        # registry (e.g. a listener that calls .get()).
        for req in expired:
            req._event.set()

    # ─── test seam ──────────────────────────────────────────────────────

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._requests.clear()
            self._listeners.clear()


# ─── module-level singleton ─────────────────────────────────────────────

_REGISTRY: ApprovalRegistry | None = None


def get_approval_registry() -> ApprovalRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ApprovalRegistry()
    return _REGISTRY


# ─── ApproverFn integration with the HITL gate ──────────────────────────


class ApprovalRequester:
    """An :data:`~aiops.policy.gate.ApproverFn` that turns a Required-HITL
    check into a pending approval request, blocks until the human answers,
    and returns the approver id (or ``None`` on deny / expire).

    The gate calls this synchronously from inside ``ToolRegistry.call``.
    For FastAPI handlers, wrap the registry call in ``asyncio.to_thread``
    so the event loop is not held during the human's reaction time.
    """

    def __init__(
        self,
        registry: ApprovalRegistry | None = None,
        *,
        timeout_seconds: int | None = None,
    ) -> None:
        self._registry = registry or get_approval_registry()
        self._timeout_seconds = timeout_seconds

    def __call__(self, action: str, context: dict[str, Any]) -> str | None:
        # ``hitl_context.skip_approval=True`` exists as an explicit override for
        # eval-harness runs that don't want to block — they get the default
        # "no approver" behaviour (REQUIRED stays blocked) without spawning a
        # pending request that has to time out.
        if context.get("skip_approval"):
            return None
        req = self._registry.create(
            action=action,
            context=context,
            timeout_seconds=context.get("approval_timeout_seconds") or self._timeout_seconds,
            request_id=context.get("approval_id"),
        )
        # Surface the id back to the caller so HTTP/CLI surfaces can show it.
        context["pending_approval_id"] = req.id
        resolved = self._registry.wait_for(req.id)
        if resolved.status is ApprovalStatus.APPROVED:
            return resolved.approver or "unknown-approver"
        # Annotate context so the gate's Decision.reason can render
        # "denied by <approver>: <reason>" / "expired" rather than the
        # generic "approver missing" fallback.
        context["approval_decision"] = resolved.status.value
        context["approval_approver"] = resolved.approver
        context["approval_reason"] = resolved.reason
        return None


def install_default_approver(
    registry: ApprovalRegistry | None = None,
    *,
    timeout_seconds: int | None = None,
) -> None:
    """Replace the gate's stub ``_no_approver`` with an :class:`ApprovalRequester`
    bound to ``registry``.  Idempotent: re-installing rebinds without spawning
    a new registry.

    Called from the FastAPI startup hook so the demo's gate posts to chatops.
    Library / CLI consumers that don't want the chatops behaviour simply skip
    this call.
    """
    from aiops.policy.gate import get_gate

    requester = ApprovalRequester(registry, timeout_seconds=timeout_seconds)
    get_gate()._approver = requester


def chatops_listener_factory():
    """Build a registry listener that posts approval lifecycle events through
    the chatops seam.  Imported lazily so the policy package has no hard
    dependency on the tools package (keeps the dep arrow one-way).
    """
    from aiops.tools.chatops import ChatMessage, Severity, get_client
    from aiops.tools.chatops.models import InteractivePrompt

    _SEVERITY_BY_EVENT: dict[str, Severity] = {
        "created": Severity.P1,
        "approved": Severity.INFO,
        "denied": Severity.P2,
        "expired": Severity.P2,
    }

    _TITLE_BY_EVENT: dict[str, str] = {
        "created": "HITL approval requested",
        "approved": "HITL approval granted",
        "denied": "HITL approval denied",
        "expired": "HITL approval expired",
    }

    def _listener(event_name: str, req: ApprovalRequest) -> None:
        title = f"{_TITLE_BY_EVENT.get(event_name, event_name)}: {req.action}"
        body_lines = [
            f"Action: {req.action}",
            f"Approval id: {req.id}",
            f"Status: {req.status.value}",
        ]
        if req.context:
            ctx_view = {k: v for k, v in req.context.items() if not k.startswith("_")}
            if ctx_view:
                body_lines.append(f"Context: {ctx_view}")
        if req.approver:
            body_lines.append(f"Approver: {req.approver}")
        if req.reason:
            body_lines.append(f"Reason: {req.reason}")

        interactive: InteractivePrompt | None = None
        if event_name == "created":
            interactive = InteractivePrompt(
                approval_id=req.id,
                action=req.action,
                expires_at=req.expires_at,
            )

        msg = ChatMessage(
            channel="hitl-approvals",
            severity=_SEVERITY_BY_EVENT.get(event_name, Severity.INFO),
            title=title,
            body="\n".join(body_lines),
            interactive=interactive,
        )
        get_client().send(msg)

    return _listener


def install_chatops_listener(registry: ApprovalRegistry | None = None) -> None:
    """Wire :func:`chatops_listener_factory` into ``registry``.  Idempotent:
    safe to call from multiple startup hooks (the listener identity check
    prevents duplicates within a single process).
    """
    reg = registry or get_approval_registry()
    listener = chatops_listener_factory()
    # The factory returns a fresh closure each call, so a string tag on the
    # listener gives us a stable identity for dedup.
    listener.__aiops_listener_id__ = "chatops_bridge"  # type: ignore[attr-defined]
    with reg._lock:
        for existing in reg._listeners:
            if getattr(existing, "__aiops_listener_id__", None) == "chatops_bridge":
                return
        reg._listeners.append(listener)
