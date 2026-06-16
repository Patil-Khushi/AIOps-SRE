"""Scenario-locked smoke tests for the chained demo (PR #173 / option C).

This file is the *contract test* behind ``docs/chained_demo_walkthrough.md``.
It pins the response shape of ``/api/triage-full`` and ``/api/execute`` so
any future refactor that changes the chain — endpoint URL, response keys,
agent call order, soft-failure semantics — trips here and points back to
the walkthrough.

Three test scenarios:

1. ``test_triage_full_returns_locked_response_shape`` — asserts every
   key the walkthrough's "Step 2" PowerShell block references is
   present (verdict, classification, ticket, notifications, deliveries,
   rca, remediation, persisted, errors).

2. ``test_execute_refuses_option_without_requires_hitl`` — pins the
   catalog principle #3 invariant at the HTTP boundary: an option that
   doesn't declare ``requires_hitl=True`` is refused BEFORE the gate.

3. ``test_execute_with_yes_approver_returns_executed_and_persists`` —
   the v1 happy path through the HTTP surface: installed approver
   says yes, registered tool fires, EXECUTED status comes back,
   ExecutionRow lands in the DB.

Each test boots a fresh FastAPI ``TestClient`` so HITL state and tool
registry state are restored across tests (the registry-isolation
lesson from PR #170 lives in fixtures here too).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops import state as state_pkg
from aiops.policy import get_approval_registry, get_gate
from aiops.policy.gate import ApprovalSummary, ApproverResult
from aiops.state.repository import list_executions
from aiops.tools import get_registry
from aiops.tools.registry import Tool, ToolResult

# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def _state_db(monkeypatch, tmp_path):
    """Fresh file-based SQLite per test.

    We use a real file (not ``:memory:``) because the FastAPI app's
    lifespan re-runs ``init_db()`` inside the TestClient, and
    ``sqlite:///:memory:`` creates a new in-memory DB per connection —
    the request handler's ``save_execution`` would land in a different
    DB than the test's ``list_executions`` query. The tmp file is
    shared across connections, so both see the same tables + rows.
    """
    db_file = tmp_path / "state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db_file.as_posix()}")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


@pytest.fixture
def _isolate_gate():
    original = get_gate().approver
    get_approval_registry()._reset_for_tests()
    yield
    get_gate().set_approver(original)
    get_approval_registry()._reset_for_tests()


@pytest.fixture
def _isolate_registry():
    """Snapshot + restore the tool registry singleton (same pattern as PRS-002 tests)."""
    reg = get_registry()
    snap_active = dict(reg._active)  # type: ignore[attr-defined]
    snap_tools = dict(reg._tools)  # type: ignore[attr-defined]
    yield
    reg._active.clear()  # type: ignore[attr-defined]
    reg._active.update(snap_active)  # type: ignore[attr-defined]
    reg._tools.clear()  # type: ignore[attr-defined]
    reg._tools.update(snap_tools)  # type: ignore[attr-defined]


@pytest.fixture
def client(_state_db, _isolate_gate, _isolate_registry):
    """FastAPI TestClient against the demo server.

    Importing ``demo.ui.server`` is moderately expensive (all agents
    register their @tool providers on import) — the fixture caches by
    not re-importing, but state isolation comes from the upstream
    fixtures restoring gate / registry / DB between cases.
    """
    from demo.ui.server import app

    with TestClient(app) as c:
        yield c


def _install_yes_approver():
    """Approver that always grants approval to unlock the EXECUTED path."""

    def _yes(action: str, ctx: dict[str, Any]) -> ApproverResult:
        return ApproverResult(
            approver="demo-operator@example.com",
            summary=ApprovalSummary(
                id="appr-chained-demo",
                status="approved",
                approver="demo-operator@example.com",
                reason="chained-demo test",
            ),
        )

    get_gate().set_approver(_yes)


def _register_fake_tool(capability: str, captured: list[dict[str, Any]]):
    """Plug a synthetic ok=True tool into the registry for the given capability."""

    def _fn(flag: str, variant: str = "off") -> ToolResult:
        captured.append({"flag": flag, "variant": variant})
        return ToolResult(ok=True, data={"flag": flag, "variant": variant, "did": "flipped"})

    name = f"test.{capability.replace('.', '-')}"
    get_registry()._tools[name] = Tool(  # type: ignore[attr-defined]
        name=name,
        capability=capability,
        provider="test",
        description="chained-demo test stub",
        fn=_fn,
    )
    get_registry()._active[capability] = name  # type: ignore[attr-defined]


def _sample_alert() -> dict[str, Any]:
    """The alert shape the walkthrough uses in Step 2."""
    return {
        "alert_id": "DEMO-CHAIN-1",
        "service": "product-catalog",
        "metric": "product_catalog_latency_p95_ms",
        "value": 4500.0,
        "threshold": 1000.0,
        "timestamp": "2026-06-12T10:00:00Z",
        "source": "Prometheus",
        "summary": "Product catalog p95 latency 4500ms over 1000ms threshold",
    }


# ─── 1. Response-shape lock for /api/triage-full ──────────────────────────


def test_triage_full_returns_locked_response_shape(client):
    """The walkthrough's "Step 2" PowerShell block dereferences
    ``$chain.verdict``, ``.classification``, ``.ticket``,
    ``.notifications``, ``.deliveries``, ``.rca``, ``.remediation``,
    ``.persisted``, and ``.errors``. If any of those goes missing the
    walkthrough breaks — fail here and tell the maintainer to update
    the doc.

    Note: this test does NOT assert ``rca`` and ``remediation`` are
    *non-null*. RCA + the recommender can soft-fail when the LLM is
    unavailable (eval-harness path, no API key); the contract is that
    the keys are present even on soft-failure, and ``errors`` carries
    the diagnostic.
    """
    body = {
        "alert": _sample_alert(),
        "scenario_id": "slow-product-catalog",
        "environment": "production",
    }
    resp = client.post("/api/triage-full", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    LOCKED_KEYS = {
        "verdict",
        "classification",
        "ticket",
        "notifications",
        "deliveries",
        "rca",
        "remediation",
        "persisted",
        "errors",
    }
    missing = LOCKED_KEYS - set(data.keys())
    assert not missing, (
        f"/api/triage-full response is missing locked keys {missing}; the "
        "chained-demo walkthrough's Step 2 PowerShell block will break. "
        "Update docs/chained_demo_walkthrough.md alongside any contract change."
    )

    # Reactive half MUST always be non-null. Triage cannot soft-fail
    # without breaking the whole chain.
    assert data["verdict"] is not None, "RA-001 triage produced null verdict"
    assert data["verdict"].get("affected_service") == "product-catalog"
    assert data["classification"] is not None, "RA-002 classification produced null"
    assert data["ticket"] is not None, "RA-003 auto-ticketing produced null"

    # Errors dict is the soft-failure escape hatch — must be present even
    # when empty so consumers can branch on `if errors:` without KeyError.
    assert isinstance(data["errors"], dict), "errors must be a dict (possibly empty)"


# ─── 2. /api/execute boundary: refuses non-HITL options ───────────────────


def test_execute_refuses_option_without_requires_hitl(client):
    """Catalog principle #3 enforced at the HTTP boundary: an upstream
    that strips ``requires_hitl=True`` from an option cannot smuggle a
    non-gated execution through Auto-Healer.
    """
    body = {
        "option": {
            "option_id": "bad-no-hitl",
            "action_type": "set_flag",
            "blast_radius": "low",
            "tool_capability": "feature_flags.set_variant",
            "tool_args": {"flag": "x", "variant": "off"},
            "rollback": "flip back",
            # NOTE: no requires_hitl field — the agent must REFUSE.
        },
        "affected_service": "product-catalog",
        "dry_run": True,
    }
    resp = client.post("/api/execute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "refused"
    assert data["requires_hitl"] is True  # invariant — the verdict still
    # declares the action would be gated; refusal happened upstream of the gate
    assert data["would_execute"] is False
    assert "requires_hitl" in data["decision"]["reason"].lower()


# ─── 3. v1 happy path through HTTP ────────────────────────────────────────


def test_execute_with_yes_approver_returns_executed_and_persists(client):
    """End-to-end through ``/api/execute``: install a yes-approver + a
    registered tool, POST a valid option with ``dry_run=False``, and
    assert EXECUTED + tool_result populated + audit row landed.

    This is the test that fails if either:
      - the platform tool registry stops dispatching feature_flags.set_variant
      - the agent's verdict shape changes (tool_result envelope, status enum)
      - ExecutionRow persistence breaks
    """
    _install_yes_approver()
    captured: list[dict[str, Any]] = []
    _register_fake_tool("feature_flags.set_variant", captured)

    body = {
        "option": {
            "option_id": "rca-step-1",
            "action_type": "set_flag",
            "blast_radius": "low",
            "tool_capability": "feature_flags.set_variant",
            "tool_args": {"flag": "productCatalogFailure", "variant": "off"},
            "rollback": "Re-enable the flag.",
            "requires_hitl": True,
        },
        "incident_id": "INC-DEMO-1",
        "affected_service": "product-catalog",
        "operator": "demo-operator@example.com",
        "dry_run": False,
        "hitl_context": {"skip_approval": False},
    }
    resp = client.post("/api/execute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Status + dispatch.
    assert data["status"] == "executed"
    assert data["decision"]["allowed"] is True
    assert data["tool_result"] is not None
    assert data["tool_result"]["ok"] is True
    assert data["tool_result"]["data"]["flag"] == "productCatalogFailure"
    assert len(captured) == 1, "tool should have been called exactly once"

    # Audit row landed.
    rows = list_executions(affected_service="product-catalog", limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "executed"
    assert row["request_id"] == data["request_id"]
    assert row["option_id"] == "rca-step-1"
    assert row["tool_capability"] == "feature_flags.set_variant"
    assert row["tool_result"]["ok"] is True


# ─── 4. /api/remediation standalone — shape lock ──────────────────────────


def test_remediation_endpoint_returns_ranked_options(client):
    """The walkthrough doesn't call ``/api/remediation`` directly (the
    triage-full chain does it inline) but it's part of the public
    contract. Asserts the standalone endpoint produces a verdict with
    a non-empty options list and a recommended_option_id pointing at
    one of them.
    """
    body = {
        "rca_verdict": {
            "affected_service": "product-catalog",
            "root_cause": "Injected failure flag enabled on product-catalog deployment",
            "confidence_score": 0.85,
            "ranked_fix_steps": [
                {
                    "description": "Disable the productCatalogFailure flag",
                    "blast_radius": "low",
                    "rollback": "Re-enable the flag",
                    "requires_hitl": True,
                    "action_type": "set_flag",
                    "flag": "productCatalogFailure",
                    "variant": "off",
                }
            ],
            "audit_metadata": {
                "created_at": "2026-06-12T10:00:00Z",
                "created_by": "PRS-008",
            },
        },
        "environment": "production",
    }
    resp = client.post("/api/remediation", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["affected_service"] == "product-catalog"
    assert isinstance(data["options"], list) and len(data["options"]) >= 1
    assert data["requires_hitl"] is True
    assert data["auto_pick_eligible"] is False
    assert any(o["option_id"] == data["recommended_option_id"] for o in data["options"])
