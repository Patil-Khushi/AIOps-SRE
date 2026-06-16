"""Tests for HITL-gated ticket closure (PRS-007 Step 2, increment 2b).

The security-critical property: ``itsm.ticket.close`` is REQUIRED-HITL, so a
ticket cannot be closed unless a human approves the "verified resolved — close
ticket?" card. On PASS the verifier requests closure; on FAIL it notifies and
never proposes closure.
"""

from __future__ import annotations

import pytest

# Side-effect import: registers seam.itsm.ticket.close.
import aiops.tools.itsm_close as itsm_close
from agents.resolution_verifier.models import CheckResult, CheckStatus
from agents.resolution_verifier.verifier import Verifier, VerifyContext
from aiops.policy import AutonomyLevel, get_gate
from aiops.tools import get_registry
from aiops.tools.registry import Tool, ToolResult

# Module-level recorder so the (once-registered) fake tool always writes to the
# list the current test is inspecting — the fixture clears it per test.
_UPDATE_CALLS: list[dict] = []


def _fake_update(sys_id: str = "", fields: dict | None = None, **_):
    _UPDATE_CALLS.append({"sys_id": sys_id, "fields": fields or {}})
    return ToolResult(ok=True, data={"sys_id": sys_id, "state": (fields or {}).get("state")})


@pytest.fixture
def fake_update():
    """Register + activate a recording ``itsm.incident.update`` provider so the
    close tool's body is deterministic without a real/mock ServiceNow. Restores
    the previously-active provider afterward."""
    reg = get_registry()
    _UPDATE_CALLS.clear()
    name = "test.itsm.incident.update"
    if name not in reg._tools:  # type: ignore[attr-defined]
        reg.register(
            Tool(
                name=name,
                description="test",
                fn=_fake_update,
                capability="itsm.incident.update",
                provider="test",
            )
        )
    prev = reg._active.get("itsm.incident.update")  # type: ignore[attr-defined]
    reg.select_provider("itsm.incident.update", name)
    yield {"calls": _UPDATE_CALLS}
    if prev is not None:
        reg._active["itsm.incident.update"] = prev  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_gate():
    get_gate().reset_approver()
    yield
    get_gate().reset_approver()


# ─── the close gate ──────────────────────────────────────────────────────────


def test_capability_is_required_hitl():
    assert get_gate().level_for("itsm.ticket.close") is AutonomyLevel.REQUIRED


def test_close_blocked_without_approver(fake_update):
    out = itsm_close.request_ticket_close(incident_id="INC1", sys_id="sys-1", hitl_context={})
    assert out["status"] == "blocked"
    assert fake_update["calls"] == []  # ticket NOT touched — cannot self-close


def test_close_succeeds_with_approver(fake_update):
    get_gate().set_approver(lambda action, ctx: "alice@example.com")
    out = itsm_close.request_ticket_close(
        incident_id="INC1", sys_id="sys-1", close_code="Solved (Permanently)", hitl_context={}
    )
    assert out["status"] == "closed"
    # Two-step Resolve → Close: state 6 (with resolution code + proof notes),
    # then state 7 (Closed).
    assert len(fake_update["calls"]) == 2
    assert fake_update["calls"][0]["fields"]["state"] == "6"  # Resolved
    assert fake_update["calls"][1]["fields"]["state"] == "7"  # Closed
    assert "close_code" in fake_update["calls"][0]["fields"]
    assert "close_notes" in fake_update["calls"][0]["fields"]


# ─── verifier integration ────────────────────────────────────────────────────


def _verifier(tmp_path, *, close_fn, notify_fn, results):
    return Verifier(
        itsm_call=lambda cap, **kw: (
            ToolResult(ok=True, data={"incident": {"sys_id": "sys-X"}})
            if cap == "itsm.incident.get"
            else ToolResult(ok=True, data={})
        ),
        checks=lambda ctx, mc: list(results),
        windows=[0.0],
        sleep_fn=lambda _s: None,
        state_file=tmp_path / "v.json",
        close_fn=close_fn,
        notify_fn=notify_fn,
    )


def test_verifier_pass_requests_closure(tmp_path):
    closed: list = []
    notified: list = []
    v = _verifier(
        tmp_path,
        close_fn=lambda ctx, sys_id, report: (
            closed.append((ctx.incident_id, sys_id)) or {"status": "closed"}
        ),
        notify_fn=lambda ctx, report: notified.append(ctx.incident_id),
        results=[CheckResult(name="m", status=CheckStatus.PASS, detail="ok")],
    )
    v.run(VerifyContext(incident_id="INC-PASS", service="payment"))
    assert closed == [("INC-PASS", "sys-X")]  # closure requested with resolved sys_id
    assert notified == []  # no failure notification


def test_verifier_fail_notifies_and_never_closes(tmp_path):
    closed: list = []
    notified: list = []
    v = _verifier(
        tmp_path,
        close_fn=lambda ctx, sys_id, report: closed.append(ctx.incident_id) or {"status": "closed"},
        notify_fn=lambda ctx, report: notified.append(ctx.incident_id),
        results=[CheckResult(name="m", status=CheckStatus.FAIL, detail="still bad", after="9")],
    )
    v.run(VerifyContext(incident_id="INC-FAIL", service="payment"))
    assert closed == []  # closure NEVER proposed on a failed verification
    assert notified == ["INC-FAIL"]
