"""Alert Triage agent (RA-001) — Reactive-Active phase.

Owns workflow steps 1-8 of the canonical alert→incident pipeline, then
classifies the incident (the former RA-002 Incident Classifier, now folded into
this one agent):

    1. Receive alert     5. Correlate metrics + traces
    2. Validate schema   6. Classify severity (Sev-1..Sev-4)
    3. Normalize fields  7. Resolve ownership (CMDB + on-call)
    4. Deduplicate       8. Generate incident summary
                         9. Classify incident type (infra / app / network /
                            external_dependency / change_related) via similar
                            past incidents, LLM, then keyword fallback

Steps 10-12 belong to downstream agents in the same phase:
    10. Create ticket      — Auto-Ticketing (ITSM provider)
    11. Send notification  — Notification Assembler (chat ops)
    12. Store audit logs   — platform persistence

Public surface::

    from agents.alert_triage import (
        Alert, AuditMetadata, TriageVerdict, triage, run,
        Classification, ClassificationInput, IncidentType, classify,
        CombinedResult, triage_and_classify,
    )
"""

from agents.alert_triage.agent import (
    classify,
    reset_state,
    run,
    triage,
    triage_and_classify,
)
from agents.alert_triage.classifier_models import (
    Classification,
    ClassificationInput,
    CombinedResult,
    IncidentType,
)
from agents.alert_triage.models import Alert, AuditMetadata, TriageVerdict

__all__ = [
    "Alert",
    "AuditMetadata",
    "Classification",
    "ClassificationInput",
    "CombinedResult",
    "IncidentType",
    "TriageVerdict",
    "classify",
    "reset_state",
    "run",
    "triage",
    "triage_and_classify",
]
