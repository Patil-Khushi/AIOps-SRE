"""Tests for the RA-003 description / classification / decision-trace body.

DEMO-3 / #55: a ServiceNow incident must carry the full triage context in
its ``description`` field, plus ``assignment_group`` and ``category``, so a
human triaging the ticket can act without re-running the agent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agents.alert_triage.models import AuditMetadata as TriageAudit
from agents.alert_triage.models import TriageVerdict
from agents.auto_ticketing import ticket
from agents.incident_classifier.models import AuditMetadata as ClsAudit
from agents.incident_classifier.models import Classification


@pytest.fixture(autouse=True)
def _stub_llm_and_mock_itsm(monkeypatch):
    """Force the stub LLM + the mock ITSM provider for deterministic assertions."""
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.setenv("AIOPS_USE_MOCK_ITSM", "true")
    from aiops.tools import get_registry, mock_providers  # noqa: F401

    get_registry().select_provider("itsm.incident.create", "mock.itsm.incident.create")
    yield


def _verdict(decision_trace: list[str] | None = None) -> TriageVerdict:
    return TriageVerdict(
        affected_service="payment",
        severity="Sev-1",
        confidence_score=0.92,
        alert_summary="payment 5xx error rate 0.05/s above 0.01/s threshold (source: Prometheus)",
        assigned_team="Payments Team",
        assigned_engineer="oncall@payments.example.com",
        recommended_runbook="https://runbooks.example.com/payment-5xx",
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=TriageAudit(
            created_at=datetime(2026, 5, 14, 10, 0, tzinfo=UTC),
            created_by="RA-001",
            source_alerts=["ALT-1001"],
            decision_trace=decision_trace
            or [
                "ingested alert ALT-1001",
                "dedup: cluster miss, treating as new",
                "embed: signal vector built",
                "LLM severity: Sev-1 (confidence 0.92)",
                "CMDB lookup: team=Payments Team",
                "on-call lookup: engineer=oncall@payments.example.com",
                "runbook resolved",
                "assembled verdict",
            ],
        ),
    )


def _classification() -> Classification:
    return Classification(
        incident_type="application",
        confidence=0.78,
        rationale="5xx pattern matches past application-tier outages",
        tags=["5xx", "payment", "checkout-blocker"],
        probable_root_cause="downstream Stripe API rejections elevated 5xx in payment-service",
        routing_team="Payments Team",
        on_call_engineer="oncall@payments.example.com",
        recommended_runbook="https://runbooks.example.com/payment-5xx",
        dependencies=["currency", "fraud-detection"],
        similar_incident_ids=["HIST-001", "HIST-009"],
        audit_metadata=ClsAudit(
            created_at=datetime(2026, 5, 14, 10, 0, tzinfo=UTC),
            created_by="RA-002",
            decision_trace=["Tier-1 similarity wins"],
            similar_incidents=[],
        ),
    )


def _last_create_payload(monkeypatch) -> dict[str, Any]:
    """Spy on ``itsm.incident.create`` and return the kwargs RA-003 sent.

    The registry filters kwargs against the registered function's signature,
    so the spy must mirror ``mock_create_incident``'s parameters exactly —
    a bare ``**kwargs`` shim would lose every named arg at the registry's
    ``inspect.signature`` filter.
    """
    from aiops.tools import get_registry
    from aiops.tools import mock_providers as mp

    captured: dict[str, Any] = {}
    real_fn = mp.mock_create_incident

    def _spy(
        short_description: str,
        urgency: int = 3,
        description: str | None = None,
        assignment_group: str | None = None,
        category: str | None = None,
    ):
        captured["short_description"] = short_description
        captured["urgency"] = urgency
        captured["description"] = description
        captured["assignment_group"] = assignment_group
        captured["category"] = category
        return real_fn(
            short_description=short_description,
            urgency=urgency,
            description=description,
            assignment_group=assignment_group,
            category=category,
        )

    reg = get_registry()
    tool_obj = reg.by_capability("itsm.incident.create")
    monkeypatch.setattr(tool_obj, "fn", _spy)
    return captured


# ─── happy path: classification supplied ─────────────────────────────────────


def test_description_includes_alert_summary_routing_classification_and_trace(monkeypatch):
    captured = _last_create_payload(monkeypatch)
    record = ticket(_verdict(), classification=_classification())

    assert record.created is True
    assert record.ticket_id == "INC0000001"
    # New TicketRecord fields (#196): ownership carried through + mapped category.
    assert record.assigned_team == "Payments Team"
    assert record.assigned_engineer == "oncall@payments.example.com"
    assert record.category == "software"
    # No alert_name supplied here, so the Grafana attachment path never runs.
    assert record.attachment_added is False

    desc = captured["description"]
    assert isinstance(desc, str)

    # Section headers (=== style, #196)
    assert "=== Alert Summary ===" in desc
    assert "=== Routing ===" in desc
    assert "=== Classification (RA-002) ===" in desc
    assert "=== Decision Trace (RA-001) ===" in desc

    # Alert summary verbatim
    assert "0.05/s above 0.01/s threshold" in desc

    # Routing block — Team / Engineer / Runbook only (severity + confidence dropped)
    assert "Team:" in desc and "Payments Team" in desc
    assert "Engineer:" in desc and "oncall@payments.example.com" in desc
    assert "Runbook:" in desc and "https://runbooks.example.com/payment-5xx" in desc

    # Classification block filled; Rationale line intentionally dropped per #196,
    # and no pending placeholder
    assert "Type:" in desc and "application" in desc
    assert "Probable cause:" in desc and "downstream Stripe API rejections" in desc
    assert "Tags:" in desc and "5xx, payment, checkout-blocker" in desc
    assert "Rationale" not in desc
    assert "Pending" not in desc

    # Hybrid decision trace: the doc's CMDB-lookup + on-call lines, with the
    # full RA-001 8-stage trace preserved beneath them.
    assert "- CMDB lookup: payment -> Payments Team" in desc
    assert "- assigned on-call: oncall@payments.example.com (PagerDuty schedule)" in desc
    for n in range(1, 9):
        assert f"  {n}. " in desc
    assert "LLM severity: Sev-1" in desc


def test_assignment_group_and_category_passed_to_registry(monkeypatch):
    captured = _last_create_payload(monkeypatch)
    ticket(_verdict(), classification=_classification())

    assert captured["assignment_group"] == "Payments Team"
    # RA-002's ``incident_type=application`` maps to ServiceNow's stock
    # ``software`` category — see _INCIDENT_TYPE_TO_SNOW_CATEGORY.
    assert captured["category"] == "software"
    # short_description still carries the headline (existing contract)
    assert captured["short_description"].startswith("[Sev-1] payment:")
    # #196: urgency is forwarded to the ITSM seam as a string ("1".."3").
    assert captured["urgency"] == "1"


@pytest.mark.parametrize(
    "incident_type,expected_category",
    [
        ("infrastructure", "hardware"),
        ("application", "software"),
        ("network", "network"),
        ("external_dependency", "software"),
        ("change_related", "software"),
    ],
)
def test_incident_type_maps_to_servicenow_category(monkeypatch, incident_type, expected_category):
    """Every RA-002 incident_type must produce a stock ServiceNow category."""
    captured = _last_create_payload(monkeypatch)
    c = _classification().model_copy(update={"incident_type": incident_type})
    ticket(_verdict(), classification=c)
    assert captured["category"] == expected_category


# ─── classification missing (eval-harness path) ─────────────────────────────


def test_description_omits_classification_block_when_classification_missing(monkeypatch):
    captured = _last_create_payload(monkeypatch)
    record = ticket(_verdict())  # no classification

    assert record.created is True
    desc = captured["description"]
    # #196: no placeholder — the whole classification section is omitted when
    # RA-002 has not run.
    assert "=== Classification (RA-002) ===" not in desc
    assert "Pending" not in desc
    # The other sections still render.
    assert "=== Alert Summary ===" in desc
    assert "=== Routing ===" in desc
    assert "=== Decision Trace (RA-001) ===" in desc
    # category is omitted when classification is unavailable
    assert captured["category"] is None
    assert record.category is None
    # assignment_group still flows from the verdict
    assert captured["assignment_group"] == "Payments Team"
    assert record.assigned_team == "Payments Team"


def test_description_handles_missing_optional_verdict_fields(monkeypatch):
    """assigned_engineer / runbook are optional on the verdict — they render
    as 'unassigned' / 'none' instead of crashing."""
    captured = _last_create_payload(monkeypatch)
    v = _verdict()
    v = v.model_copy(update={"assigned_engineer": None, "recommended_runbook": None})
    ticket(v)

    desc = captured["description"]
    assert "Engineer: unassigned" in desc
    assert "none" in desc
    # on-call also reflects 'unassigned' in the decision-trace block
    assert "- assigned on-call: unassigned (PagerDuty schedule)" in desc


# ─── suppressed: nothing should be sent at all ─────────────────────────────


def test_suppressed_verdict_does_not_call_registry(monkeypatch):
    captured = _last_create_payload(monkeypatch)
    v = _verdict()
    v = v.model_copy(update={"status": "Suppressed", "duplicate_alert_count": 5})
    record = ticket(v, classification=_classification())

    assert record.created is False
    assert record.ticket_id is None
    assert captured == {}  # spy never fired
