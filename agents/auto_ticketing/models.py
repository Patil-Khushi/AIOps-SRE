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
    channel_notified: str | None = None
    notification_sent: bool = False
    audit_metadata: list[str] = Field(default_factory=list)
