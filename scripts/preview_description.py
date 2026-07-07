"""Print the description body RA-003 would send to ServiceNow.

Run: ``uv run python scripts/preview_description.py``
"""

from datetime import UTC, datetime

from agents.alert_triage.classifier_models import AuditMetadata as CA
from agents.alert_triage.classifier_models import Classification
from agents.alert_triage.models import AuditMetadata as TA
from agents.alert_triage.models import TriageVerdict
from agents.auto_ticketing.agent import _build_description

v = TriageVerdict(
    affected_service="payment",
    severity="Sev-1",
    confidence_score=0.92,
    alert_summary="payment 5xx error rate 0.05/s above 0.01/s threshold (Prometheus)",
    assigned_team="Payments Team",
    assigned_engineer="oncall@payments.example.com",
    recommended_runbook="https://runbooks.example.com/payment-5xx",
    duplicate_alert_count=1,
    status="Active",
    audit_metadata=TA(
        created_at=datetime.now(UTC),
        source_alerts=["ALT-1"],
        decision_trace=[
            "ingested alert ALT-1",
            "dedup: cluster miss",
            "embed: signal built",
            "LLM severity: Sev-1",
            "CMDB lookup: Payments Team",
            "on-call: oncall@payments",
            "runbook resolved",
            "assembled verdict",
        ],
    ),
)

c = Classification(
    incident_type="application",
    confidence=0.78,
    rationale="5xx pattern matches past application outages",
    tags=["5xx", "payment"],
    probable_root_cause="downstream Stripe API rejections",
    routing_team="Payments Team",
    dependencies=[],
    similar_incident_ids=[],
    audit_metadata=CA(created_at=datetime.now(UTC)),
)

print("=" * 70)
print("DESCRIPTION RA-003 WOULD SEND TO SERVICENOW:")
print("=" * 70)
print(_build_description(v, c))
