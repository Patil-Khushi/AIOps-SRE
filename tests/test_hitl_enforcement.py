"""HITL enforcement at the tool-registry seam (A12).

Validates CLAUDE.md principle #3: HITL must be enforced at the platform
boundary, not in agent code. Every ``ToolRegistry.call()`` consults
``aiops.policy.gate`` for the capability before invoking the tool function.
"""

from __future__ import annotations

import pytest

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


# ─── automation.fault.clear autonomy pin ──────────────────────────────────


def test_fault_clear_is_explicitly_autonomous(monkeypatch):
    """``automation.fault.clear`` must stay NONE, even under a stricter default.

    It is an inner hop: both routes to it (``rca.fix_step.execute`` and
    ``automation.runbook.execute``) are REQUIRED, so the human has already
    approved by the time it runs, and the inner dispatch forwards no
    hitl_context — gating it again would refuse an approved fix. The RCA agent
    also probes it on every grounding pass, which at REQUIRED would mint a
    pending approval per probe.

    Pinned rather than left to AIOPS_HITL_DEFAULT so setting that to "required"
    cannot silently deadlock the remediation path.
    """
    gate = get_gate()
    assert gate.level_for("automation.fault.clear") is AutonomyLevel.NONE

    monkeypatch.setenv("AIOPS_HITL_DEFAULT", "required")
    assert gate.level_for("automation.fault.clear") is AutonomyLevel.NONE


# ─── HITL-4 (#104) public approver setter / getter ────────────────────────


def test_gate_set_approver_replaces_current_function():
    """``HITLGate.set_approver`` is the supported way to swap approvers;
    the old private-attribute poke (``gate._approver = ...``) was the
    workaround it replaces."""
    gate = get_gate()
    original = gate.approver

    def custom(_action, _ctx):
        return "manual-approver"

    gate.set_approver(custom)
    try:
        assert gate.approver is custom
    finally:
        gate.set_approver(original)
    assert gate.approver is original


def test_gate_approver_property_is_read_only():
    """``HITLGate.approver`` exposes the current function for save/restore
    in tests; assigning to it must not silently shadow the real slot —
    if it did, ``gate.set_approver(saved)`` would restore a stale value."""
    gate = get_gate()
    original = gate.approver
    with pytest.raises(AttributeError):
        gate.approver = lambda *_a, **_k: None  # type: ignore[misc]
    assert gate.approver is original


def test_required_level_action_runs_with_approver(monkeypatch):
    gate = get_gate()
    monkeypatch.setitem(gate._levels, "test.hitl.approved", AutonomyLevel.REQUIRED)
    original_approver = gate.approver
    gate.set_approver(lambda action, ctx: "test-approver@example.com")

    @tool(name="test_required_approved", capability="test.hitl.approved", provider="test")
    def fake(**_kwargs):
        return ToolResult(ok=True, data="approved-and-ran")

    try:
        res = get_registry().call("test.hitl.approved")
        assert res.ok is True
        assert res.data == "approved-and-ran"
    finally:
        gate.set_approver(original_approver)
