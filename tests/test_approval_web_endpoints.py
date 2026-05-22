"""HTTP surface for the HITL approval flow (HITL-1, issue #77).

Spins up the FastAPI demo server via TestClient and exercises:

* GET  /api/approvals               — list pending
* GET  /api/approvals/{id}          — fetch one
* POST /api/approvals/{id}/approve  — web approve
* POST /api/approvals/{id}/deny     — web deny
* POST /api/approvals/slack/callback — signed Slack interactivity callback

The Slack callback test exercises both the success path and the signature-
rejection path.  The server's request body is built from the Slack v0 spec.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from aiops.policy import (
    ApprovalStatus,
    get_approval_registry,
    get_gate,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AIOPS_SLACK_SIGNING_SECRET", "test-secret")
    # HITL-2 (#102): default this fixture to "unset" so the existing tests
    # exercising the unauthenticated demo path keep passing.  The auth-matrix
    # tests below override per-test with monkeypatch.setenv.
    monkeypatch.delenv("AIOPS_HITL_APPROVAL_TOKEN", raising=False)

    # Re-running the FastAPI startup hook accumulates state on three
    # process-wide singletons: the gate's approver, the chatops adapters,
    # and the chatops WebSocket hub's history ring.  Snapshot all three
    # so this test never pollutes ``tests/test_chatops_ws.py`` (which
    # also relies on the chatops singleton).
    import demo.ui.server as srv
    from aiops.tools.chatops import client as chat_client_mod
    from demo.ui import chatops_ws as ws_mod

    original_approver = get_gate().approver
    saved_adapters = list(chat_client_mod._CLIENT._adapters)
    saved_history = list(ws_mod._HUB._history)
    get_approval_registry()._reset_for_tests()
    try:
        with TestClient(srv.app) as c:
            yield c
    finally:
        get_gate().set_approver(original_approver)
        get_approval_registry()._reset_for_tests()
        chat_client_mod._CLIENT._adapters[:] = saved_adapters
        ws_mod._HUB._history.clear()
        ws_mod._HUB._history.extend(saved_history)


# ─── web list / get / approve / deny ──────────────────────────────────────


def test_list_pending_is_empty_initially(client: TestClient):
    res = client.get("/api/approvals")
    assert res.status_code == 200
    assert res.json() == {"count": 0, "approvals": []}


def test_approve_endpoint_resolves_pending_request(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {"target": "deployment/foo"})

    res = client.post(
        f"/api/approvals/{req.id}/approve",
        json={"approver": "chinmay", "reason": "verified in pre-prod"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["approver"] == "chinmay"
    assert reg.get(req.id).status is ApprovalStatus.APPROVED


def test_deny_endpoint_resolves_pending_request(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})

    res = client.post(
        f"/api/approvals/{req.id}/deny",
        json={"approver": "chinmay", "reason": "blast radius too large"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "denied"
    assert reg.get(req.id).status is ApprovalStatus.DENIED


def test_approve_unknown_id_returns_404(client: TestClient):
    res = client.post("/api/approvals/missing/approve", json={"approver": "x"})
    assert res.status_code == 404


def test_double_decide_returns_409(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    reg.decide(req.id, approved=True, approver="alice")
    res = client.post(f"/api/approvals/{req.id}/deny", json={"approver": "bob"})
    assert res.status_code == 409


def test_get_endpoint_returns_request_record(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {"x": 1})
    res = client.get(f"/api/approvals/{req.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == req.id
    assert body["context"] == {"x": 1}
    assert body["status"] == "pending"


# ─── Slack interactivity callback ─────────────────────────────────────────


def _sign(body: bytes, ts: str, secret: str = "test-secret") -> str:
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _build_slack_payload(approval_id: str, verdict: str, username: str = "alice") -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": username},
        "actions": [
            {
                "action_id": f"hitl_{verdict}",
                "value": f"{approval_id}|{verdict}",
            }
        ],
    }
    return urlencode({"payload": json.dumps(payload)}).encode("utf-8")


def test_slack_callback_resolves_approval_on_approve(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})

    body = _build_slack_payload(req.id, "approve", username="oncall")
    ts = str(int(time.time()))
    res = client.post(
        "/api/approvals/slack/callback",
        content=body,
        headers={
            "x-slack-signature": _sign(body, ts),
            "x-slack-request-timestamp": ts,
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 200, res.text
    assert reg.get(req.id).status is ApprovalStatus.APPROVED
    assert reg.get(req.id).approver == "slack:oncall"


def test_slack_callback_resolves_approval_on_deny(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})

    body = _build_slack_payload(req.id, "deny", username="sre")
    ts = str(int(time.time()))
    res = client.post(
        "/api/approvals/slack/callback",
        content=body,
        headers={
            "x-slack-signature": _sign(body, ts),
            "x-slack-request-timestamp": ts,
        },
    )
    assert res.status_code == 200
    assert reg.get(req.id).status is ApprovalStatus.DENIED


def test_slack_callback_rejects_bad_signature(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    body = _build_slack_payload(req.id, "approve")
    ts = str(int(time.time()))
    res = client.post(
        "/api/approvals/slack/callback",
        content=body,
        headers={
            "x-slack-signature": "v0=deadbeef",
            "x-slack-request-timestamp": ts,
        },
    )
    assert res.status_code == 401
    assert reg.get(req.id).status is ApprovalStatus.PENDING


def test_slack_callback_rejects_stale_timestamp(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    body = _build_slack_payload(req.id, "approve")
    stale_ts = str(int(time.time()) - 60 * 10)
    res = client.post(
        "/api/approvals/slack/callback",
        content=body,
        headers={
            "x-slack-signature": _sign(body, stale_ts),
            "x-slack-request-timestamp": stale_ts,
        },
    )
    assert res.status_code == 401


# ─── HITL-2 (#102) bearer-token auth on web approve/deny ─────────────────


def test_approve_accepts_without_header_when_token_unset(client: TestClient):
    """With AIOPS_HITL_APPROVAL_TOKEN unset the server is in demo mode and
    accepts every approve/deny.  The startup log line carries the warning."""
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(f"/api/approvals/{req.id}/approve", json={"approver": "alice"})
    assert res.status_code == 200
    assert reg.get(req.id).status is ApprovalStatus.APPROVED


def test_deny_accepts_without_header_when_token_unset(client: TestClient):
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(f"/api/approvals/{req.id}/deny", json={"approver": "alice"})
    assert res.status_code == 200
    assert reg.get(req.id).status is ApprovalStatus.DENIED


def test_approve_accepts_with_correct_bearer_when_token_set(client: TestClient, monkeypatch):
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(
        f"/api/approvals/{req.id}/approve",
        json={"approver": "alice"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert res.status_code == 200
    assert reg.get(req.id).status is ApprovalStatus.APPROVED


def test_deny_accepts_with_correct_bearer_when_token_set(client: TestClient, monkeypatch):
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(
        f"/api/approvals/{req.id}/deny",
        json={"approver": "alice"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert res.status_code == 200
    assert reg.get(req.id).status is ApprovalStatus.DENIED


def test_approve_rejects_missing_authorization_header_when_token_set(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(f"/api/approvals/{req.id}/approve", json={"approver": "x"})
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid approval token"
    assert reg.get(req.id).status is ApprovalStatus.PENDING


def test_deny_rejects_missing_authorization_header_when_token_set(client: TestClient, monkeypatch):
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(f"/api/approvals/{req.id}/deny", json={"approver": "x"})
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid approval token"
    assert reg.get(req.id).status is ApprovalStatus.PENDING


def test_approve_rejects_wrong_bearer_token_when_token_set(client: TestClient, monkeypatch):
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(
        f"/api/approvals/{req.id}/approve",
        json={"approver": "x"},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid approval token"
    assert reg.get(req.id).status is ApprovalStatus.PENDING


def test_approve_rejects_wrong_scheme_when_token_set(client: TestClient, monkeypatch):
    """Same 401 / same detail whether the header is missing, the scheme is
    wrong (Basic, Token, etc.), or the token value is wrong — no side
    channel for probers."""
    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    reg = get_approval_registry()
    req = reg.create("rca.fix_step.execute", {})
    res = client.post(
        f"/api/approvals/{req.id}/approve",
        json={"approver": "x"},
        headers={"Authorization": "Basic czNjcmV0"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid approval token"


def test_approval_token_uses_constant_time_compare():
    """``_require_approval_token`` must use ``hmac.compare_digest`` — the
    standard timing-safe comparator — rather than ``==``.  We import the
    module and verify the symbol is referenced (the failure mode this
    guards against is a future refactor swapping in a plain ``==``)."""
    import inspect

    from demo.ui import server as srv

    src = inspect.getsource(srv._require_approval_token)
    assert "hmac.compare_digest" in src, (
        "approve/deny token compare must be timing-safe via hmac.compare_digest"
    )


def test_startup_logs_warning_when_token_unset(monkeypatch, caplog):
    """Acceptance: 'When unset, server logs a single
    WARNING: HITL web endpoints are unauthenticated line at startup'."""
    import logging

    from demo.ui import server as srv

    monkeypatch.delenv("AIOPS_HITL_APPROVAL_TOKEN", raising=False)
    caplog.set_level(logging.WARNING, logger=srv.logger.name)
    srv._warn_if_approval_token_unset()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert messages.count("HITL web endpoints are unauthenticated") == 1


def test_startup_silent_when_token_set(monkeypatch, caplog):
    import logging

    from demo.ui import server as srv

    monkeypatch.setenv("AIOPS_HITL_APPROVAL_TOKEN", "s3cret")
    caplog.set_level(logging.WARNING, logger=srv.logger.name)
    srv._warn_if_approval_token_unset()
    assert "HITL web endpoints are unauthenticated" not in caplog.text


def test_slack_callback_rejects_malformed_action(client: TestClient):
    # Signature valid but action value lacks the "|verdict" suffix.
    body = urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": "U", "username": "x"},
                    "actions": [{"action_id": "hitl_approve", "value": "no-pipe"}],
                }
            )
        }
    ).encode("utf-8")
    ts = str(int(time.time()))
    res = client.post(
        "/api/approvals/slack/callback",
        content=body,
        headers={
            "x-slack-signature": _sign(body, ts),
            "x-slack-request-timestamp": ts,
        },
    )
    assert res.status_code == 400
