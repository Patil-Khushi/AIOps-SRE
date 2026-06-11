"""Unit tests for PRS-002 generic surface — ``agents.auto_healer_lite.execute``.

Covers the v1 paths that the eval-harness goldens can't deterministically
hit (because they require an installed approver and a registered tool
capability):

- DRY_RUN_OK happy path (gate clears, dry_run=True, no tool call).
- EXECUTED happy path (gate clears, dry_run=False, tool returns ok).
- EXECUTION_FAILED — tool returns ok=False.
- EXECUTION_FAILED — tool capability not registered.
- EXECUTION_FAILED — tool raises an exception (boundary catch).
- BLOCKED — gate denies (denied summary status).
- ExecutionRow is persisted on every outcome (REFUSED + EXECUTED + BLOCKED).

The legacy HITL-1 path (``recommend_restart``) keeps its own test file
(``tests/test_auto_healer_lite.py``); these are PRS-002-only.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.auto_healer_lite import ExecutionRequest, ExecutionStatus, execute
from aiops import state as state_pkg
from aiops.policy import get_approval_registry, get_gate
from aiops.policy.gate import ApprovalSummary, ApproverResult
from aiops.state.repository import list_executions
from aiops.tools import get_registry
from aiops.tools.registry import ToolResult

# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _in_memory_state(monkeypatch):
    """Fresh in-memory SQLite DB per test so ExecutionRow rows don't leak."""
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot + restore the global tool registry around each test.

    Without this, ``_register_tool`` leaks its stub into the singleton
    ``get_registry()`` and downstream test files (e.g. the flagd
    adapter's suite) inherit our fake. CI catches this — local runs
    pass because pytest may collect those tests in a different order.
    """
    reg = get_registry()
    snapshot_active = dict(reg._active)  # type: ignore[attr-defined]
    snapshot_tools = dict(reg._tools)  # type: ignore[attr-defined]
    yield
    reg._active.clear()  # type: ignore[attr-defined]
    reg._active.update(snapshot_active)  # type: ignore[attr-defined]
    reg._tools.clear()  # type: ignore[attr-defined]
    reg._tools.update(snapshot_tools)  # type: ignore[attr-defined]


@pytest.fixture
def _isolate_gate():
    """Reset the singleton gate approver around each test."""
    original = get_gate().approver
    get_approval_registry()._reset_for_tests()
    yield
    get_gate().set_approver(original)
    get_approval_registry()._reset_for_tests()


def _install_yes_approver():
    """Install a synchronous approver that always says yes."""

    def _yes(action: str, ctx: dict[str, Any]) -> ApproverResult:
        return ApproverResult(
            approver="alice@example.com",
            summary=ApprovalSummary(
                id="appr-test-1",
                status="approved",
                approver="alice@example.com",
                reason="auto-approved for test",
            ),
        )

    get_gate().set_approver(_yes)


def _install_no_approver():
    """Install a synchronous approver that always denies."""

    def _no(action: str, ctx: dict[str, Any]) -> ApproverResult:
        return ApproverResult(
            approver=None,
            summary=ApprovalSummary(
                id="appr-test-deny",
                status="denied",
                approver="alice@example.com",
                reason="too risky for test",
            ),
        )

    get_gate().set_approver(_no)


def _register_tool(capability: str, *, ok: bool = True, error: str | None = None):
    """Register a synthetic ``feature_flags.set_variant`` test tool.

    The platform registry filters kwargs through ``inspect.signature``
    before dispatch, so the test fn must declare ``flag`` + ``variant``
    as named parameters to receive them. ``**kwargs`` alone would be
    filtered to an empty dict — that's a real platform constraint we
    work with rather than against.

    Returns the captured-calls list so the test can assert on it.
    """
    captured: list[dict[str, Any]] = []

    def _fn(flag: str, variant: str = "off") -> ToolResult:
        captured.append({"flag": flag, "variant": variant})
        if ok:
            return ToolResult(ok=True, data={"called": True, "flag": flag, "variant": variant})
        return ToolResult(ok=False, error=error or "synthetic failure")

    name = f"test.{capability.replace('.', '-')}-{ok}-{error or ''}"[:60]
    from aiops.tools.registry import Tool

    get_registry()._tools[name] = Tool(  # type: ignore[attr-defined]
        name=name,
        capability=capability,
        provider="test",
        description=f"test stub for {capability}",
        fn=_fn,
    )
    get_registry()._active[capability] = name  # type: ignore[attr-defined]
    return captured


def _option(**overrides) -> dict[str, Any]:
    base = {
        "option_id": "rca-step-2",
        "action_type": "set_flag",
        "blast_radius": "low",
        "tool_capability": "feature_flags.set_variant",
        "tool_args": {"flag": "paymentEventsV2", "variant": "off"},
        "rollback": "flip on",
        "requires_hitl": True,
    }
    base.update(overrides)
    return base


# ─── Happy paths ───────────────────────────────────────────────────────────


def test_dry_run_ok_when_gate_clears_and_dry_run_true(_isolate_gate):
    _install_yes_approver()
    captured = _register_tool("feature_flags.set_variant")

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=True,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    assert v.status == ExecutionStatus.DRY_RUN_OK
    assert v.decision.allowed is True
    assert v.would_execute is True  # would have called the tool
    assert v.tool_result is None  # but didn't
    assert captured == []  # tool was NOT invoked


def test_executed_when_gate_clears_and_dry_run_false(_isolate_gate):
    _install_yes_approver()
    captured = _register_tool("feature_flags.set_variant")

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    assert v.status == ExecutionStatus.EXECUTED
    assert v.decision.allowed is True
    assert v.would_execute is False  # actually executed, no "would" implied
    assert v.tool_result is not None
    assert v.tool_result["ok"] is True
    assert v.error is None
    assert len(captured) == 1
    assert captured[0] == {"flag": "paymentEventsV2", "variant": "off"}


# ─── Failure modes ─────────────────────────────────────────────────────────


def test_execution_failed_when_tool_returns_not_ok(_isolate_gate):
    _install_yes_approver()
    _register_tool("feature_flags.set_variant", ok=False, error="flagd unreachable")

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    assert v.status == ExecutionStatus.EXECUTION_FAILED
    assert v.decision.allowed is True  # gate cleared
    assert v.tool_result is not None
    assert v.tool_result["ok"] is False
    assert "flagd unreachable" in (v.error or "")


def test_execution_failed_when_tool_capability_not_registered(_isolate_gate):
    _install_yes_approver()
    # NOTE: no _register_tool call — capability is not in the registry.
    # We need to first clear any active provider for this capability so
    # the registry raises KeyError instead of dispatching to the mock.
    reg = get_registry()
    reg._active.pop("feature_flags.set_variant", None)  # type: ignore[attr-defined]

    req = ExecutionRequest(
        option=_option(tool_capability="feature_flags.does_not_exist"),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    assert v.status == ExecutionStatus.EXECUTION_FAILED
    assert "not registered" in (v.error or "")
    assert v.tool_result is None


def test_execution_failed_when_tool_raises(_isolate_gate):
    _install_yes_approver()

    def _raises(**kwargs: Any) -> ToolResult:
        raise RuntimeError("tool exploded")

    from aiops.tools.registry import Tool

    get_registry()._tools["test-raises"] = Tool(  # type: ignore[attr-defined]
        name="test-raises",
        capability="feature_flags.set_variant",
        provider="test",
        description="raises",
        fn=_raises,
    )
    get_registry()._active["feature_flags.set_variant"] = "test-raises"  # type: ignore[attr-defined]

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    # ToolRegistry.call() catches the exception itself and returns ok=False.
    # So this surfaces as a failed-tool-result rather than an exception
    # caught by the agent — both paths are valid; the agent reports
    # EXECUTION_FAILED with the wrapped error string.
    assert v.status == ExecutionStatus.EXECUTION_FAILED
    assert v.error is not None


def test_blocked_when_gate_denies(_isolate_gate):
    _install_no_approver()

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)

    assert v.status == ExecutionStatus.BLOCKED
    assert v.decision.allowed is False
    assert v.tool_result is None


# ─── Persistence ───────────────────────────────────────────────────────────


def test_executed_row_persisted(_isolate_gate):
    _install_yes_approver()
    _register_tool("feature_flags.set_variant")

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)
    rows = list_executions(affected_service="payment")

    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == v.request_id
    assert row["option_id"] == "rca-step-2"
    assert row["status"] == "executed"
    assert row["dry_run"] is False
    assert row["tool_capability"] == "feature_flags.set_variant"
    assert row["tool_result"]["ok"] is True
    assert row["error"] is None


def test_refused_row_also_persisted():
    """Even REFUSED outcomes get a row — the dashboard history view
    needs to see "we tried, here's why we declined" too."""
    # No gate isolation needed; we never reach the gate on REFUSED.
    req = ExecutionRequest(
        option={
            "option_id": "bad-1",
            "action_type": "set_flag",
            "rollback": "x",
        },  # missing requires_hitl
        affected_service="payment",
    )

    v = execute(req)
    rows = list_executions(affected_service="payment")

    assert v.status == ExecutionStatus.REFUSED
    assert len(rows) == 1
    assert rows[0]["status"] == "refused"
    assert rows[0]["option_id"] == "bad-1"


def test_blocked_row_persisted(_isolate_gate):
    _install_no_approver()

    req = ExecutionRequest(
        option=_option(),
        affected_service="payment",
        dry_run=False,
        hitl_context={"skip_approval": False},
    )

    v = execute(req)
    rows = list_executions(affected_service="payment")

    assert v.status == ExecutionStatus.BLOCKED
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert rows[0]["decision"]["allowed"] is False
