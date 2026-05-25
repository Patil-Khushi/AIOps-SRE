"""Unit tests for the approval registry (HITL-1, issue #77)."""

from __future__ import annotations

import threading
import time

import pytest

from aiops.policy.approvals import (
    ApprovalError,
    ApprovalRegistry,
    ApprovalStatus,
)


@pytest.fixture
def registry() -> ApprovalRegistry:
    return ApprovalRegistry(default_timeout_seconds=2)


def test_create_returns_pending_request_with_unique_id(registry: ApprovalRegistry):
    a = registry.create("automation.runbook.execute", {"target": "deployment/foo"})
    b = registry.create("automation.runbook.execute", {"target": "deployment/bar"})

    assert a.status is ApprovalStatus.PENDING
    assert b.status is ApprovalStatus.PENDING
    assert a.id != b.id
    assert a.action == "automation.runbook.execute"
    assert a.context["target"] == "deployment/foo"


def test_explicit_request_id_is_honoured(registry: ApprovalRegistry):
    req = registry.create("test.action", {}, request_id="known-id")
    assert req.id == "known-id"
    with pytest.raises(ApprovalError):
        registry.create("test.action", {}, request_id="known-id")


def test_decide_approve_resolves_pending_request(registry: ApprovalRegistry):
    req = registry.create("rca.fix_step.execute", {})
    out = registry.decide(req.id, approved=True, approver="oncall@example.com", reason="lgtm")
    assert out.status is ApprovalStatus.APPROVED
    assert out.approver == "oncall@example.com"
    assert out.reason == "lgtm"
    assert out.decided_at is not None


def test_decide_deny_resolves_pending_request(registry: ApprovalRegistry):
    req = registry.create("rca.fix_step.execute", {})
    out = registry.decide(req.id, approved=False, approver="sre@example.com", reason="nope")
    assert out.status is ApprovalStatus.DENIED
    assert out.approver == "sre@example.com"


def test_decide_idempotent_with_same_verdict(registry: ApprovalRegistry):
    req = registry.create("rca.fix_step.execute", {})
    first = registry.decide(req.id, approved=True, approver="alice")
    second = registry.decide(req.id, approved=True, approver="alice")
    assert first.id == second.id
    assert second.status is ApprovalStatus.APPROVED


def test_decide_conflicting_verdict_raises(registry: ApprovalRegistry):
    req = registry.create("rca.fix_step.execute", {})
    registry.decide(req.id, approved=True, approver="alice")
    with pytest.raises(ApprovalError) as ei:
        registry.decide(req.id, approved=False, approver="bob")
    assert "already approved" in str(ei.value)


def test_decide_unknown_id_raises(registry: ApprovalRegistry):
    with pytest.raises(ApprovalError) as ei:
        registry.decide("missing", approved=True, approver="x")
    assert "unknown" in str(ei.value)


def test_wait_for_unblocks_when_decision_is_made(registry: ApprovalRegistry):
    req = registry.create("test.action", {}, timeout_seconds=10)

    decided: dict = {}

    def waiter():
        decided["req"] = registry.wait_for(req.id)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    registry.decide(req.id, approved=True, approver="alice")
    t.join(timeout=2)
    assert not t.is_alive(), "wait_for did not return after decide"
    assert decided["req"].status is ApprovalStatus.APPROVED


def test_wait_for_returns_expired_when_timeout_elapses(registry: ApprovalRegistry):
    req = registry.create("test.action", {}, timeout_seconds=1)
    resolved = registry.wait_for(req.id, timeout=1.5)
    assert resolved.status is ApprovalStatus.EXPIRED
    assert resolved.reason == "expired"


def test_list_pending_excludes_resolved_and_expired(registry: ApprovalRegistry):
    a = registry.create("test.a", {})
    b = registry.create("test.b", {})
    c = registry.create("test.c", {}, timeout_seconds=1)
    registry.decide(a.id, approved=True, approver="x")
    # Wait for c to expire.
    time.sleep(1.1)
    pending = registry.list_pending()
    pending_ids = {r.id for r in pending}
    assert a.id not in pending_ids
    assert c.id not in pending_ids
    assert b.id in pending_ids


def test_get_lazily_expires_pending(registry: ApprovalRegistry):
    req = registry.create("test.action", {}, timeout_seconds=1)
    time.sleep(1.1)
    fresh = registry.get(req.id)
    assert fresh.status is ApprovalStatus.EXPIRED


def test_listeners_fire_for_each_lifecycle_event(registry: ApprovalRegistry):
    events: list[tuple[str, str]] = []

    def listener(event_name: str, req):
        events.append((event_name, req.id))

    registry.add_listener(listener)

    req = registry.create("test.action", {})
    registry.decide(req.id, approved=True, approver="alice")

    assert ("created", req.id) in events
    assert ("approved", req.id) in events

    other = registry.create("test.action", {})
    registry.decide(other.id, approved=False, approver="bob")
    assert ("denied", other.id) in events

    expiring = registry.create("test.action", {}, timeout_seconds=1)
    time.sleep(1.2)
    registry.list_pending()  # triggers sweep
    assert ("expired", expiring.id) in events


def test_listener_exception_does_not_break_other_listeners(registry: ApprovalRegistry):
    seen: list[str] = []
    registry.add_listener(lambda e, r: (_ for _ in ()).throw(RuntimeError("boom")))
    registry.add_listener(lambda e, r: seen.append(e))
    req = registry.create("test.action", {})
    registry.decide(req.id, approved=True, approver="x")
    assert "created" in seen and "approved" in seen


def test_expire_forces_pending_to_expired(registry: ApprovalRegistry):
    req = registry.create("test.action", {}, timeout_seconds=60)
    out = registry.expire(req.id)
    assert out.status is ApprovalStatus.EXPIRED


def test_to_record_is_json_friendly(registry: ApprovalRegistry):
    import json

    req = registry.create("rca.fix_step.execute", {"x": 1})
    registry.decide(req.id, approved=True, approver="alice", reason="ok")
    rec = registry.get(req.id).to_record()
    blob = json.dumps(rec)  # must not raise
    assert "alice" in blob and "approved" in blob


def test_slow_listener_does_not_serialize_concurrent_operations(
    registry: ApprovalRegistry,
):
    """Regression for the bug where listeners ran under self._lock: a Slack
    HTTP post in the chatops listener would block every other registry call
    for the duration of the round-trip.  Listeners now fire post-lock, so a
    slow listener on thread A must not delay an independent decide() on
    thread B."""
    barrier = threading.Event()
    release = threading.Event()

    def slow_listener(event_name, req):
        # Block on the FIRST event we see, until the test releases us.
        if not barrier.is_set():
            barrier.set()
            release.wait(timeout=5)

    registry.add_listener(slow_listener)

    # Thread A creates a request — its "created" event will park inside the
    # slow listener.  If listeners ran under the lock, the registry would be
    # frozen until ``release`` is set.
    req_a_box: dict = {}

    def create_a():
        req_a_box["req"] = registry.create("a.action", {}, timeout_seconds=60)

    t_a = threading.Thread(target=create_a, daemon=True)
    t_a.start()

    # Wait for thread A to enter the slow listener.
    assert barrier.wait(timeout=2), "slow listener never fired"

    # Thread B must be able to create another request while A's listener is
    # still blocked.  Wrap with our own timeout so a regression fails fast.
    req_b_box: dict = {}

    def create_b():
        req_b_box["req"] = registry.create("b.action", {}, timeout_seconds=60)

    t_b = threading.Thread(target=create_b, daemon=True)
    t_b.start()
    t_b.join(timeout=2)
    assert not t_b.is_alive(), "thread B was blocked by thread A's slow listener"
    assert req_b_box["req"].action == "b.action"

    # Cleanup — let A finish.
    release.set()
    t_a.join(timeout=2)
    assert not t_a.is_alive()


def test_listener_may_call_back_into_registry(registry: ApprovalRegistry):
    """Listeners run outside the lock, so it's now legal for a listener to
    call back into the registry (e.g. .get()) without deadlocking."""
    seen: list[str] = []

    def reentrant_listener(event_name, req):
        if event_name == "created":
            # This used to deadlock when listeners ran under self._lock.
            fetched = registry.get(req.id)
            seen.append(fetched.id)

    registry.add_listener(reentrant_listener)
    req = registry.create("test.action", {}, timeout_seconds=60)
    assert seen == [req.id]


# ─── HITL-4 (#104) idempotent add_listener(id=...) ────────────────────────


def test_add_listener_without_id_stacks(registry: ApprovalRegistry):
    """Anonymous listeners (no id) stack — the legacy behaviour used by tests
    that attach throwaway fakes."""
    calls: list[str] = []
    registry.add_listener(lambda _e, _r: calls.append("a"))
    registry.add_listener(lambda _e, _r: calls.append("b"))
    registry.create("test.action", {})
    # Both fire on the "created" event.
    assert sorted(calls) == ["a", "b"]


def test_add_listener_same_id_replaces_previous(registry: ApprovalRegistry):
    """A second ``add_listener`` with the same id replaces the prior
    registration rather than stacking duplicates — the core HITL-4
    acceptance bullet for ``install_chatops_listener`` idempotency."""
    calls: list[str] = []
    registry.add_listener(lambda _e, _r: calls.append("first"), id="bridge")
    registry.add_listener(lambda _e, _r: calls.append("second"), id="bridge")
    registry.create("test.action", {})
    assert calls == ["second"]


def test_add_listener_different_ids_coexist(registry: ApprovalRegistry):
    """Two ids → two listeners, both fire."""
    calls: list[str] = []
    registry.add_listener(lambda _e, _r: calls.append("a"), id="a-bridge")
    registry.add_listener(lambda _e, _r: calls.append("b"), id="b-bridge")
    registry.create("test.action", {})
    assert sorted(calls) == ["a", "b"]


def test_add_listener_id_can_replace_anonymous_or_other_id(registry: ApprovalRegistry):
    """Replace-on-duplicate-id only matches the exact id; anonymous and other
    ids are preserved."""
    calls: list[str] = []
    registry.add_listener(lambda _e, _r: calls.append("anon"))
    registry.add_listener(lambda _e, _r: calls.append("other"), id="other")
    registry.add_listener(lambda _e, _r: calls.append("bridge-v1"), id="bridge")
    registry.add_listener(lambda _e, _r: calls.append("bridge-v2"), id="bridge")
    registry.create("test.action", {})
    assert sorted(calls) == ["anon", "bridge-v2", "other"]


def test_remove_listener_targets_id(registry: ApprovalRegistry):
    fired: list[str] = []
    registry.add_listener(lambda _e, _r: fired.append("kept"), id="keep")
    registry.add_listener(lambda _e, _r: fired.append("gone"), id="trash")
    assert registry.remove_listener(id="trash") is True
    assert registry.remove_listener(id="missing") is False
    registry.create("test.action", {})
    assert fired == ["kept"]


def test_install_chatops_listener_is_idempotent(monkeypatch):
    """End-to-end: re-installing the chatops bridge does not stack listeners.
    Without HITL-4's id-based dedup, a second ``install_chatops_listener()``
    call (e.g. a test fixture or hot-reload path) would fire every chatops
    side-effect twice per approval."""
    from aiops.policy.approvals import install_chatops_listener
    from aiops.tools.chatops import ChatMessage, ChatOpsClient

    sent: list[ChatMessage] = []

    class _Capture:
        def send(self, msg: ChatMessage) -> None:
            sent.append(msg)

    client = ChatOpsClient()
    client.register(_Capture())
    monkeypatch.setattr("aiops.tools.chatops.client._CLIENT", client)

    reg = ApprovalRegistry(default_timeout_seconds=2)
    install_chatops_listener(reg)
    install_chatops_listener(reg)
    install_chatops_listener(reg)

    reg.create("test.action", {})
    # Exactly one "created" event per registered bridge.  Without dedup
    # this would be three.
    assert len(sent) == 1, [m.title for m in sent]
