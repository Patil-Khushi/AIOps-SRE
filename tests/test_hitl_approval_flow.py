"""End-to-end HITL approval flow tests (HITL-1, issue #77).

Validates the full path: agent → tool registry → gate → approval registry
→ chatops broadcast → approver decides → tool runs (or is blocked).
"""

from __future__ import annotations

import threading
import time

import pytest

from aiops.policy import (
    ApprovalRegistry,
    ApprovalRequester,
    ApprovalStatus,
    get_approval_registry,
    get_gate,
    install_chatops_listener,
)
from aiops.policy.gate import AutonomyLevel
from aiops.tools import ToolResult, get_registry, tool
from aiops.tools.chatops import ChatMessage, ChatOpsClient


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Reset the singletons + restore the gate's original approver."""
    original_approver = get_gate()._approver
    get_approval_registry()._reset_for_tests()
    yield
    get_gate()._approver = original_approver
    get_approval_registry()._reset_for_tests()


@pytest.fixture
def fake_chatops(monkeypatch):
    """Replace the singleton chatops client with a fresh one + capturing adapter."""
    captured: list[ChatMessage] = []

    class _Capture:
        def send(self, msg: ChatMessage) -> None:
            captured.append(msg)

    client = ChatOpsClient()
    client.register(_Capture())
    # The chatops listener resolves ``get_client()`` at call time, which
    # reads ``aiops.tools.chatops.client._CLIENT`` from the module dict.
    # Patching the module attribute is enough — the listener sees this client.
    monkeypatch.setattr("aiops.tools.chatops.client._CLIENT", client)
    yield captured


# ─── gate-level integration ───────────────────────────────────────────────


def test_required_action_blocks_when_approver_denies(monkeypatch, fake_chatops):
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.deny", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_deny",
        capability="test.hitl.deny",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="should not run")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=10)

    def denier():
        # Wait for the pending request to appear, then deny.
        for _ in range(50):
            time.sleep(0.02)
            pending = reg.list_pending()
            if pending:
                reg.decide(pending[0].id, approved=False, approver="sre@x.io", reason="risky")
                return
        raise AssertionError("no pending approval appeared")

    t = threading.Thread(target=denier, daemon=True)
    t.start()
    res = get_registry().call("test.hitl.deny")
    t.join(timeout=2)

    assert res.ok is False
    assert "blocked by HITL gate" in (res.error or "")
    # Chatops listener fired both "created" and "denied"
    titles = [m.title for m in fake_chatops]
    assert any("HITL approval requested" in t for t in titles)
    assert any("HITL approval denied" in t for t in titles)


def test_required_action_runs_when_approver_approves(monkeypatch, fake_chatops):
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.approve", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_approve",
        capability="test.hitl.approve",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="ran-after-approval")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=10)

    def approver():
        for _ in range(50):
            time.sleep(0.02)
            pending = reg.list_pending()
            if pending:
                reg.decide(pending[0].id, approved=True, approver="oncall@x.io")
                return
        raise AssertionError("no pending approval appeared")

    t = threading.Thread(target=approver, daemon=True)
    t.start()
    res = get_registry().call("test.hitl.approve")
    t.join(timeout=2)

    assert res.ok is True
    assert res.data == "ran-after-approval"


def test_required_action_expires_when_nobody_responds(monkeypatch, fake_chatops):
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.expire", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_expire",
        capability="test.hitl.expire",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="should not run")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=1)
    res = get_registry().call("test.hitl.expire")

    assert res.ok is False
    # The pending request should now be marked EXPIRED.
    all_reqs = reg.list_all()
    assert len(all_reqs) == 1
    assert all_reqs[0].status is ApprovalStatus.EXPIRED


def test_skip_approval_short_circuits_to_no_approver(monkeypatch):
    monkeypatch.setitem(get_gate()._levels, "test.hitl.skip", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_skip",
        capability="test.hitl.skip",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="never")

    reg = ApprovalRegistry(default_timeout_seconds=5)
    get_gate()._approver = ApprovalRequester(reg)
    res = get_registry().call("test.hitl.skip", hitl_context={"skip_approval": True})

    # Skip means we behave like the original "no approver" path: blocked.
    assert res.ok is False
    # And we did NOT spawn an approval request.
    assert reg.list_all() == []


def test_denied_action_error_message_includes_approver_id(monkeypatch, fake_chatops):
    """DoD #2: deny must produce ``reason="denied by <approver>"`` so the
    audit trail, agent, and UI all show who blocked the action."""
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.deny_reason", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_deny_reason",
        capability="test.hitl.deny_reason",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="should not run")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=10)

    def denier():
        for _ in range(50):
            time.sleep(0.02)
            pending = reg.list_pending()
            if pending:
                reg.decide(
                    pending[0].id,
                    approved=False,
                    approver="alice@example.com",
                    reason="blast radius too large",
                )
                return

    t = threading.Thread(target=denier, daemon=True)
    t.start()
    res = get_registry().call("test.hitl.deny_reason")
    t.join(timeout=2)

    assert res.ok is False
    assert "denied by alice@example.com" in (res.error or "")
    assert "blast radius too large" in (res.error or "")


def test_expired_action_error_message_says_expired(monkeypatch, fake_chatops):
    """DoD #3 surface check: timeout produces 'expired' wording, not 'approver missing'."""
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.expire_reason", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_expire_reason",
        capability="test.hitl.expire_reason",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="should not run")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=1)
    res = get_registry().call("test.hitl.expire_reason")

    assert res.ok is False
    assert "expired" in (res.error or "")


def test_pending_approval_id_is_surfaced_back_to_caller(monkeypatch, fake_chatops):
    install_chatops_listener()
    monkeypatch.setitem(get_gate()._levels, "test.hitl.id", AutonomyLevel.REQUIRED)

    @tool(
        name="test_required_id",
        capability="test.hitl.id",
        provider="test",
        description="test",
    )
    def fake(**_kwargs):
        return ToolResult(ok=True, data="ok")

    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=5)
    ctx: dict = {}

    def approver():
        for _ in range(50):
            time.sleep(0.02)
            pending = reg.list_pending()
            if pending:
                reg.decide(pending[0].id, approved=True, approver="alice")
                return

    t = threading.Thread(target=approver, daemon=True)
    t.start()
    get_registry().call("test.hitl.id", hitl_context=ctx)
    t.join(timeout=2)

    assert "pending_approval_id" in ctx
    assert reg.get(ctx["pending_approval_id"]).status is ApprovalStatus.APPROVED
