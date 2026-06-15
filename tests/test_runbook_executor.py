"""Tests for the Runbook Executor (RA-004).

Covers selection (service/tags/severity substring), markdown→plan parsing, and
every resolution path through the real platform seams: resolved (autonomous +
HITL-approved), denied at the gate, rolled_back on step failure, and failed when
a rollback step also fails. HITL is exercised at the registry boundary — the
agent never gate-checks itself.

Fault injection swaps in test providers for the ``automation.runbook.*``
capabilities via the registry's ``select_provider`` (the same seam production
uses to choose a backend), and restores the mock afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Side-effect import: registers the mock automation.runbook.* providers.
import aiops.tools.mock_providers  # noqa: F401
from agents.runbook_executor import (
    ExecutableRunbook,
    Incident,
    RunbookStep,
    execute_runbook,
    load_runbooks,
    run,
    run_plan,
    select,
)
from agents.runbook_executor.agent import APPLY_CAP, EXECUTE_CAP
from aiops.policy import ApproverResult, get_gate
from aiops.tools import ToolResult, get_registry
from aiops.tools.registry import Tool
from evals.scoring import score_case

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "runbook_executor"


# ─── fault-injection plumbing ────────────────────────────────────────────────

_FAULT: dict[str, set[str]] = {
    "exec_fail": set(),
    "exec_rollback_fail": set(),
    "apply_fail": set(),
    "apply_rollback_fail": set(),
}
FAULTY_EXEC = "test.faulty.automation.execute"
FAULTY_APPLY = "test.faulty.automation.apply"


def _reset_fault() -> None:
    for s in _FAULT.values():
        s.clear()


def _faulty_execute(
    runbook="", target="", namespace="", dry_run=True, step="", action="", mode="execute"
) -> ToolResult:
    if mode == "rollback":
        if step in _FAULT["exec_rollback_fail"]:
            return ToolResult(ok=False, error=f"rollback failed for {step}")
        return ToolResult(ok=True, data={"step": step, "mode": mode, "rolled_back": True})
    if step in _FAULT["exec_fail"]:
        return ToolResult(ok=False, error=f"execute failed for {step}")
    return ToolResult(ok=True, data={"step": step, "mode": mode, "executed": True})


def _faulty_apply(step="", target="", namespace="", action="", mode="execute") -> ToolResult:
    if mode == "rollback":
        if step in _FAULT["apply_rollback_fail"]:
            return ToolResult(ok=False, error=f"apply-rollback failed for {step}")
        return ToolResult(ok=True, data={"step": step, "mode": mode})
    if step in _FAULT["apply_fail"]:
        return ToolResult(ok=False, error=f"apply failed for {step}")
    return ToolResult(ok=True, data={"step": step, "mode": mode, "applied": True})


@pytest.fixture
def faulty_providers():
    """Swap the execute/apply capabilities to controllable fault-injecting
    providers, then restore the mock providers afterwards."""
    reg = get_registry()
    existing = {t.name for t in reg.list()}
    if FAULTY_EXEC not in existing:
        reg.register(Tool(FAULTY_EXEC, "fault inject", _faulty_execute, EXECUTE_CAP, "test"))
    if FAULTY_APPLY not in existing:
        reg.register(Tool(FAULTY_APPLY, "fault inject", _faulty_apply, APPLY_CAP, "test"))
    exec_prev = reg.by_capability(EXECUTE_CAP).name
    apply_prev = reg.by_capability(APPLY_CAP).name
    reg.select_provider(EXECUTE_CAP, FAULTY_EXEC)
    reg.select_provider(APPLY_CAP, FAULTY_APPLY)
    _reset_fault()
    try:
        yield _FAULT
    finally:
        _reset_fault()
        reg.select_provider(EXECUTE_CAP, exec_prev)
        reg.select_provider(APPLY_CAP, apply_prev)


@pytest.fixture
def auto_approve():
    """Install a synchronous always-approve approver so REQUIRED-HITL destructive
    steps execute. The autouse ``_hermetic_gate_approver`` fixture resets it
    afterwards; we reset here too for belt-and-braces."""
    get_gate().set_approver(lambda action, ctx: ApproverResult(approver="test-approver"))
    try:
        yield
    finally:
        get_gate().reset_approver()


def _destructive_pair_runbook() -> ExecutableRunbook:
    """Two destructive steps so a rollback also routes through the REQUIRED
    execute capability (single fault provider controls forward + rollback)."""
    return ExecutableRunbook(
        id="rb-pair",
        title="Two destructive steps",
        service="payment",
        severity="sev1",
        tags=["crash"],
        steps=[
            RunbookStep(
                name="step-one",
                action="act_one",
                destructive=True,
                rollback_action="undo_one",
                target="deployment/payment",
            ),
            RunbookStep(
                name="step-two",
                action="act_two",
                destructive=True,
                rollback_action="undo_two",
                target="deployment/payment",
            ),
        ],
    )


# ─── selection ───────────────────────────────────────────────────────────────


def test_selects_payment_runbook_by_service_and_tags():
    rb = select(Incident(service="payment", severity="sev1", tags=["oom", "crash"]))
    assert rb is not None
    assert rb.id == "payment-restart"


def test_selects_by_substring_service_spelling():
    rb = select(Incident(service="payment-service", tags=["crashloop"]))
    assert rb is not None and rb.id == "payment-restart"


def test_selects_cart_runbook_by_tag_substring():
    # "high-latency" must match the runbook tag "high-latency"/"latency".
    rb = select(Incident(service="cart", severity="sev2", tags=["high-latency"]))
    assert rb is not None and rb.id == "cartservice-scale-up"


def test_no_matching_runbook_returns_none():
    assert select(Incident(service="telemetry-aggregator", tags=["lag"])) is None


def test_execute_with_no_runbook_is_clean_no_runbook_status():
    out = execute_runbook(Incident(service="telemetry-aggregator", tags=["lag"]))
    assert out.status == "no_runbook"
    assert out.selected_runbook is None
    assert out.steps == []


# ─── parsing ─────────────────────────────────────────────────────────────────


def test_runbooks_parse_into_steps_with_flags():
    by_id = {rb.id: rb for rb in load_runbooks()}
    pay = by_id["payment-restart"]
    assert [s.name for s in pay.steps] == ["drain-connections", "restart-pods"]
    drain, restart = pay.steps
    assert drain.destructive is False
    assert restart.destructive is True
    assert restart.rollback_action == "rescale_previous"
    assert restart.action == "restart_deployment"


def test_three_runbooks_ship_in_the_library():
    ids = {rb.id for rb in load_runbooks()}
    assert {"payment-restart", "cartservice-scale-up", "checkout-rollback-deploy"} <= ids


# ─── resolved: autonomous (non-destructive only) ─────────────────────────────


def test_non_destructive_only_resolves_without_any_approver():
    rb = ExecutableRunbook(
        id="rb-safe",
        title="Safe",
        service="cart",
        steps=[
            RunbookStep(name="snapshot", action="snapshot_replicas", destructive=False),
            RunbookStep(name="healthcheck", action="healthcheck", destructive=False),
        ],
    )
    out = run_plan(Incident(service="cart"), rb)  # no approver installed
    assert out.status == "resolved"
    assert out.steps_executed == 2
    assert all(s.status == "executed" for s in out.steps)


# ─── denied: destructive step blocked at the gate ────────────────────────────


def test_destructive_step_denied_without_approver():
    out = execute_runbook(Incident(service="payment", severity="sev1", tags=["oom"]))
    assert out.status == "denied"
    # The safe drain ran; the destructive restart was blocked and nothing past it ran.
    drain, restart = out.steps
    assert drain.status == "executed"
    assert restart.status == "denied"
    assert out.steps_executed == 1
    assert "gate" in out.reason.lower()


# ─── resolved: destructive step approved via the HITL gate ───────────────────


def test_destructive_step_resolves_with_approver(auto_approve):
    out = execute_runbook(Incident(service="payment", severity="sev1", tags=["oom"]))
    assert out.status == "resolved"
    assert out.steps_executed == 2
    _drain, restart = out.steps
    assert restart.name == "restart-pods"
    assert restart.status == "executed"
    assert restart.executed is not None  # evidence captured


# ─── rolled_back: a step fails mid-plan, prior steps undone ──────────────────


def test_step_failure_rolls_back_prior_steps(faulty_providers, auto_approve):
    faulty_providers["exec_fail"].add("step-two")
    out = run_plan(Incident(service="payment"), _destructive_pair_runbook())
    assert out.status == "rolled_back"
    s1, s2 = out.steps
    assert s1.status == "rolled_back"  # executed then undone
    assert s1.rolled_back is True
    assert s2.status == "failed"
    # rollback happened in reverse and produced an artifact for step-one
    assert [a["step"] for a in out.rollback_artifacts] == ["step-one"]
    assert out.rollback_artifacts[0]["ok"] is True


# ─── failed: the rollback itself fails ───────────────────────────────────────


def test_rollback_failure_yields_failed(faulty_providers, auto_approve):
    faulty_providers["exec_fail"].add("step-two")
    faulty_providers["exec_rollback_fail"].add("step-one")
    out = run_plan(Incident(service="payment"), _destructive_pair_runbook())
    assert out.status == "failed"
    assert out.rollback_artifacts[0]["ok"] is False
    assert "manual intervention" in out.reason.lower()


# ─── dry-run preview ─────────────────────────────────────────────────────────


def test_dry_run_previews_every_step_even_when_gate_blocks_first_step():
    # checkout's first step is destructive -> denied immediately in phase 2,
    # but phase 1 must have previewed BOTH steps without executing anything.
    out = execute_runbook(Incident(service="checkout", severity="sev1", tags=["bad-deploy"]))
    assert out.status == "denied"
    assert out.steps_executed == 0
    assert len(out.steps) == 2
    for rec in out.steps:
        assert rec.simulate is not None
        assert rec.simulate.get("dry_run") is True
        assert rec.simulate.get("changes") == []


# ─── eval-harness contract + goldens ─────────────────────────────────────────


def test_run_returns_eval_dict():
    out = run({"service": "payment", "severity": "sev1", "tags": ["oom"]})
    assert out["selected_runbook"] == "payment-restart"
    assert out["status"] == "denied"
    assert out["steps_total"] == 2
    assert out["destructive_steps"] == 1
    assert out["steps_executed"] == 1


def test_run_no_runbook_dict():
    out = run({"service": "telemetry-aggregator", "tags": ["lag"]})
    assert out["status"] == "no_runbook"
    assert out["selected_runbook"] is None


def test_golden_cases_all_pass():
    golden = json.loads((AGENT_DIR / "evals" / "golden.json").read_text(encoding="utf-8"))
    for case in golden["cases"]:
        actual = run(case["input"])
        scored = score_case(actual=actual, expected=case["expected"])
        assert scored["passed"], f"{case['id']}: {scored['details']}"
