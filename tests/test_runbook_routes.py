"""The Runbook Executor HTTP surface (§33/§34).

Mounts the router on a minimal FastAPI app — not the full demo server — so the surface
is tested without the server's lifespan, auto-triage loop or ServiceNow watcher, the
same way ``tests/test_knowledge_routes.py`` does it.

What matters here is the *contract*: the frontend must be able to render candidates,
applicability, a dry run, an approval gate and an execution state without inferring
anything, and the API must refuse the things §7 says a human cannot override.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aiops.tools.mock_providers  # noqa: F401 - registers the automation.runbook.* mocks
from agents.runbook_executor import metrics
from agents.runbook_executor.execution_state import UiState
from aiops.policy import ApproverResult, get_gate
from aiops.tools import ToolResult, get_registry
from aiops.tools.registry import Tool
from demo.ui.runbook_routes import router
from tests.test_runbook_execution_state import RUNBOOK_MD

INCIDENT = {
    "incident_id": "INC-1042",
    "service": "order-service",
    "severity": "Sev-2",
    "alert_name": "EcommerceOrderErrorRateHigh",
    "summary": "Error rate 20% on POST /orders — HTTP 500s",
    "tags": ["error", "5xx"],
    "environment": "production",
    "probe_alert": False,
}

_STUB_FAULT_CLEAR = "test.routes.fault.clear"

# What ``ui_state`` may be once an execution has completed. Never a bare "COMPLETED":
# the executor does not decide recovery, so the UI is either waiting for the Resolution
# Verifier or reporting its verdict (§26/§34). Which one depends on whether the
# fire-and-forget verification has landed yet, which is a race by design.
POST_EXECUTION_UI_STATES = {
    "WAITING_VERIFICATION",
    "VERIFICATION_PASSED",
    "VERIFICATION_FAILED",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A minimal app with the runbook router, a one-runbook library and a stub seam."""
    (tmp_path / "order-service-controlled.md").write_text(RUNBOOK_MD, encoding="utf-8")
    monkeypatch.setenv("AIOPS_RUNBOOK_EXECUTOR_DIR", str(tmp_path))
    monkeypatch.setenv("AIOPS_ENVIRONMENT", "production")

    reg = get_registry()
    if _STUB_FAULT_CLEAR not in {t.name for t in reg.list()}:
        reg.register(
            Tool(
                _STUB_FAULT_CLEAR,
                "stub",
                lambda fault="", target="off": ToolResult(ok=True, data={"fault": fault}),
                "automation.fault.clear",
                "test",
            )
        )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def approve():
    get_gate().set_approver(lambda action, ctx: ApproverResult(approver="sre@test"))
    try:
        yield
    finally:
        get_gate().reset_approver()


# ─── candidates (§33) ────────────────────────────────────────────────────────


def test_candidates_returns_everything_the_ui_renders(client):
    res = client.post("/api/runbook-executor/candidates", json=INCIDENT)
    assert res.status_code == 200
    body = res.json()
    assert body["decision"] == "AUTO_SELECT"
    assert body["ui_state"] == "RUNBOOKS_FOUND"
    candidate = body["candidates"][0]
    for field in (
        "runbook_id",
        "version",
        "title",
        "match_score",
        "match_reasons",
        "applicability_status",
        "risk_level",
        "rollback_available",
        "hitl_required",
        "missing_prerequisites",
        "warnings",
        "status",
    ):
        assert field in candidate, field
    assert candidate["match_reasons"], "the UI must be able to explain the score"


def test_alert_name_is_translated_into_category_and_signals(client):
    """The deployment-specific translation happens at this boundary, not in the agent."""
    body = client.post("/api/runbook-executor/candidates", json=INCIDENT).json()
    incident = body["incident"]
    assert incident["failure_category"] == "application_error"
    assert "error_rate_high" in incident["observed_signals"]


def test_summary_keywords_seed_signals_without_an_alert_name(client):
    payload = {**INCIDENT, "alert_name": "", "summary": "pods are restarting, OOM killed"}
    incident = client.post("/api/runbook-executor/candidates", json=payload).json()["incident"]
    assert {"memory_saturation", "pod_restarting"} <= set(incident["observed_signals"])


def test_no_runbook_for_an_unknown_service(client):
    body = client.post(
        "/api/runbook-executor/candidates", json={**INCIDENT, "service": "telemetry-aggregator"}
    ).json()
    assert body["decision"] == "NO_RUNBOOK"
    assert body["candidates"] == []
    assert body["ui_state"] == "NO_RUNBOOK"


# ─── plan / dry run (§33) ────────────────────────────────────────────────────


def test_plan_returns_the_dry_run_and_reserves_an_execution(client):
    res = client.post(
        "/api/runbook-executor/plan",
        json={**INCIDENT, "runbook_id": "order-service-controlled", "selected_by": "sre@test"},
    )
    body = res.json()
    assert res.status_code == 200
    assert body["ui_state"] == UiState.DRY_RUN_READY.value
    assert body["execution_id"]
    dry = body["dry_run"]
    assert dry["status"] == "READY"
    assert dry["risk_level"] == "MEDIUM"
    assert dry["hitl_required"] is True
    assert dry["rollback_available"] is True
    assert [s["index"] for s in dry["steps"]] == [1, 2, 3]
    assert [s["mutation"] for s in dry["steps"]] == [True, True, False]
    assert dry["expected_impact"]


def test_plan_exposes_the_applicability_breakdown(client):
    body = client.post(
        "/api/runbook-executor/plan",
        json={**INCIDENT, "runbook_id": "order-service-controlled"},
    ).json()
    applicability = body["dry_run"]["applicability"]
    facets = {f["name"]: f["verdict"] for f in applicability["facets"]}
    assert facets["service"] == "match"
    assert facets["environment"] == "match"
    assert facets["failure_category"] == "match"
    assert facets["alert"] == "match"
    prereqs = {p["id"]: p["status"] for p in applicability["prerequisites"]}
    assert prereqs["incident_active"] == "satisfied"
    assert prereqs["target_in_scope"] == "satisfied"


def test_operator_cannot_plan_a_runbook_for_another_service(client):
    body = client.post(
        "/api/runbook-executor/plan",
        json={**INCIDENT, "runbook_id": "payment-service-restart"},
    ).json()
    assert body["ui_state"] == "BLOCKED"
    assert body["execution_id"] is None
    assert body["blocking_reasons"]


def test_operator_cannot_plan_around_a_closed_incident(client):
    body = client.post(
        "/api/runbook-executor/plan",
        json={
            **INCIDENT,
            "runbook_id": "order-service-controlled",
            "incident_status": "resolved",
        },
    ).json()
    assert body["ui_state"] in ("BLOCKED", "DRY_RUN_BLOCKED")
    assert body["execution_id"] is None
    assert any("closed incident" in r for r in body["blocking_reasons"])


# ─── execute (§18–§21) ───────────────────────────────────────────────────────


def test_execute_runs_and_reports_the_contract(client, approve):
    res = client.post(
        "/api/runbook-executor/execute",
        json={
            **INCIDENT,
            "runbook_id": "order-service-controlled",
            "selected_by": "sre@test",
            "approver": "sre@test",
            "synchronous": True,
        },
    )
    body = res.json()
    assert res.status_code == 200
    assert body["accepted"] is True
    result = body["result"]
    assert result["status"] == "EXECUTED"
    assert result["next_action"] == "VERIFY"
    assert result["ui_state"] in POST_EXECUTION_UI_STATES
    assert result["verification_handoff"]["runbook_id"] == "order-service-controlled"
    assert body["execution"]["state"] == "completed"
    assert body["execution"]["ui_state"] in POST_EXECUTION_UI_STATES


def test_execute_without_an_approver_is_blocked_not_executed(client):
    body = client.post(
        "/api/runbook-executor/execute",
        json={
            **INCIDENT,
            "runbook_id": "order-service-controlled",
            "synchronous": True,
        },
    ).json()
    result = body["result"]
    assert result["status"] == "BLOCKED"
    assert result["next_action"] == "RCA"
    statuses = {s["name"]: s["status"] for s in result["steps"]}
    assert statuses["restart-pods"] == "denied"


def test_execute_refuses_an_unselectable_runbook(client, approve):
    body = client.post(
        "/api/runbook-executor/execute",
        json={**INCIDENT, "runbook_id": "payment-service-restart", "synchronous": True},
    ).json()
    assert body["accepted"] is False
    assert body["blocking_reasons"]


def test_duplicate_execute_returns_the_first_execution(client, approve):
    payload = {
        **INCIDENT,
        "runbook_id": "order-service-controlled",
        "selected_by": "sre@test",
        "synchronous": True,
    }
    first = client.post("/api/runbook-executor/execute", json=payload).json()
    second = client.post("/api/runbook-executor/execute", json=payload).json()
    assert second["accepted"] is False
    assert second["duplicate"] is True
    assert second["execution"]["execution_id"] == first["result"]["execution_id"]


def _await_terminal(client, execution_id: str, timeout: float = 10.0) -> dict:
    """Poll an execution until it is terminal — what a real client does.

    Also what keeps this test hermetic: the gated path runs on a pool thread, and a
    thread still writing to the state DB after the test's tmp database is torn down
    surfaces as "no such table" inside an unrelated later test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/runbook-executor/executions/{execution_id}").json()
        if body.get("is_terminal"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"execution {execution_id} never reached a terminal state")


def test_gated_execute_returns_immediately_without_waiting(client):
    """A gated run must not block the request thread for the approval window."""
    body = client.post(
        "/api/runbook-executor/execute",
        json={**INCIDENT, "runbook_id": "order-service-controlled", "approval_timeout_seconds": 5},
    ).json()
    assert body["accepted"] is True
    assert body["hitl_required"] is True
    assert body["ui_state"] == "WAITING_APPROVAL"
    assert body["execution_id"]
    assert body["dry_run"]["status"] == "READY"

    # With no approver installed the gate refuses immediately, so the background run
    # lands on a terminal state rather than sitting out the approval window.
    final = _await_terminal(client, body["execution_id"])
    assert final["state"] == "aborted"
    assert final["status"] == "BLOCKED"


# ─── execution state (§34) ───────────────────────────────────────────────────


def test_execution_can_be_polled_by_id(client, approve):
    created = client.post(
        "/api/runbook-executor/execute",
        json={**INCIDENT, "runbook_id": "order-service-controlled", "synchronous": True},
    ).json()
    execution_id = created["result"]["execution_id"]
    res = client.get(f"/api/runbook-executor/executions/{execution_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["execution_id"] == execution_id
    assert body["state"] == "completed"
    assert body["ui_state"] in POST_EXECUTION_UI_STATES
    assert body["is_terminal"] is True
    assert body["steps"] and body["audit_events"]


def test_unknown_execution_is_404(client):
    assert client.get("/api/runbook-executor/executions/EXEC-nope").status_code == 404


def test_executions_can_be_listed_for_an_incident(client, approve):
    client.post(
        "/api/runbook-executor/execute",
        json={**INCIDENT, "runbook_id": "order-service-controlled", "synchronous": True},
    )
    body = client.get("/api/runbook-executor/executions", params={"incident_id": "INC-1042"}).json()
    assert body["count"] == 1
    assert body["executions"][0]["incident_id"] == "INC-1042"


def test_ui_state_reflects_verification_when_it_lands(client, approve):
    created = client.post(
        "/api/runbook-executor/execute",
        json={**INCIDENT, "runbook_id": "order-service-controlled", "synchronous": True},
    ).json()
    execution_id = created["result"]["execution_id"]
    res = client.post(
        f"/api/runbook-executor/executions/{execution_id}/verification",
        params={"verdict": "pass"},
    )
    assert res.status_code == 200
    assert res.json()["ui_state"] == "VERIFICATION_PASSED"
    assert metrics.counter("verification_pass") >= 1
    assert (
        client.post(
            f"/api/runbook-executor/executions/{execution_id}/verification",
            params={"verdict": "maybe"},
        ).status_code
        == 400
    )


def test_oversized_input_is_rejected(client):
    """There is no auth on this API, so unbounded fields are refused up front.

    Without the caps a multi-megabyte summary would be keyword-scanned, ranked, and then
    persisted verbatim into the execution row's candidate snapshot.
    """
    huge = client.post(
        "/api/runbook-executor/candidates", json={**INCIDENT, "summary": "x" * 10_000}
    )
    assert huge.status_code == 422
    many = client.post(
        "/api/runbook-executor/candidates",
        json={**INCIDENT, "tags": [f"tag{i}" for i in range(200)]},
    )
    assert many.status_code == 422
    # A long element inside an allowed-size list is truncated, not rejected: the list is
    # advisory input, and refusing the whole request over one long tag is not useful.
    ok = client.post("/api/runbook-executor/candidates", json={**INCIDENT, "tags": ["y" * 500]})
    assert ok.status_code == 200
    assert all(len(t) <= 64 for t in ok.json()["incident"]["tags"])


# ─── metrics (§31) ───────────────────────────────────────────────────────────


def test_metrics_endpoint_reports_counters_and_rates(client, approve):
    client.post("/api/runbook-executor/candidates", json=INCIDENT)
    body = client.get("/api/runbook-executor/metrics").json()
    assert body["counters"]["discovery_total"] >= 1
    assert "runbook_match_rate" in body["rates"]
    assert "no_runbook_rate" in body["rates"]


def test_rates_are_null_not_zero_before_anything_happens(client):
    """A rate over an empty denominator must not read as 0% success."""
    metrics.reset()
    body = client.get("/api/runbook-executor/metrics").json()
    assert body["rates"]["execution_success_rate"] is None
    assert body["rates"]["hitl_approval_rate"] is None
