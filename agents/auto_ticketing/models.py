"""Pydantic output model for the Auto-Ticketing agent (RA-003).

``TicketRecord`` is the agent's return shape. Field names align with the
catalog's documented output JSON: ``ticket_id`` (provider-issued id),
``system`` (servicenow / mock), ``urgency`` (1=High, 2=Medium, 3=Low —
ServiceNow's native convention), ``channel_notified`` (chat-ops sink),
``created`` (false when the agent intentionally skipped — e.g. duplicate
suppressed verdicts), plus a decision_trace mirroring alert_triage's
``AuditMetadata.decision_trace`` so every action is explainable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TicketSystem = Literal["servicenow", "mock", "none"]


class TicketRecord(BaseModel):
    """Auto-Ticketing agent output.

    ``created=False`` means the agent decided not to file a ticket (the
    Suppressed-verdict path). When ``created=True`` and ``ticket_id`` is
    ``None`` the ITSM provider returned an error — the agent still notified
    chat-ops with a creation-failed marker so humans see the alert.
    """

    model_config = ConfigDict(extra="forbid")

    created: bool
    ticket_id: str | None = None
    system: TicketSystem = "none"
    urgency: int | None = Field(default=None, ge=1, le=3)
    short_description: str | None = None
    # ServiceNow stock category the incident_type mapped to (software / hardware
    # / network / …); ``None`` when RA-002 classification was not supplied.
    category: str | None = None
    channel_notified: str | None = None
    notification_sent: bool = False
    # Ownership carried through from the verdict so downstream consumers
    # (RA-008 Incident Commander) don't have to re-open the triage verdict.
    assigned_team: str | None = None
    assigned_engineer: str | None = None
    # True only when the Grafana panel PNG actually attached to the incident.
    # Attachment is best-effort (DEMO-8 / #60): a failure here is non-fatal and
    # leaves this ``False`` while the ticket itself is still ``created``.
    attachment_added: bool = False
    audit_metadata: list[str] = Field(default_factory=list)
    # Stamped at construction. ``model_dump(mode="json")`` serializes this to an
    # ISO-8601 string for the API/eval surfaces.
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
