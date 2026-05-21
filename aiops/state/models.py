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
    # alert_id of the *originating* alert for this verdict. Used by the agent's
    # transport-layer idempotency check (Fragile #6 fix) — a duplicate delivery
    # of the same alert_id within a short window returns the cached verdict
    # instead of re-running the pipeline. Nullable for rows written before this
    # column existed.
    alert_id: str | None = Field(default=None, index=True)
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

    Embedding storage: L2-normalized centroid vector as a JSON list of floats
    (same shape as ``HistoricalIncidentRow.embedding``). Persisting it lets
    embedding-similarity dedup survive a process restart instead of cold-windowing
    until everything ages out. Empty list = no embedding (e.g. when the
    ``embeddings`` extra isn't installed). The centroid is maintained as a
    running mean (see ``agents.alert_triage.agent._EMA_ALPHA``) so the cluster
    is anchored to its origin while still tracking slow drift.
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
    embedding: list[float] = Field(default_factory=list, sa_column=Column(JSON))


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
    """Persists a Notification Router (RA-005) ``RoutingDecision`` so the
    dashboard's history view can answer "notifications by service over the
    last week" via SQL — instead of re-parsing ``demo/audit/chatops.jsonl``.

    The JSONL adapter keeps writing alongside this row; the structured
    column set is *additive* (CHAT-2 #82). Joinable by ``verdict_id`` against
    ``VerdictRow`` and (via the same id) against ``ClassificationRow`` and
    ``TicketRow``, which is what AUDIT-1 (#78) needs.

    ``actions`` is nullable JSON: CHAT-3 (#83) populates it with the logical
    actions taken (e.g. ``["page_oncall", "post_to_chat"]``). Until that lands
    we accept the empty list / None from the agent without blocking.
    """

    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    verdict_id: int = Field(foreign_key="verdicts.id", index=True)
    routed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    channel: str = Field(index=True)
    chat_severity: str = Field(index=True)  # p0 | p1 | p2 | p3 | info
    title: str
    body: str
    service: str | None = Field(default=None, index=True)
    # TODO(CHAT-3 #83): populate from RoutingDecision.actions once the agent
    # emits a stable action vocabulary. Nullable until then.
    actions: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    reason: str
    audit_trace: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class ClassificationRow(SQLModel, table=True):
    """Persists RA-002 Incident Classifier output. One row per classification.

    Linked to the originating RA-001 verdict by ``verdict_id``. The FK is
    nullable so RA-002 can also be run standalone (CLAUDE.md principle #2 —
    individually sellable agents). Audit fields (decision_trace, similar_
    incidents snapshot) live under ``audit_metadata`` as a JSON column to
    keep the row schema flat — same convention as ``VerdictRow``.
    """

    __tablename__ = "classifications"

    id: int | None = Field(default=None, primary_key=True)
    verdict_id: int | None = Field(default=None, foreign_key="verdicts.id", index=True)
    incident_type: str = Field(index=True)
    confidence: float
    rationale: str
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    probable_root_cause: str = ""
    routing_team: str = Field(default="", index=True)
    on_call_engineer: str | None = None
    recommended_runbook: str | None = None
    dependencies: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    similar_incident_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    audit_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )


class HistoricalIncidentRow(SQLModel, table=True):
    """Past incidents used by RA-002 (Incident Classifier) for similarity-driven
    classification. Seeded with synthetic incidents on first boot; the store
    grows over time as the agent classifies new incidents — that's how the
    agent "learns" without retraining.

    Embedding storage: L2-normalized vector stored as a JSON list of floats.
    Nearest search is brute-force cosine via dot product — fine for POC scale
    (a few thousand rows). Swap to pgvector when row count makes scanning slow.
    """

    __tablename__ = "historical_incidents"

    id: int | None = Field(default=None, primary_key=True)
    incident_key: str = Field(index=True)  # not unique: same alert may be re-classified
    incident_type: str = Field(index=True)
    affected_service: str = Field(index=True)
    severity: str
    summary: str
    probable_root_cause: str
    recommended_runbook: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    embedding: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    embedding_text: str
    source: str = Field(default="live", index=True)  # "seed" | "live"
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )


__all__ = [
    "ClassificationRow",
    "ClusterRow",
    "HistoricalIncidentRow",
    "NotificationRow",
    "TicketRow",
    "VerdictRow",
]
