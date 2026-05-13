"""Alert Triage agent (RA-001) — Reactive-Active phase.

Owns workflow steps 1-8 of the canonical alert→incident pipeline:

    1. Receive alert     5. Correlate metrics + traces
    2. Validate schema   6. Classify severity (Sev-1..Sev-4)
    3. Normalize fields  7. Resolve ownership (CMDB + on-call)
    4. Deduplicate       8. Generate incident summary

Steps 9-12 belong to downstream agents in the same phase:
    9. Create ticket       — Auto-Ticketing (ITSM provider)
    10. Send notification  — Notification Router (chat ops)
    11. Store audit logs   — platform persistence
    12. Update dashboards  — post-v1

Public surface::

    from agents.alert_triage import Alert, TriageVerdict, AuditMetadata, triage, run
"""

from agents.alert_triage.agent import reset_state, run, triage
from agents.alert_triage.models import Alert, AuditMetadata, TriageVerdict

__all__ = ["Alert", "AuditMetadata", "TriageVerdict", "reset_state", "run", "triage"]
