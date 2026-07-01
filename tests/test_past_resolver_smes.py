"""Tests for the past-resolver SME memory (RA-005+006).

When a war room is resolved, the SMEs who fixed it are recorded; on a later
Sev-1/Sev-2 incident on the same service + failure sub-domain, they're re-invited
alongside the on-call and dependency owners.

Covers three layers:
- repository: save/list with de-dup, category scoping, service-wide fallback, limit
- agent: `_past_resolver_smes` re-invites via the `incident.resolvers.lookup` seam
- server: `_record_resolvers_from_row` records joined SMEs (or on-call fallback)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aiops import state as state_pkg
from aiops.state import repository as repo


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


# ─── repository ────────────────────────────────────────────────────────────


def test_save_and_list_newest_first_deduped():
    repo.save_incident_resolver(affected_service="payment", resolver_handle="@amit")
    repo.save_incident_resolver(affected_service="payment", resolver_handle="@bela")
    repo.save_incident_resolver(affected_service="payment", resolver_handle="@amit")  # again, newer

    rows = repo.list_incident_resolvers(affected_service="payment")
    handles = [r["resolver_handle"] for r in rows]
    # De-duped, most-recent-first: amit's later record floats him to the top.
    assert handles == ["@amit", "@bela"]


def test_category_scoping_and_service_wide_fallback():
    repo.save_incident_resolver(
        affected_service="payment", category="Payment Gateway", resolver_handle="@gw"
    )
    repo.save_incident_resolver(
        affected_service="payment", category="Payment Database", resolver_handle="@db"
    )

    # Sub-domain scoped: only the gateway resolver.
    gw = repo.list_incident_resolvers(affected_service="payment", category="Payment Gateway")
    assert [r["resolver_handle"] for r in gw] == ["@gw"]

    # Service-wide (category=None): both sub-domains.
    allp = repo.list_incident_resolvers(affected_service="payment")
    assert set(r["resolver_handle"] for r in allp) == {"@gw", "@db"}

    # A different service sees nobody.
    assert repo.list_incident_resolvers(affected_service="cart") == []


def test_limit_caps_results():
    for i in range(5):
        repo.save_incident_resolver(affected_service="payment", resolver_handle=f"@eng{i}")
    assert len(repo.list_incident_resolvers(affected_service="payment", limit=2)) == 2


# ─── agent: re-invite via the seam ─────────────────────────────────────────


def _verdict(severity="Sev-1", service="payment", incident_id="INC-1"):
    from agents.alert_triage import AuditMetadata, TriageVerdict

    return TriageVerdict(
        incident_id=incident_id,
        affected_service=service,
        severity=severity,  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary=f"{service} degraded",
        assigned_team="Payments Team",
        assigned_engineer="oncall@payments.example.com",
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 13, 2, 0, tzinfo=UTC), source_alerts=["a-1"]
        ),
    )


def test_agent_invites_past_resolver_on_recurrence():
    import aiops.tools.resolvers  # noqa: F401 — registers incident.resolvers.lookup
    from agents.notification_assembler import decide

    # A prior incident on payment was resolved by @amit.
    repo.save_incident_resolver(affected_service="payment", resolver_handle="@amit")

    plan = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
    assert plan.war_room is not None and plan.war_room.assembled is True

    past = [s for s in plan.war_room.invited if s.source == "past_resolver"]
    assert [s.handle for s in past] == ["@amit"]
    assert "resolved a past payment incident" in past[0].reason


def test_agent_no_past_resolver_when_none_recorded():
    import aiops.tools.resolvers  # noqa: F401
    from agents.notification_assembler import decide

    plan = decide(_verdict(severity="Sev-1"), now=datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
    assert plan.war_room is not None
    assert [s for s in plan.war_room.invited if s.source == "past_resolver"] == []


# ─── server: record on resolve ─────────────────────────────────────────────


def test_record_resolvers_prefers_joined_then_falls_back_to_oncall():
    from demo.ui import server as srv

    # Row with a joined SME + an on-call who didn't join → only the joined one.
    row_joined = {
        "service": "payment",
        "notification": {"category_display": "Payment Gateway"},
        "assembly": {
            "invited": [
                {
                    "handle": "@oncall",
                    "source": "oncall",
                    "attendance": "invited",
                    "name": "On Call",
                },
                {
                    "handle": "@helper",
                    "source": "dependency_owner",
                    "attendance": "joined",
                    "name": "Helper",
                },
            ]
        },
    }
    assert srv._record_resolvers_from_row(row_joined) == 1
    rows = repo.list_incident_resolvers(affected_service="payment", category="Payment Gateway")
    assert [r["resolver_handle"] for r in rows] == ["@helper"]

    # Row where nobody joined → fall back to the on-call SME.
    row_none = {
        "service": "cart",
        "notification": {"category_display": None},
        "assembly": {
            "invited": [
                {"handle": "@cartoncall", "source": "oncall", "attendance": "invited"},
            ]
        },
    }
    assert srv._record_resolvers_from_row(row_none) == 1
    assert [
        r["resolver_handle"] for r in repo.list_incident_resolvers(affected_service="cart")
    ] == ["@cartoncall"]
