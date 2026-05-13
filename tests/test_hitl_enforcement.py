"""HITL enforcement at the tool-registry seam (A12).

Validates CLAUDE.md principle #3: HITL must be enforced at the platform
boundary, not in agent code. Every ``ToolRegistry.call()`` consults
``aiops.policy.gate`` for the capability before invoking the tool function.
"""

from __future__ import annotations

from aiops.policy import get_gate
from aiops.policy.gate import AutonomyLevel
from aiops.tools import ToolResult, get_registry, tool


def test_required_level_action_blocks_without_approver(monkeypatch):
    monkeypatch.setitem(get_gate()._levels, "test.hitl.required", AutonomyLevel.REQUIRED)

    @tool(name="test_required_block", capability="test.hitl.required", provider="test")
    def fake(**_kwargs):
        return ToolResult(ok=True, data="should not run")

    res = get_registry().call("test.hitl.required")
    assert res.ok is False
    assert res.error.startswith("blocked by HITL gate"), res.error
    assert res.metadata["blocked_by"] == "hitl_gate"
    assert res.metadata["level"] == "required"
    assert res.metadata["capability"] == "test.hitl.required"
    assert res.data != "should not run", "Tool function must not have been invoked"


def test_none_level_action_passes_through(monkeypatch):
    monkeypatch.setitem(get_gate()._levels, "test.hitl.none", AutonomyLevel.NONE)

    @tool(name="test_none_allow", capability="test.hitl.none", provider="test")
    def fake(**_kwargs):
        return ToolResult(ok=True, data="ran")

    res = get_registry().call("test.hitl.none")
    assert res.ok is True
    assert res.data == "ran"
    assert "blocked_by" not in res.metadata


def test_required_level_action_runs_with_approver(monkeypatch):
    gate = get_gate()
    monkeypatch.setitem(gate._levels, "test.hitl.approved", AutonomyLevel.REQUIRED)
    monkeypatch.setattr(gate, "_approver", lambda action, ctx: "test-approver@example.com")

    @tool(name="test_required_approved", capability="test.hitl.approved", provider="test")
    def fake(**_kwargs):
        return ToolResult(ok=True, data="approved-and-ran")

    res = get_registry().call("test.hitl.approved")
    assert res.ok is True
    assert res.data == "approved-and-ran"
