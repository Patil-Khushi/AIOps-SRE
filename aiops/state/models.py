"""Row models for persistent state.

JSON columns hold lists / dicts that are nested or grow over time
(audit_metadata, decision_trace, source_alerts). Keeps the schema flat — no
join tables for what is effectively per-verdict structured logging.

Naming: ``*Row`` to make it obvious at call sites these are DB rows, not the
agent's Pydantic models. ``aiops.state.repository`` is responsible for
mapping between the two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class VerdictRow(SQLModel, table=True):
    __tablename__ = "verdicts"

    id: int | None = Field(default=None, primary_key=True)
    cluster_key: str = Field(index=True)
    affected_service: str = Field(index=True)
    severity: str = Field(index=True)
    confidence_score: float
    alert_summary: str
    assigned_team: str
    assigned_engineer: str | None = None
    recommended_runbook: str | None = None
    duplicate_alert_count: int = 1
    status: str = Field(index=True)
    incident_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    audit_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ClusterRow(SQLModel, table=True):
    """Persists dedup clusters across uvicorn restarts.

    Embedding vectors are NOT stored — they live in an in-memory cache
    keyed by ``cluster_key`` for the lifetime of the agent process. After a
    restart, exact-key dedup keeps working from the first new alert; the
    embedding-similarity path degrades for one 5-minute window then heals.
    """

    __tablename__ = "clusters"

    cluster_key: str = Field(primary_key=True)
    service: str = Field(index=True)
    metric: str
    first_seen: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )
    last_seen: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    alert_count: int = 1
    source_alerts: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class TicketRow(SQLModel, table=True):
    """Placeholder for Auto-Ticketing (RA-003). Created when that agent lands
    so verdict → ticket lineage is queryable end-to-end."""

    __tablename__ = "tickets"

    id: int | None = Field(default=None, primary_key=True)
    verdict_id: int = Field(foreign_key="verdicts.id", index=True)
    external_id: str | None = Field(default=None, index=True)  # ServiceNow / Jira number
    system: str  # "servicenow" | "jira"
    state: str = "open"
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class NotificationRow(SQLModel, table=True):
    """Placeholder for Notification Router (RA-005). Lets the dashboard show
    'which alert pinged which channel'."""

    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    verdict_id: int = Field(foreign_key="verdicts.id", index=True)
    channel: str
    target: str  # slack channel id, team email, pagerduty service key
    status: str = "sent"  # sent | failed | suppressed
    sent_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


__all__ = ["ClusterRow", "NotificationRow", "TicketRow", "VerdictRow"]
