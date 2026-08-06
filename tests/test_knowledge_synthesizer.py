"""End-to-end tests for the Knowledge Synthesizer agent (PRS-007).

Runs under the hermetic stub LLM provider (conftest pins it) and with
embeddings disabled (conftest), so synthesis takes the deterministic fallback
and dedup uses the signature path — both fully reproducible.
"""

from __future__ import annotations

import copy

import pytest

from agents.knowledge_synthesizer.agent import run, synthesize
from aiops import state as state_pkg
from aiops.state import repository as repo


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # Fresh in-memory state DB + an isolated runbook library (seeded on demand).
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path / "runbooks"))
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _bundle(incident_id: str = "INC-MYSQL-1", **overrides):
    """A resolved incident bundle for an ecommerce scenario.

    Retargeted from the OTel Demo's slow-product-catalog when that app was
    removed. user_service_mysql_down is chosen deliberately: a seed runbook
    exists for it (rb-user-service-mysql-down), which is what lets
    test_runbook_suggestion_updates_existing_seed_runbook exercise the *update*
    path rather than the *new* path.
    """
    bundle = {
        "incident_id": incident_id,
        "scenario_id": "user_service_mysql_down",
        "resolved_at": "2026-08-03T10:10:00Z",
        "triage_verdict": {
            "affected_service": "user-service",
            "severity": "Sev-1",
            "alert_summary": "user-service cannot reach MySQL; all logins failing",
            "audit_metadata": {"created_at": "2026-08-03T10:00:00Z"},
        },
        "rca_verdict": {
            "affected_service": "user-service",
            "root_cause": (
                "The MySQL StatefulSet was scaled to zero, so user-service could not "
                "open a database connection and returned HTTP 500 on every login."
            ),
            "ranked_fix_steps": [
                {
                    "description": "Clear the user_service.mysql_down fault "
                    "(scale the MySQL StatefulSet back to 1).",
                    "blast_radius": "low",
                    "rollback": "Scale MySQL back to 0 — instant, PVC retained.",
                    "action_type": "set_flag",
                    "flag": "user_service.mysql_down",
                    "variant": "off",
                }
            ],
            "confidence_score": 0.85,
            "audit_metadata": {"created_at": "2026-08-03T10:05:00Z"},
        },
    }
    bundle.update(overrides)
    return copy.deepcopy(bundle)


# ─── happy path ──────────────────────────────────────────────────────────────


def test_synthesize_creates_pending_review_article():
    res = synthesize(_bundle())
    assert res.dedup_action == "create"
    assert res.status.value == "pending_review"
    assert res.affected_service == "user-service"
    assert "MySQL" in res.root_cause
    assert res.kb_article_id is not None
    # Persisted as a pending-review draft.
    stored = repo.get_kb_article(res.kb_article_id)
    assert stored["status"] == "pending_review"
    assert stored["incident_id"] == "INC-MYSQL-1"


def test_postmortem_has_reconstructed_timeline():
    res = synthesize(_bundle())
    events = [e.event for e in res.postmortem.timeline]
    # triage + rca + resolved, in chronological order.
    assert "Alert triaged" in events
    assert "Root cause identified" in events
    assert events[-1] == "Incident resolved"


def test_runbook_suggestion_updates_existing_seed_runbook():
    res = synthesize(_bundle())
    assert res.runbook_mode == "update"
    # Matched by SERVICE, and user-service has more than one seed runbook
    # (mysql-down and crashloop), so which of them wins is not part of the
    # contract — asserting a specific id here would encode iteration order.
    # What matters is that an existing seed was found rather than a new
    # runbook invented.
    assert res.related_runbook_id in {
        "rb-user-service-mysql-down",
        "rb-user-service-crashloop",
    }
    assert "## Resolution steps" in res.runbook_suggestion.body_markdown


def test_runbook_suggestion_new_when_no_runbook_for_service():
    bundle = _bundle(incident_id="INC-FE-1")
    bundle["triage_verdict"]["affected_service"] = "frontend"
    bundle["rca_verdict"]["affected_service"] = "frontend"
    res = synthesize(bundle)
    assert res.runbook_mode == "new"
    assert res.related_runbook_id == "rb-frontend"


def test_quality_score_is_high_for_complete_incident():
    res = synthesize(_bundle())
    assert res.quality_score >= 0.7


# ─── redaction ───────────────────────────────────────────────────────────────


def test_pii_in_root_cause_is_redacted_in_kb_body():
    bundle = _bundle(incident_id="INC-PII-1")
    bundle["rca_verdict"]["root_cause"] += " Reported by oncall@example.com from 10.0.0.5."
    res = synthesize(bundle)
    stored = repo.get_kb_article(res.kb_article_id)
    assert "oncall@example.com" not in stored["body"]
    assert "10.0.0.5" not in stored["body"]
    assert "[REDACTED_EMAIL]" in stored["body"]
    assert "email" in res.redaction_summary


def test_clean_incident_reports_no_redaction():
    res = synthesize(_bundle())
    assert res.redaction_summary == "no PII/secrets detected"


# ─── idempotency + dedup ─────────────────────────────────────────────────────


def test_same_incident_is_idempotent():
    first = synthesize(_bundle(incident_id="INC-DUP"))
    second = synthesize(_bundle(incident_id="INC-DUP"))
    assert second.dedup_action == "skip_idempotent"
    assert second.kb_article_id == first.kb_article_id
    assert repo.count_kb_articles() == 1  # no second row


def test_near_duplicate_different_incident_is_deduped():
    first = synthesize(_bundle(incident_id="INC-A"))
    # Different incident id, same service + root cause → signature duplicate.
    second = synthesize(_bundle(incident_id="INC-B"))
    assert second.dedup_action == "duplicate"
    assert second.kb_article_id == first.kb_article_id
    assert repo.count_kb_articles() == 1  # no near-identical second doc


def test_different_service_is_not_deduped():
    synthesize(_bundle(incident_id="INC-A"))
    bundle = _bundle(incident_id="INC-CART")
    bundle["triage_verdict"]["affected_service"] = "cart"
    bundle["rca_verdict"]["affected_service"] = "cart"
    bundle["rca_verdict"]["root_cause"] = "flagd flag cartFailure is on; cart returns 5xx."
    res = synthesize(bundle)
    assert res.dedup_action == "create"
    assert repo.count_kb_articles() == 2


# ─── harness contract ────────────────────────────────────────────────────────


def test_run_returns_serializable_dict():
    out = run(_bundle())
    assert out["affected_service"] == "user-service"
    assert out["status"] == "pending_review"
    assert out["dedup_action"] == "create"
    assert "postmortem" in out and "kb_article" in out


def test_reset_state_wipes_kb_articles():
    synthesize(_bundle())
    assert repo.count_kb_articles() == 1
    from agents.knowledge_synthesizer.agent import reset_state

    reset_state()
    assert repo.count_kb_articles() == 0


# ─── idempotency-key fallback (review #2: no real incident id) ─────────────────


def _idless(service: str, summary: str, created: str = "2026-06-12T10:00:00Z") -> dict:
    """A bundle with NO incident_id / ticket / triage incident_id — exercises
    the fallback key path."""
    return {
        "scenario_id": None,
        "resolved_at": "2026-06-12T10:10:00Z",
        "triage_verdict": {
            "affected_service": service,
            "severity": "Sev-2",
            "alert_summary": summary,
            "audit_metadata": {"created_at": created},
        },
        "rca_verdict": {
            "affected_service": service,
            "root_cause": f"root cause for {summary}",
            "ranked_fix_steps": [],
            "confidence_score": 0.5,
            "audit_metadata": {"created_at": created},
        },
    }


def test_idless_different_incidents_same_service_both_synthesize():
    # Two DISTINCT incidents on the same service, neither with an id. The old
    # service-only key collided → 2nd skip_idempotent (silent data loss).
    r1 = synthesize(_idless("payment", "error rate high"))
    r2 = synthesize(_idless("payment", "latency spike"))
    assert r1.dedup_action == "create"
    assert r2.incident_id != r1.incident_id
    assert r2.dedup_action != "skip_idempotent"  # the fix: not collided
    assert repo.count_kb_articles() == 2


def test_idless_same_incident_is_idempotent():
    b = _idless("cart", "cart 5xx")
    first = synthesize(b)
    second = synthesize(b)  # identical bundle → same stable key
    assert second.incident_id == first.incident_id
    assert second.dedup_action == "skip_idempotent"
    assert repo.count_kb_articles() == 1


def test_idless_no_discriminating_info_refuses():
    import pytest

    with pytest.raises(ValueError):
        synthesize(
            {
                "triage_verdict": {"affected_service": "x"},
                "rca_verdict": {
                    "affected_service": "x",
                    "root_cause": "r",
                    "ranked_fix_steps": [],
                    "confidence_score": 0.0,
                },
            }
        )
