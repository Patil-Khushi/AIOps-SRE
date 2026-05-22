"""End-to-end test of the Auto-Healer-lite HITL demo agent (HITL-1, issue #77)."""

from __future__ import annotations

import threading
import time

import pytest

from agents.auto_healer_lite import RestartRecommendation, recommend_restart
from aiops.policy import (
    ApprovalRequester,
    ApprovalStatus,
    get_approval_registry,
    get_gate,
)


@pytest.fixture(autouse=True)
def _isolate_state():
    original_approver = get_gate()._approver
    get_approval_registry()._reset_for_tests()
    yield
    get_gate()._approver = original_approver
    get_approval_registry()._reset_for_tests()


def test_agent_executes_after_approver_says_yes():
    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=5)

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
    outcome = recommend_restart(
        RestartRecommendation(deployment="product-catalog", reason="stuck pod")
    )
    t.join(timeout=2)

    assert outcome.status == "executed"
    assert outcome.approval_id is not None
    assert outcome.result is not None
    assert outcome.result["dry_run"] is True
    assert outcome.result["target"] == "deployment/product-catalog"


def test_agent_reports_denied_when_approver_says_no():
    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=5)

    def denier():
        for _ in range(50):
            time.sleep(0.02)
            pending = reg.list_pending()
            if pending:
                reg.decide(pending[0].id, approved=False, approver="sre@x.io", reason="risky")
                return

    t = threading.Thread(target=denier, daemon=True)
    t.start()
    outcome = recommend_restart(
        RestartRecommendation(deployment="product-catalog", reason="stuck pod")
    )
    t.join(timeout=2)

    assert outcome.status == "denied"
    assert outcome.approval_id is not None
    assert reg.get(outcome.approval_id).status is ApprovalStatus.DENIED


def test_agent_reports_expired_when_nobody_responds():
    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg, timeout_seconds=1)

    outcome = recommend_restart(
        RestartRecommendation(deployment="product-catalog", reason="stuck pod")
    )

    assert outcome.status == "expired"
    assert outcome.approval_id is not None
    assert reg.get(outcome.approval_id).status is ApprovalStatus.EXPIRED


def test_skip_approval_yields_blocked_outcome():
    """With ``skip_approval=True`` the agent does not spawn a request and the
    gate returns blocked.  This is the eval-harness path so goldens never
    deadlock.  Status is "blocked" not "denied" because we never opened a
    request to deny."""
    reg = get_approval_registry()
    get_gate()._approver = ApprovalRequester(reg)

    outcome = recommend_restart(
        RestartRecommendation(deployment="product-catalog", reason="x"),
        hitl_context={"skip_approval": True},
    )

    assert outcome.status == "blocked"
    assert outcome.approval_id is None
    assert reg.list_all() == []
