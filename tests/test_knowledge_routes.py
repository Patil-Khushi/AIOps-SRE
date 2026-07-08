"""Tests for the Knowledge Synthesizer HTTP surface (PRS-007, Checkpoint 6).

Mounts the router on a minimal FastAPI app (not the full demo server) so the
surface is tested in isolation — fast, and free of the server's lifespan /
auto-triage machinery.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiops import state as state_pkg
from aiops.policy import get_gate
from demo.ui.knowledge_routes import router


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # File-based (not :memory:) so the table is visible across the endpoint's
    # background worker threads — a fresh :memory: DB is per-connection.
    db = tmp_path / "state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path / "runbooks"))
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    get_gate().reset_approver()
    # These tests exercise synthesis MECHANICS, not the ticket-closed gate, so
    # stub the gate to "closed". Gate-specific tests re-override this below.
    monkeypatch.setattr("demo.ui.knowledge_routes._ticket_is_closed", lambda incident_id: True)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c
    get_gate().reset_approver()
    state_pkg.reset_engine_for_tests()


def _bundle(incident_id: str = "INC-PCAT-1") -> dict:
    return {
        "incident_id": incident_id,
        "scenario_id": "slow-product-catalog",
        "resolved_at": "2026-06-11T10:10:00Z",
        "triage_verdict": {
            "affected_service": "productcatalogservice",
            "severity": "Sev-2",
            "alert_summary": "Product catalog p95 latency high",
            "audit_metadata": {"created_at": "2026-06-11T10:00:00Z"},
        },
        "rca_verdict": {
            "affected_service": "productcatalogservice",
            "root_cause": "flagd flag productCatalogFailure is on, injecting ~5s delay.",
            "ranked_fix_steps": [
                {
                    "description": "Set productCatalogFailure off.",
                    "blast_radius": "low",
                    "rollback": "flip back on",
                    "action_type": "set_flag",
                    "flag": "productCatalogFailure",
                }
            ],
            "confidence_score": 0.85,
            "audit_metadata": {"created_at": "2026-06-11T10:05:00Z"},
        },
    }


def test_synthesize_endpoint_returns_result(client):
    r = client.post("/api/synthesize", json=_bundle())
    assert r.status_code == 200
    body = r.json()
    assert body["affected_service"] == "productcatalogservice"
    assert body["status"] == "pending_review"
    assert body["dedup_action"] == "create"
    assert body["kb_article_id"] is not None


def test_kb_list_and_get(client):
    aid = client.post("/api/synthesize", json=_bundle()).json()["kb_article_id"]
    listed = client.get("/api/kb").json()
    assert listed["count"] == 1
    assert listed["articles"][0]["id"] == aid

    one = client.get(f"/api/kb/{aid}")
    assert one.status_code == 200
    assert one.json()["incident_id"] == "INC-PCAT-1"

    assert client.get("/api/kb/999999").status_code == 404


def test_kb_list_filters_by_status(client):
    client.post("/api/synthesize", json=_bundle())
    assert client.get("/api/kb", params={"status": "pending_review"}).json()["count"] == 1
    assert client.get("/api/kb", params={"status": "published"}).json()["count"] == 0


def test_publish_blocked_without_approver(client):
    aid = client.post("/api/synthesize", json=_bundle()).json()["kb_article_id"]
    approval_id = client.post(f"/api/kb/{aid}/publish", json={}).json()["approval_id"]
    outcome = _poll(client, approval_id)
    assert outcome["status"] == "blocked"
    # Still pending_review — the API cannot self-publish.
    assert client.get(f"/api/kb/{aid}").json()["status"] == "pending_review"


def test_publish_succeeds_with_approver(client):
    get_gate().set_approver(lambda action, ctx: "alice@example.com")
    aid = client.post("/api/synthesize", json=_bundle()).json()["kb_article_id"]
    approval_id = client.post(f"/api/kb/{aid}/publish", json={}).json()["approval_id"]
    outcome = _poll(client, approval_id)
    assert outcome["status"] == "published"
    assert client.get(f"/api/kb/{aid}").json()["status"] == "published"


def test_publish_missing_article_404(client):
    assert client.post("/api/kb/999999/publish", json={}).status_code == 404


# ─── ticket-closed guard (the incident must be Closed before synthesis) ────────


def test_synthesize_blocked_when_ticket_open(client, monkeypatch):
    """Open ticket → 409, and no draft is created (skips the close-ticket gate)."""
    monkeypatch.setattr("demo.ui.knowledge_routes._ticket_is_closed", lambda _id: False)
    r = client.post("/api/synthesize", json=_bundle())
    assert r.status_code == 409
    assert "not Resolved/Closed" in r.json()["detail"]
    assert client.get("/api/kb").json()["count"] == 0


def test_synthesize_blocked_when_ticket_unknown(client, monkeypatch):
    """Indeterminate (ServiceNow unreachable / ticket not found) → fail-closed."""
    monkeypatch.setattr("demo.ui.knowledge_routes._ticket_is_closed", lambda _id: None)
    assert client.post("/api/synthesize", json=_bundle()).status_code == 409
    assert client.get("/api/kb").json()["count"] == 0


def test_synthesize_bypass_allows_open_ticket(client, monkeypatch):
    """Explicit bypass (offline demo) drafts even for an open ticket."""
    monkeypatch.setattr("demo.ui.knowledge_routes._ticket_is_closed", lambda _id: False)
    r = client.post("/api/synthesize", json=_bundle(), params={"bypass_ticket_check": "true"})
    assert r.status_code == 200
    assert r.json()["kb_article_id"] is not None


def _poll(client, approval_id: str, tries: int = 60) -> dict:
    """Poll the publish outcome until it resolves (background worker)."""
    for _ in range(tries):
        out = client.get(f"/api/kb/publish/outcome/{approval_id}").json()
        if out.get("status") != "pending":
            return out
        time.sleep(0.05)
    return {"status": "timeout"}
