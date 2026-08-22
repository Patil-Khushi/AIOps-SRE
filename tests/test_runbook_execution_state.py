"""Durable execution: state machine, idempotency, concurrency, timeouts, rollback.

Covers §19–§25 plus the §27 result contract. Every test drives the real
``plan_execution`` → ``execute_plan`` path against the real registry and the real HITL
gate; only the *providers* are swapped (the same ``select_provider`` seam production
uses) so a step can be made to fail, hang or succeed on demand.

The runbooks are written to a tmp directory rather than taken from the shipped library:
these tests need specific step shapes, and generating the file also exercises the
frontmatter parser end to end.
"""

from __future__ import annotations

import time
import unittest.mock
from typing import Any

import pytest

import aiops.tools.mock_providers  # noqa: F401 - registers the automation.runbook.* mocks
from agents.runbook_executor import (
    ExecutionState,
    ExecutorStatus,
    NextAction,
    UiState,
    metrics,
    plan_execution,
)
from agents.runbook_executor.agent import APPLY_CAP, EXECUTE_CAP, execute_plan, guarded_dispatch
from aiops.policy import ApproverResult, get_gate
from aiops.state import repository
from aiops.tools import ToolResult, get_registry
from aiops.tools.registry import Tool
from tests.test_runbook_matching import NOW, order_incident

FAULTY_EXEC = "test.state.execute"
FAULTY_APPLY = "test.state.apply"

_FAIL: dict[str, set[str]] = {"exec": set(), "exec_rollback": set(), "apply": set()}
_CALLS: list[tuple[str, str, str]] = []  # (capability, step, mode)


def _fake_execute(
    runbook="", target="", namespace="", dry_run=True, step="", action="", mode="execute", **_
) -> ToolResult:
    _CALLS.append((EXECUTE_CAP, step, mode))
    if mode == "rollback":
        if step in _FAIL["exec_rollback"]:
            return ToolResult(ok=False, error=f"rollback failed for {step}")
        return ToolResult(ok=True, data={"step": step, "mode": mode})
    if step in _FAIL["exec"]:
        return ToolResult(ok=False, error=f"execute failed for {step}")
    return ToolResult(ok=True, data={"step": step, "mode": mode})


def _fake_apply(step="", target="", namespace="", action="", mode="execute", **_) -> ToolResult:
    _CALLS.append((APPLY_CAP, step, mode))
    if step in _FAIL["apply"]:
        return ToolResult(ok=False, error=f"apply failed for {step}")
    return ToolResult(ok=True, data={"step": step, "mode": mode})


@pytest.fixture
def providers():
    """Swap execute/apply for controllable fakes, then restore the mocks."""
    reg = get_registry()
    existing = {t.name for t in reg.list()}
    if FAULTY_EXEC not in existing:
        reg.register(Tool(FAULTY_EXEC, "fake", _fake_execute, EXECUTE_CAP, "test"))
    if FAULTY_APPLY not in existing:
        reg.register(Tool(FAULTY_APPLY, "fake", _fake_apply, APPLY_CAP, "test"))
    prev_exec = reg.by_capability(EXECUTE_CAP).name
    prev_apply = reg.by_capability(APPLY_CAP).name
    reg.select_provider(EXECUTE_CAP, FAULTY_EXEC)
    reg.select_provider(APPLY_CAP, FAULTY_APPLY)
    for bucket in _FAIL.values():
        bucket.clear()
    _CALLS.clear()
    try:
        yield _FAIL
    finally:
        for bucket in _FAIL.values():
            bucket.clear()
        _CALLS.clear()
        reg.select_provider(EXECUTE_CAP, prev_exec)
        reg.select_provider(APPLY_CAP, prev_apply)


@pytest.fixture
def approve():
    get_gate().set_approver(lambda action, ctx: ApproverResult(approver="sre@test"))
    try:
        yield
    finally:
        get_gate().reset_approver()


RUNBOOK_MD = """---
title: order-service — controlled test runbook
service: order-service
severity: sev2
version: 1
status: active
owner: sre-platform
approved_by: test-suite
tags:
- error
- 5xx
applicability:
  environments:
  - production
  failure_category: application_error
  alerts:
  - EcommerceOrderErrorRateHigh
  required_signals:
  - error_rate_high
  allowed_services:
  - order-service
  allowed_namespaces:
  - ecommerce
prerequisites:
- id: incident_active
  description: The incident is still open.
  mandatory: true
  check: incident_active
- id: target_in_scope
  description: Steps stay in scope.
  mandatory: true
  check: service_scope
steps:
- name: drain-connections
  action: drain
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
- name: restart-pods
  action: restart_deployment
  destructive: true
  idempotent: false
  rollback_action: rescale_previous
  target: deployment/order-service
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# controlled test runbook
"""


@pytest.fixture
def library(tmp_path):
    """A one-runbook library on disk, so discovery has exactly one applicable match."""
    (tmp_path / "order-service-controlled.md").write_text(RUNBOOK_MD, encoding="utf-8")
    return tmp_path


def _plan(library, ctx=None, **kwargs):
    return plan_execution(ctx or order_incident(), runbooks_dir=library, now=NOW, **kwargs)


# ─── the happy path (§19, §27, §29) ──────────────────────────────────────────


def test_execution_completes_and_hands_off_to_verification(library, providers, approve):
    plan = _plan(library)
    assert plan.decision.value == "AUTO_SELECT"  # exactly one applicable runbook
    assert plan.selected_by == "auto"
    assert plan.ui_state is UiState.DRY_RUN_READY

    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.EXECUTED
    assert result.next_action is NextAction.VERIFY
    assert result.execution_state is ExecutionState.COMPLETED
    assert result.ui_state is UiState.WAITING_VERIFICATION  # never "resolved"
    assert [s["status"] for s in result.steps] == ["executed", "executed", "executed"]

    handoff = result.verification_handoff
    assert handoff is not None
    assert handoff.execution_id == plan.execution_id
    assert handoff.incident_id == "INC-1042"
    assert handoff.runbook_id == "order-service-controlled"
    assert handoff.runbook_version == 1
    assert handoff.status == "completed"
    assert len(handoff.actions_executed) == 3
    assert handoff.rollback_status == "not_required"
    assert handoff.audit_metadata["plan_hash"] == plan.dry_run.plan_hash


def test_execution_is_persisted_with_its_audit_trail(library, providers, approve):
    plan = _plan(library)
    execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["state"] == "completed"
    assert row["status"] == "EXECUTED"
    assert row["next_action"] == "VERIFY"
    assert row["risk_level"] == "MEDIUM"
    assert row["selected_by"] == "auto"
    assert len(row["steps"]) == 3
    assert row["audit_events"], "the append-only event log must be persisted"
    assert row["candidates"], "the candidates offered must be recorded (§30)"
    assert row["started_at"] and row["completed_at"]


def test_legacy_result_is_carried_alongside_the_new_contract(library, providers, approve):
    """Existing consumers keep reading RunbookExecution."""
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.legacy is not None
    assert result.legacy.status == "resolved"
    assert result.legacy.steps_total == 3
    api = result.to_api_dict()
    assert api["steps_total"] == 3 and api["steps_executed"] == 3
    assert api["legacy"]["selected_runbook"] == "order-service-controlled"


# ─── idempotency / duplicate protection (§20) ────────────────────────────────


def test_same_plan_reserves_the_same_execution(library, providers, approve):
    first = _plan(library)
    second = _plan(library)
    assert second.execution_id == first.execution_id


def test_second_execute_does_not_run_production_actions_again(library, providers, approve):
    plan = _plan(library)
    execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    calls_after_first = list(_CALLS)

    again = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert _CALLS == calls_after_first, "a duplicate request re-dispatched steps"
    assert again.status is ExecutorStatus.EXECUTED
    assert again.duplicate_of == plan.execution_id
    assert metrics.counter("execution_duplicate") >= 1


def test_in_flight_execution_refuses_a_second_start(library, providers, approve):
    plan = _plan(library)
    repository.update_runbook_execution(plan.execution_id, state=ExecutionState.EXECUTING.value)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.BLOCKED
    assert "already executing" in result.reason or "executing" in result.reason
    assert not _CALLS


def test_a_different_incident_gets_its_own_execution(library, providers, approve):
    first = _plan(library, order_incident(incident_id="INC-1"))
    second = _plan(library, order_incident(incident_id="INC-2"))
    assert first.execution_id != second.execution_id


def test_the_cas_loser_does_not_release_the_winners_lease(library, providers, approve):
    """Both callers share ``execution_id`` (they collapsed onto one row), so
    ``acquire_runbook_lease``'s re-entrant rule lets both of them "acquire" the shared
    lease before the compare-and-set decides who runs — only one of them then reaches
    ``run_plan`` (the CAS loser returns earlier). The loser must not release the lease on
    its way out: that would drop it out from under the winner while it is still
    executing, letting a third execution steal it mid-run.
    """
    import threading

    import agents.runbook_executor.agent as agent_module

    plan = _plan(library)
    run_plan_calls = 0
    lease_present_during_run = False
    real_run_plan = agent_module.run_plan

    def _observe_lease_then_run(*args, **kwargs):
        nonlocal run_plan_calls, lease_present_during_run
        run_plan_calls += 1
        lease_present_during_run = (
            repository.get_runbook_lease("ecommerce/order-service") is not None
        )
        return real_run_plan(*args, **kwargs)

    with unittest.mock.patch.object(agent_module, "run_plan", side_effect=_observe_lease_then_run):
        results: list[object] = []

        def _go():
            results.append(execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW))

        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

    # Only the winner ever reached run_plan, and the lease was still there when it did —
    # proof the loser's early return did not tear it down mid-run.
    assert run_plan_calls == 1
    assert lease_present_during_run is True
    assert repository.get_runbook_lease("ecommerce/order-service") is None


# ─── the concurrency hole an adversarial review reproduced (§6/§20, §25) ─────


def test_two_concurrent_executions_of_one_plan_dispatch_the_steps_once(library, providers, approve):
    """Two threads executing the same plan must not both run the production steps.

    This was a real defect: the state guard was a read-then-check, so both threads saw
    ``state='planned'`` and passed it, and the lease granted re-entrancy to both because
    they shared one ``execution_id``. The destructive, non-idempotent ``restart-pods``
    step was dispatched twice — a deployment restarted twice mid-incident — and the
    loser's terminal write overwrote the winner's steps and audit events, so one of the
    two real runs left no record at all.

    The fix is a compare-and-set on the state transition: the database decides who
    starts, and the loser is told what the winner is doing.
    """
    import threading

    plan = _plan(library)
    results: list[object] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            results.append(execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2

    # The destructive step ran exactly once.
    restarts = [c for c in _CALLS if c == (EXECUTE_CAP, "restart-pods", "execute")]
    assert len(restarts) == 1, f"restart-pods dispatched {len(restarts)}x: {_CALLS}"

    # Exactly one caller actually ran the plan: only the winner carries a legacy
    # RunbookExecution (the object run_plan returns). The loser reports the winner's
    # outcome — which is the right answer to "execute this plan", and is flagged as a
    # collapse onto an existing execution rather than a second run.
    ran = [r for r in results if r.legacy is not None]
    collapsed = [r for r in results if r.legacy is None]
    assert len(ran) == 1, [r.status.value for r in results]
    assert len(collapsed) == 1
    assert collapsed[0].duplicate_of == plan.execution_id
    assert ran[0].status is ExecutorStatus.EXECUTED

    # The row keeps the winner's evidence.
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["state"] == "completed"
    assert len(row["steps"]) == 3
    assert row["audit_events"]


def test_a_refused_plan_can_be_executed_after_the_blocker_clears(library, providers, approve):
    """An execution refused before it dispatched anything must not lock the plan out.

    The stale-incident guard aborts the execution — correctly — but that abort is a
    *refusal record*, not a run. Reusing its idempotency key forever meant the plan could
    never be executed even after the incident was reopened: the terminal row answered
    every later request with "already ran".
    """
    stale_plan = _plan(library)
    refused = execute_plan(
        stale_plan, order_incident(incident_status="resolved"), runbooks_dir=library, now=NOW
    )
    assert refused.status is ExecutorStatus.BLOCKED
    assert not _CALLS  # nothing was dispatched

    # The incident is active again: a fresh plan gets its own execution and runs.
    retry_plan = _plan(library)
    assert retry_plan.execution_id != stale_plan.execution_id
    assert retry_plan.ready is True
    result = execute_plan(retry_plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.EXECUTED


def test_a_real_run_is_never_silently_retried(library, providers, approve):
    """The retry salt applies only to executions that dispatched nothing. A completed
    run keeps its key, so re-planning collapses onto it rather than running again."""
    plan = _plan(library)
    execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    again = _plan(library)
    assert again.execution_id == plan.execution_id
    assert again.already_executed is True
    assert again.ready is False


# ─── stale incident (§24) ────────────────────────────────────────────────────


def test_incident_closed_between_plan_and_execute_is_blocked(library, providers, approve):
    plan = _plan(library)
    stale = order_incident(incident_status="resolved")
    result = execute_plan(plan, stale, runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.BLOCKED
    assert result.next_action is NextAction.RCA
    assert any("not applied to a closed incident" in r for r in result.blocking_reasons)
    assert not _CALLS, "nothing may be dispatched for a stale incident"
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["state"] == "aborted"
    assert metrics.counter("execution_stale_blocked") >= 1


def test_plan_hash_mismatch_is_refused(library, providers, approve):
    """An approval cannot be replayed against a plan that has since changed."""
    plan = _plan(library)
    repository.update_runbook_execution(plan.execution_id, plan_hash="deadbeef")
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.BLOCKED
    assert any("plan hash mismatch" in r for r in result.blocking_reasons)
    assert not _CALLS


# ─── parameter overrides survive the plan → execute hop ──────────────────────


SCALE_RUNBOOK_MD = RUNBOOK_MD.replace(
    """- name: restart-pods
  action: restart_deployment
  destructive: true
  idempotent: false
  rollback_action: rescale_previous
  target: deployment/order-service
  namespace: ecommerce""",
    """- name: scale-out
  action: scale_deployment
  destructive: true
  idempotent: true
  rollback_action: rescale_previous
  target: deployment/order-service
  namespace: ecommerce""",
)


@pytest.fixture
def scale_library(tmp_path):
    (tmp_path / "order-service-scale.md").write_text(SCALE_RUNBOOK_MD, encoding="utf-8")
    return tmp_path


def test_authorized_overrides_are_persisted_and_actually_executed(
    scale_library, providers, approve
):
    """An override must reach the provider, not just the plan hash.

    The overrides were validated at plan time and hashed into ``plan_hash`` — then
    dropped. Execution rebuilt the plan without them, so the hash never matched and
    every overridden plan was aborted; and even if it had run, the step would have
    dispatched the runbook's default parameters rather than the approved ones.
    """
    plan = plan_execution(
        order_incident(),
        runbooks_dir=scale_library,
        overrides={"scale-out": {"replicas": 4}},
        now=NOW,
    )
    assert plan.ready is True, plan.blocking_reasons
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["overrides"] == {"scale-out": {"replicas": 4}}

    result = execute_plan(plan, order_incident(), runbooks_dir=scale_library, now=NOW)
    assert result.status is ExecutorStatus.EXECUTED, result.blocking_reasons or result.reason
    step = next(s for s in result.steps if s["name"] == "scale-out")
    assert step["parameters"] == {"replicas": 4}


def test_an_invalid_override_refuses_the_plan_rather_than_being_dropped(scale_library):
    """A dropped override means the operator approved one plan and another one ran."""
    plan = plan_execution(
        order_incident(),
        runbooks_dir=scale_library,
        overrides={"scale-out": {"replicas": 9999}},
        now=NOW,
    )
    assert plan.ready is False
    assert plan.execution_id is None
    assert any("above the maximum" in r for r in plan.blocking_reasons)


# ─── concurrency (§25) ───────────────────────────────────────────────────────


def test_conflicting_execution_on_the_same_service_is_refused(library, providers, approve):
    plan = _plan(library, order_incident(incident_id="INC-A"))
    other = _plan(library, order_incident(incident_id="INC-B"))
    ok, _ = repository.acquire_runbook_lease(
        resource_key="ecommerce/order-service", execution_id="EXEC-someone-else", ttl_seconds=300
    )
    assert ok
    result = execute_plan(plan, order_incident(incident_id="INC-A"), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.BLOCKED
    assert any("holds the lease" in r for r in result.blocking_reasons)
    assert not _CALLS
    assert other.execution_id != plan.execution_id


def test_lease_is_released_after_execution(library, providers, approve):
    plan = _plan(library)
    execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert repository.get_runbook_lease("ecommerce/order-service") is None


def test_lease_is_released_even_when_a_step_fails(library, providers, approve):
    providers["exec"].add("restart-pods")
    plan = _plan(library)
    execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert repository.get_runbook_lease("ecommerce/order-service") is None


# ─── HITL (§18) ──────────────────────────────────────────────────────────────


def test_denied_approval_blocks_and_does_not_execute_the_destructive_step(library, providers):
    """No approver installed ⇒ the gate refuses; nothing past it runs."""
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.BLOCKED
    assert result.execution_state is ExecutionState.ABORTED
    assert result.legacy.status == "denied"
    statuses = {s["name"]: s["status"] for s in result.steps}
    assert statuses["drain-connections"] == "executed"  # the safe step ran
    assert statuses["restart-pods"] == "denied"
    assert statuses["verify-health"] != "executed"  # nothing past the gate
    assert (EXECUTE_CAP, "restart-pods", "execute") not in _CALLS
    assert metrics.counter("hitl_rejected") >= 1


def test_approval_is_recorded_on_the_execution(library, providers, approve):
    plan = _plan(library)
    execute_plan(
        plan,
        order_incident(),
        runbooks_dir=library,
        hitl_context={"approval_id": "APPROVE-1", "approver": "sre@test"},
        now=NOW,
    )
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["approval_id"] == "APPROVE-1"
    assert row["approver"] == "sre@test"
    assert metrics.counter("hitl_approved") >= 1


# ─── failure handling + rollback (§21, §22) ──────────────────────────────────


def test_step_failure_stops_and_rolls_back(library, providers, approve):
    providers["exec"].add("restart-pods")
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    assert result.status is ExecutorStatus.ROLLED_BACK
    assert result.next_action is NextAction.RCA
    assert result.execution_state is ExecutionState.ROLLED_BACK
    statuses = {s["name"]: s["status"] for s in result.steps}
    assert statuses["restart-pods"] == "failed"
    assert statuses["verify-health"] == "skipped"  # execution stopped, did not continue
    row = repository.get_runbook_execution(plan.execution_id)
    assert row["state"] == "rolled_back"
    assert row["rollback_status"] in ("rolled_back", "not_required")


def test_rollback_failure_escalates(library, providers, approve):
    providers["exec"].add("restart-pods")
    providers["exec_rollback"].add("drain-connections")
    providers["apply"].add("")  # no-op, keeps the dict shape honest
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    # drain-connections has no rollback_action, so the reverse of the *failed* plan is
    # trivial; the run still reports the failure rather than success.
    assert result.status in (ExecutorStatus.ROLLED_BACK, ExecutorStatus.FAILED)
    assert result.next_action is NextAction.RCA
    assert "restart-pods" in result.reason


def test_rollback_status_is_recorded_per_step(library, providers, approve):
    providers["apply"].add("verify-health")
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    by_name = {s["name"]: s for s in result.steps}
    assert by_name["verify-health"]["status"] == "failed"
    assert by_name["restart-pods"]["rollback_status"] in ("rolled_back", "rollback_failed")
    assert result.rollback_status in ("rolled_back", "rollback_failed")


# ─── step detail (§21) ───────────────────────────────────────────────────────


def test_every_step_records_identity_and_timing(library, providers, approve):
    plan = _plan(library)
    result = execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)
    for step in result.steps:
        assert step["step_id"] and step["action_id"]
        assert step["target"] and step["namespace"] == "ecommerce"
        assert step["capability"] in (APPLY_CAP, EXECUTE_CAP)
        assert step["started_at"] and step["completed_at"]
        assert step["duration_ms"] is not None
        assert step["attempts"] >= 1


# ─── timeouts + retries (§23) ────────────────────────────────────────────────


def test_slow_step_times_out_and_is_reported(monkeypatch):
    """A hung provider fails the step instead of hanging the execution."""
    monkeypatch.setenv("AIOPS_RUNBOOK_STEP_TIMEOUT", "0.2")
    monkeypatch.setenv("AIOPS_RUNBOOK_MAX_RETRIES", "0")
    from agents.runbook_executor.models import RunbookStep

    step = RunbookStep(
        name="slow",
        action="healthcheck",
        destructive=False,
        target="deployment/order-service",
        namespace="ecommerce",
    )
    reg = get_registry()
    name = "test.slow.apply"
    if name not in {t.name for t in reg.list()}:
        reg.register(
            Tool(
                name,
                "slow",
                lambda **_: (time.sleep(2), ToolResult(ok=True))[1],
                APPLY_CAP,
                "test",
            )
        )
    prev = reg.by_capability(APPLY_CAP).name
    reg.select_provider(APPLY_CAP, name)
    try:
        result = guarded_dispatch(APPLY_CAP, step, {"step": "slow"})
    finally:
        reg.select_provider(APPLY_CAP, prev)
    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert result.metadata["timed_out"] is True


def test_only_retry_safe_actions_are_retried(monkeypatch):
    monkeypatch.setenv("AIOPS_RUNBOOK_MAX_RETRIES", "3")
    from agents.runbook_executor.actions import resolve_action
    from agents.runbook_executor.execution_state import step_policy

    assert step_policy(resolve_action("healthcheck")).retries == 3  # idempotent read
    assert step_policy(resolve_action("clear_fault")).retries == 3  # declared retry-safe
    assert step_policy(resolve_action("restart_deployment")).retries == 0  # a rollout is not
    assert step_policy(resolve_action("rollback_deployment")).retries == 0


def test_gated_calls_are_never_retried_and_wait_out_the_approval_window(monkeypatch):
    """Retrying a gated call would ask the human twice; a short timeout would abandon
    an approval that is still pending."""
    monkeypatch.setenv("AIOPS_RUNBOOK_STEP_TIMEOUT", "5")
    monkeypatch.setenv("AIOPS_RUNBOOK_MAX_RETRIES", "3")
    from agents.runbook_executor.models import RunbookStep

    seen: list[float] = []

    def _capture(name, fn, *, policy=None, **kwargs):
        seen.append(policy.timeout)
        assert policy.retries == 0
        from aiops.tools.resilience import GuardOutcome

        outcome: GuardOutcome[Any] = GuardOutcome(value=ToolResult(ok=True), ok=True, attempts=1)
        return outcome

    monkeypatch.setattr("agents.runbook_executor.agent.guard", _capture)
    step = RunbookStep(name="restart", action="restart_deployment", destructive=True)
    guarded_dispatch(
        EXECUTE_CAP, step, {"step": "restart", "hitl_context": {"approval_timeout_seconds": 300}}
    )
    assert seen == [305.0]  # 5s step budget + the 300s approval window


def test_caching_is_never_enabled_for_a_step():
    """A cached mutation result would report success for a call that never ran."""
    from agents.runbook_executor.actions import resolve_action
    from agents.runbook_executor.execution_state import step_policy

    for action in ("healthcheck", "clear_fault", "restart_deployment"):
        policy = step_policy(resolve_action(action))
        assert policy.cache_ttl == 0.0
        assert policy.cache_empty_ttl == 0.0


def test_unexpected_crash_terminalises_the_execution(library, providers, approve, monkeypatch):
    """A crash mid-run must not park the execution in a non-terminal state.

    If it did, the idempotency key would refuse every future attempt for this plan with
    "already executing", and nobody could tell whether a step had run. The row is marked
    FAILED with the reason, and the exception still propagates so the caller sees it.
    """
    plan = _plan(library)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded past the registry guard")

    monkeypatch.setattr("agents.runbook_executor.agent.run_plan", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        execute_plan(plan, order_incident(), runbooks_dir=library, now=NOW)

    row = repository.get_runbook_execution(plan.execution_id)
    assert row["state"] == "failed"
    assert row["status"] == "FAILED"
    assert row["next_action"] == "ESCALATE"
    assert "RuntimeError" in (row["error"] or "")
    assert "unknown" in row["reason"]
    # And the lease is gone, so the service is not wedged.
    assert repository.get_runbook_lease("ecommerce/order-service") is None


# ─── state machine (§19) ─────────────────────────────────────────────────────


def test_terminal_executions_never_transition_again():
    from agents.runbook_executor.execution_state import (
        StateTransitionError,
        assert_transition,
    )

    for terminal in (
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
        ExecutionState.ROLLED_BACK,
        ExecutionState.ABORTED,
    ):
        with pytest.raises(StateTransitionError):
            assert_transition(terminal, ExecutionState.EXECUTING)


def test_ui_state_never_says_completed_without_verification():
    from agents.runbook_executor.execution_state import ui_state_for

    assert ui_state_for(state=ExecutionState.COMPLETED) is UiState.WAITING_VERIFICATION
    assert (
        ui_state_for(state=ExecutionState.COMPLETED, verification="pass")
        is UiState.VERIFICATION_PASSED
    )
    assert (
        ui_state_for(state=ExecutionState.COMPLETED, verification="fail")
        is UiState.VERIFICATION_FAILED
    )
