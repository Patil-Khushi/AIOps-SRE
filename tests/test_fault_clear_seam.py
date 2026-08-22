"""The ``automation.fault.clear`` seam, end to end.

Two independent entry paths converge on this one capability:

    RCA apply-fix    rca.fix_step.execute -> execute_rca_fix_step -> the seam
    Runbook executor automation.runbook.execute -> mock_runbook_execute -> the seam

Both are REQUIRED-HITL at their entry point, so everything below has already
been approved by a human. That is what makes an overstated result the worst
outcome available here, and it is what these tests guard.

The regression that motivated the file: ``mock_runbook_execute`` returned
``ok=True`` regardless of whether the seam fired, with the real outcome buried
in ``data["seam_ok"]`` that nothing read. An approved "clear this fault" step
reported ``executed`` and the run reported ``resolved`` while the fault was
still firing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

# Side-effect import: registers the mock automation.runbook.* providers.
import aiops.tools.mock_providers  # noqa: F401
from agents.runbook_executor import (
    ExecutableRunbook,
    Incident,
    RunbookStatus,
    RunbookStep,
    run_plan,
)
from aiops.policy import ApproverResult, get_gate
from aiops.tools import ToolResult, get_registry
from aiops.tools.registry import Tool

FAULT_CAP = "automation.fault.clear"
STUB_TOOL = "test.stub.automation.fault.clear"
FAULT_KEY = "order_service.http_500"


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def auto_approve() -> Iterator[None]:
    """Approve every gated action, so the destructive step actually runs."""
    get_gate().set_approver(lambda action, ctx: ApproverResult(approver="test-approver"))
    try:
        yield
    finally:
        get_gate().reset_approver()


@pytest.fixture
def clear_fault_stub() -> Iterator[dict[str, Any]]:
    """Register a recording stub for ``automation.fault.clear``.

    Mirrors the provider-swap idiom in tests/test_runbook_executor.py: register,
    ``select_provider``, restore the previous provider in ``finally``.

    Tests use this rather than letting the tool succeed against a missing
    provider. Hermeticity is the test's job; a production tool that reports
    success off-cluster is the bug this whole module exists to prevent.
    """
    recorder: dict[str, Any] = {"calls": [], "result": ToolResult(ok=True, data={"cleared": True})}

    def _stub(fault: str = "", target: str = "off", **_: object) -> ToolResult:
        recorder["calls"].append({"fault": fault, "target": target})
        return recorder["result"]

    reg = get_registry()
    try:
        previous = reg.by_capability(FAULT_CAP).name
    except KeyError:
        previous = None

    # Re-bind rather than register-once: the registry keys tools by name, so a
    # tool left over from an earlier test would still close over that test's
    # recorder and silently replay its result here.
    reg._tools.pop(STUB_TOOL, None)
    reg.register(Tool(STUB_TOOL, "stub fault clear", _stub, FAULT_CAP, "test"))
    reg.select_provider(FAULT_CAP, STUB_TOOL)
    try:
        yield recorder
    finally:
        reg._tools.pop(STUB_TOOL, None)
        if previous is not None:
            reg.select_provider(FAULT_CAP, previous)
        else:
            reg._active.pop(FAULT_CAP, None)


def _clear_fault_runbook(target: str = f"fault/{FAULT_KEY}") -> ExecutableRunbook:
    """A one-step runbook whose only step is a destructive fault clear."""
    return ExecutableRunbook(
        id="test-clear-fault",
        title="Clear an injected fault",
        service="order-service",
        severity="sev1",
        tags=["test"],
        status=RunbookStatus.ACTIVE,
        approved_by="test",
        steps=[
            RunbookStep(
                name="clear-injected-fault",
                action="clear_fault",
                target=target,
                namespace="ecommerce",
                destructive=True,
                idempotent=True,
            )
        ],
    )


def _run(target: str = f"fault/{FAULT_KEY}") -> Any:
    incident = Incident(service="order-service", severity="sev1", tags=["test"])
    return run_plan(incident, _clear_fault_runbook(target))


def _step_records(execution: Any) -> list[Any]:
    return list(getattr(execution, "steps", []) or [])


# ─── runbook path: the false-success regression ──────────────────────────────


def test_clear_fault_step_fails_when_no_provider_is_registered(monkeypatch, auto_approve):
    """A human approved a fault clear that nothing could perform. Not "executed".

    Before the fix this returned ok=True with ``seam_ok: False`` buried in the
    step data, so the run was marked resolved having changed nothing.
    """
    monkeypatch.delitem(get_registry()._active, FAULT_CAP, raising=False)

    execution = _run()

    assert execution.status != "resolved"
    steps = _step_records(execution)
    assert steps, "expected the clear_fault step to be recorded"
    assert steps[0].status != "executed"
    assert FAULT_CAP in (steps[0].error or "")


def test_clear_fault_step_fails_when_the_seam_reports_failure(clear_fault_stub, auto_approve):
    """Provider present, recovery refused — e.g. an unknown fault key.

    This arm is reachable in the running demo server, where the provider *is*
    registered, so it is the one that actually misled an operator.
    """
    clear_fault_stub["result"] = ToolResult(ok=False, error="unknown fault 'nope'")

    execution = _run()

    assert execution.status != "resolved"
    steps = _step_records(execution)
    assert steps[0].status != "executed"
    assert "unknown fault" in (steps[0].error or "")


def test_clear_fault_step_succeeds_and_reports_the_seam_provider(clear_fault_stub, auto_approve):
    """The happy path still works, and the target really is parsed into the call."""
    execution = _run()

    steps = _step_records(execution)
    assert steps[0].status == "executed"
    assert clear_fault_stub["calls"] == [{"fault": FAULT_KEY, "target": "off"}]


def test_legacy_flag_target_spelling_still_routes_to_the_seam(clear_fault_stub, auto_approve):
    """``flag/<key>`` predates the flagd retirement; still accepted deliberately."""
    _run(target=f"flag/{FAULT_KEY}")

    assert clear_fault_stub["calls"] == [{"fault": FAULT_KEY, "target": "off"}]


# ─── RCA path: previously zero coverage past the guard clause ────────────────


def test_rca_set_flag_path_calls_the_fault_clear_capability(clear_fault_stub):
    """``execute_rca_fix_step`` actually dispatches to the seam.

    Every case in tests/test_rca_remediation.py returns before this line: the
    only set_flag test passes ``flag=""`` and hits the guard clause. The one
    call in the RCA path that touches the cluster had no test at all.
    """
    from aiops.tools.rca_remediation import execute_rca_fix_step

    res = execute_rca_fix_step(action="set_flag", flag=FAULT_KEY, variant="off")

    assert res.ok is True
    assert clear_fault_stub["calls"] == [{"fault": FAULT_KEY, "target": "off"}]


def test_rca_set_flag_reports_a_readable_error_when_no_provider(monkeypatch):
    """Not ``KeyError: ...``.

    The registry now returns a structured missing-provider result, so the
    operator sees which capability is unwired rather than a laundered exception
    repr — after they have already approved the fix.
    """
    from aiops.tools.rca_remediation import execute_rca_fix_step

    monkeypatch.delitem(get_registry()._active, FAULT_CAP, raising=False)

    res = execute_rca_fix_step(action="set_flag", flag=FAULT_KEY, variant="off")

    assert res.ok is False
    assert "KeyError" not in (res.error or "")
    assert FAULT_CAP in (res.error or "")
    assert (res.metadata or {}).get("missing_provider") is True


def test_rca_set_flag_surfaces_a_seam_failure(clear_fault_stub):
    """An approved fix that failed is never reported as executed."""
    from aiops.tools.rca_remediation import execute_rca_fix_step

    clear_fault_stub["result"] = ToolResult(ok=False, error="kubectl exited 1")

    res = execute_rca_fix_step(action="set_flag", flag=FAULT_KEY, variant="off")

    assert res.ok is False
    assert "kubectl exited 1" in (res.error or "")


# ─── provider registration outside the demo server ───────────────────────────


def test_register_demo_providers_wires_the_capability(monkeypatch):
    """The bootstrap registers the provider, and is safe to call twice.

    ``demo/ui/fault_clear.py`` used to be imported by exactly one site — the
    FastAPI server — so the CLI runner and the test suite dispatched this
    capability into a registry that had never heard of it.
    """
    from demo.providers import register_demo_providers

    monkeypatch.delitem(get_registry()._active, FAULT_CAP, raising=False)

    register_demo_providers()
    register_demo_providers()  # idempotent: must not raise

    assert get_registry().by_capability(FAULT_CAP).provider == "ecommerce"


# ─── kubectl exit codes ──────────────────────────────────────────────────────


def test_failed_kubectl_makes_recover_report_not_ok(monkeypatch):
    """A non-zero kubectl exit must not read as a successful recovery.

    ``_k8s.run`` returned the exit code and every caller discarded it, so a
    failed ``kubectl set env`` raised nothing, the orchestrator marked the layer
    "ran", and the seam above reported a fault cleared that was still firing.
    """
    from demo.ecommerce.failure_injection import FAILURES, _k8s, recover

    monkeypatch.setattr(_k8s, "DRY_RUN", False)
    monkeypatch.setattr(_k8s.subprocess, "call", lambda *a, **k: 1)

    result = recover(FAILURES[FAULT_KEY], mode="application")

    assert result["ok"] is False
    assert result["layers"]["application"]["status"] == "error"
