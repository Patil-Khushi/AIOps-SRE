"""Tests for the HITL-gated KB publication tool (PRS-007, Checkpoint 5).

The security-critical property: ``knowledge.publish`` is REQUIRED-HITL, so a
draft cannot be published unless a human approves at the platform gate — the
agent cannot self-publish. Publishing also writes the suggested runbook.
"""

from __future__ import annotations

import pytest

# Side-effect import: registers the seam.knowledge.publish tool.
import aiops.tools.knowledge as kb_tool
from aiops import runbooks
from aiops import state as state_pkg
from aiops.policy import AutonomyLevel, get_gate
from aiops.state import repository as repo


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path / "runbooks"))
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    gate = get_gate()
    gate.reset_approver()  # fail-closed default
    yield
    gate.reset_approver()
    state_pkg.reset_engine_for_tests()


def _draft() -> int:
    return repo.save_kb_article(
        title="Cart 5xx postmortem",
        body="## Root cause\nflagd cartFailure on.",
        incident_id="INC-CART-1",
        service="cart",
        status="pending_review",
    )


def test_capability_is_required_hitl():
    assert get_gate().level_for("knowledge.publish") is AutonomyLevel.REQUIRED


def test_publish_blocked_without_approver():
    aid = _draft()
    out = kb_tool.request_publish(article_id=aid, hitl_context={})
    assert out["status"] == "blocked"
    # The draft must NOT have been published — the agent cannot self-publish.
    assert repo.get_kb_article(aid)["status"] == "pending_review"


def test_publish_succeeds_with_approver():
    aid = _draft()
    get_gate().set_approver(lambda action, ctx: "alice@example.com")
    out = kb_tool.request_publish(article_id=aid, hitl_context={})
    assert out["status"] == "published"
    assert repo.get_kb_article(aid)["status"] == "published"


def test_publish_denied_keeps_draft_pending():
    aid = _draft()
    # An approver that returns None == denial → gate blocks.
    get_gate().set_approver(lambda action, ctx: None)
    out = kb_tool.request_publish(article_id=aid, hitl_context={})
    assert out["status"] == "blocked"
    assert repo.get_kb_article(aid)["status"] == "pending_review"


def test_publish_writes_suggested_runbook():
    aid = _draft()
    get_gate().set_approver(lambda action, ctx: "alice@example.com")
    runbook = {
        "mode": "new",
        "target_id": "rb-cart-failure-incident",
        "title": "Cart failure (from INC-CART-1)",
        "body_markdown": "## Symptoms\nCart 5xx.\n## Resolution steps\n1. flip cartFailure off",
        "service": "cart",
    }
    out = kb_tool.request_publish(article_id=aid, runbook=runbook, hitl_context={})
    assert out["status"] == "published"
    assert out["runbook_written"] is True
    written = runbooks.get_runbook("rb-cart-failure-incident")
    assert written is not None
    assert written.status is runbooks.ReviewStatus.PUBLISHED
    assert written.related_kb == str(aid)


def test_publish_missing_article_is_error():
    get_gate().set_approver(lambda action, ctx: "alice@example.com")
    out = kb_tool.request_publish(article_id=99999, hitl_context={})
    assert out["status"] == "error"
