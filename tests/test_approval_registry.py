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
