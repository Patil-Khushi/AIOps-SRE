"""Tests for RA-004's audit event log + simulation detail/comparison (issue #213).

Covers:
- event completeness (a corresponding event for every step state transition),
- the append-only / immutability guarantee (frozen events + metadata, tuple on
  the result, no mutation API on EventLog),
- HITL_APPROVED emitted on gate-not-blocked even when the tool later fails,
- the simulation-vs-execution comparison across executed / failed / rolled_back.

HITL is exercised at the registry boundary — these tests never gate-check the
agent, matching the rest of the RA-004 suite. Fault providers are swapped in via
``select_provider`` (the same seam production uses) and restored afterwards.
"""

from __future__ import annotations

import pydantic
import pytest

# Side-effect import: registers the mock automation.runbook.* providers.
import aiops.tools.mock_providers  # noqa: F401
from agents.runbook_executor import (
    AuditEventType,
    EventLog,
    ExecutableRunbook,
    Incident,
    RunbookStatus,
    RunbookStep,
    execute_runbook,
    run_plan,
)
from agents.runbook_executor.agent import APPLY_CAP, EXECUTE_CAP
from aiops.policy import ApproverResult, get_gate
from aiops.tools import ToolResult, get_registry
from aiops.tools.registry import Tool

# ─── fault-injection plumbing (mirrors test_runbook_executor.py) ─────────────

_FAIL: dict[str, set[str]] = {"apply": set(), "exec": set(), "exec_rb": set()}
_T_EXEC = "test.audit.execute"
_T_APPLY = "test.audit.apply"


def _t_exec(
    runbook="",
    target="",
    namespace="",
    dry_run=True,
    step="",
    action="",
    mode="execute",
) -> ToolResult:
    if mode == "rollback":
        if step in _FAIL["exec_rb"]:
            return ToolResult(ok=False, error=f"rollback failed {step}")
        return ToolResult(ok=True, data={"step": step, "mode": mode})
    if step in _FAIL["exec"]:
        return ToolResult(ok=False, error=f"execute failed {step}")
    return ToolResult(
        ok=True,
        data={
            "step": step,
            "actual_side_effects": [f"{action}:{target}"],
            "duration_ms": 1500,
        },
    )


def _t_apply(step="", target="", namespace="", action="", mode="execute") -> ToolResult:
    if mode == "rollback":
        return ToolResult(ok=True, data={"step": step, "mode": mode})
    if step in _FAIL["apply"]:
        return ToolResult(ok=False, error=f"apply failed {step}")
    return ToolResult(
        ok=True,
        data={
            "step": step,
            "actual_side_effects": [f"{action}:{target}"],
            "duration_ms": 500,
        },
    )


@pytest.fixture
def faulty():
    reg = get_registry()
    existing = {t.name for t in reg.list()}
    if _T_EXEC not in existing:
        reg.register(Tool(_T_EXEC, "fault inject", _t_exec, EXECUTE_CAP, "test"))
    if _T_APPLY not in existing:
        reg.register(Tool(_T_APPLY, "fault inject", _t_apply, APPLY_CAP, "test"))
    exec_prev = reg.by_capability(EXECUTE_CAP).name
    apply_prev = reg.by_capability(APPLY_CAP).name
    reg.select_provider(EXECUTE_CAP, _T_EXEC)
    reg.select_provider(APPLY_CAP, _T_APPLY)
    for s in _FAIL.values():
        s.clear()
    try:
        yield _FAIL
    finally:
        for s in _FAIL.values():
            s.clear()
        reg.select_provider(EXECUTE_CAP, exec_prev)
        reg.select_provider(APPLY_CAP, apply_prev)


@pytest.fixture
def approve():
    """Synchronous always-approve approver so REQUIRED destructive steps clear."""
    get_gate().set_approver(lambda action, ctx: ApproverResult(approver="tester"))
    try:
        yield
    finally:
        get_gate().reset_approver()


def _safe_runbook() -> ExecutableRunbook:
    return ExecutableRunbook(
        id="rb-safe",
        title="Safe",
        service="order-service",
        status=RunbookStatus.ACTIVE,
        approved_by="test",
        steps=[
            RunbookStep(
                name="snap",
                action="snapshot_replicas",
                destructive=False,
                target="deployment/cart",
            ),
            RunbookStep(
                name="hc",
                action="healthcheck",
                destructive=False,
                target="deployment/cart",
            ),
        ],
    )


def _destructive_pair() -> ExecutableRunbook:
    return ExecutableRunbook(
        id="rb-pair",
        title="pair",
        service="payment-service",
        severity="sev3",
        tags=["crash"],
        status=RunbookStatus.ACTIVE,
        approved_by="test",
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


def _by_step(execution) -> dict[str, list[AuditEventType]]:
    out: dict[str, list[AuditEventType]] = {}
    for e in execution.audit_events:
        out.setdefault(e.step_id, []).append(e.status)
    return out


# ─── event shape ─────────────────────────────────────────────────────────────


def test_event_shape_matches_issue_spec():
    ex = execute_runbook(
        Incident(incident_id="INC-9", service="payment-service", severity="sev3", tags=["restart"]),
        hitl_context={"skip_approval": True},
    )
    d = ex.audit_events[0].model_dump(mode="json")
    assert set(d) >= {
        "incident_id",
        "runbook_id",
        "step_id",
        "timestamp",
        "status",
        "metadata",
    }
    assert set(d["metadata"]) >= {"reason", "gate_type", "approval_id"}
    assert d["incident_id"] == "INC-9"
    assert d["runbook_id"] == "payment-service-restart"


# ─── event completeness (no gaps) ────────────────────────────────────────────


def test_denied_run_emits_full_event_sequence():
    ex = execute_runbook(
        Incident(
            incident_id="INC-1",
            service="payment-service",
            severity="sev3",
            tags=["restart", "generic"],
        ),
        hitl_context={"skip_approval": True},
    )
    assert ex.status == "denied"
    types = [e.status for e in ex.audit_events]
    # both steps previewed in phase 1
    # 3, not 2: the generic restart runbook is drain -> restart -> verify.
    assert types.count(AuditEventType.STEP_SIMULATED) == 3
    by = _by_step(ex)
    # safe drain: started → gate(none) → executed
    assert AuditEventType.STEP_STARTED in by["drain-connections"]
    assert AuditEventType.STEP_EXECUTED in by["drain-connections"]
    # destructive restart: started → gate(required) → blocked; never "approved"
    assert AuditEventType.STEP_STARTED in by["restart-pods"]
    assert AuditEventType.STEP_BLOCKED in by["restart-pods"]
    assert AuditEventType.HITL_APPROVED not in by["restart-pods"]


def test_gate_checked_carries_correct_gate_type():
    ex = execute_runbook(
        Incident(service="payment-service", severity="sev3", tags=["restart"]),
        hitl_context={"skip_approval": True},
    )
    gate_events = [e for e in ex.audit_events if e.status == AuditEventType.GATE_CHECKED]
    by_type = {e.step_id: e.metadata.gate_type for e in gate_events}
    assert by_type["drain-connections"] == "none"  # non-destructive
    assert by_type["restart-pods"] == "required"  # destructive


def test_resolved_run_emits_hitl_approved(approve):
    ex = execute_runbook(Incident(service="payment-service", severity="sev3", tags=["restart"]))
    assert ex.status == "resolved"
    by = _by_step(ex)
    assert AuditEventType.HITL_APPROVED in by["restart-pods"]
    assert AuditEventType.STEP_EXECUTED in by["restart-pods"]


def test_seq_is_monotonic_and_gapless():
    ex = execute_runbook(
        Incident(service="order-service", severity="sev2", tags=["latency", "load"]),
        hitl_context={"skip_approval": True},
    )
    seqs = [e.seq for e in ex.audit_events]
    assert seqs == list(range(len(seqs)))


def test_hitl_approved_recorded_even_when_execution_then_fails(faulty, approve):
    """Regression for the fix: approval is a distinct fact from tool success.
    An approved destructive step whose tool then fails must still show
    HITL_APPROVED (approval happened), followed by STEP_FAILED."""
    faulty["exec"].add("restart-pods")
    ex = execute_runbook(Incident(service="payment-service", severity="sev3", tags=["restart"]))
    by = _by_step(ex)
    assert AuditEventType.HITL_APPROVED in by["restart-pods"]
    assert AuditEventType.STEP_FAILED in by["restart-pods"]


# ─── append-only / immutability ──────────────────────────────────────────────


def test_audit_event_is_frozen():
    log = EventLog(incident_id="i", runbook_id="r")
    ev = log.emit(AuditEventType.STEP_STARTED, step_id="s")
    with pytest.raises((pydantic.ValidationError, TypeError)):
        ev.status = AuditEventType.STEP_FAILED  # type: ignore[misc]


def test_audit_event_metadata_is_frozen():
    log = EventLog(incident_id="i", runbook_id="r")
    ev = log.emit(AuditEventType.GATE_CHECKED, step_id="s", reason="original")
    with pytest.raises((pydantic.ValidationError, TypeError)):
        ev.metadata.reason = "tampered"  # type: ignore[misc]


def test_eventlog_exposes_no_mutation_api():
    log = EventLog(incident_id="i", runbook_id="r")
    for attr in ("update", "delete", "remove", "pop", "clear", "insert", "__setitem__"):
        assert not hasattr(log, attr), f"EventLog must not expose {attr!r}"
    log.emit(AuditEventType.STEP_STARTED, step_id="s")
    assert isinstance(log.events, tuple)


def test_execution_audit_events_is_an_immutable_tuple():
    ex = execute_runbook(
        Incident(service="payment-service", severity="sev3", tags=["restart"]),
        hitl_context={"skip_approval": True},
    )
    assert isinstance(ex.audit_events, tuple)
    # A tuple has no append/pop, so the serialized log can't be grown/trimmed.
    with pytest.raises(AttributeError):
        ex.audit_events.append(ex.audit_events[0])  # type: ignore[attr-defined]


# ─── simulation-vs-execution comparison across all three outcomes ────────────


def test_comparison_on_executed_step_matches_prediction():
    ex = run_plan(Incident(service="order-service"), _safe_runbook())
    assert ex.status == "resolved"
    for rec in ex.steps:
        assert rec.status == "executed"
        assert rec.comparison is not None
        assert rec.comparison.matched is True
        assert rec.comparison.actual_side_effects == rec.comparison.predicted_side_effects


def test_comparison_attached_on_failed_step(faulty):
    faulty["apply"].add("snap")  # first (non-destructive) step fails
    ex = run_plan(Incident(service="order-service"), _safe_runbook())
    snap = next(s for s in ex.steps if s.name == "snap")
    assert snap.status == "failed"
    assert snap.comparison is not None
    # Nothing actually executed → predicted effects reported as "missing".
    assert snap.comparison.matched is False
    assert snap.comparison.missing_side_effects == snap.simulation.predicted_side_effects


def test_comparison_attached_on_rolled_back_step(faulty, approve):
    faulty["exec"].add("step-two")  # 2nd destructive step fails → roll back step-one
    ex = run_plan(Incident(service="payment-service"), _destructive_pair())
    assert ex.status == "rolled_back"
    s1 = next(s for s in ex.steps if s.name == "step-one")
    assert s1.status == "rolled_back"
    # The forward comparison computed when step-one executed is preserved.
    assert s1.comparison is not None
    by = _by_step(ex)
    assert AuditEventType.STEP_ROLLED_BACK in by["step-one"]
    assert AuditEventType.STEP_FAILED in by["step-two"]
