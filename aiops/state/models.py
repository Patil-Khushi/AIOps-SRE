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
    target: str  # e.g. "chatops:incidents-payments"
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


class KBArticleRow(SQLModel, table=True):
    """Knowledge-base article produced by the Knowledge Synthesizer (PRS-007).

    Drafts and published articles share this table, distinguished by
    ``status`` (draft | pending_review | published | rejected). Publication is
    platform-HITL-gated (``knowledge.publish``, Required) — a row only reaches
    ``published`` after a human approves — so this table doubles as the
    draft-pending-review store for the review workflow.

    ``incident_id`` is the idempotency key: ``find_kb_by_incident_id`` guards
    against synthesizing the same resolved incident into a second article.

    Embedding storage mirrors ``HistoricalIncidentRow``: an L2-normalized
    vector as a JSON list of floats, so dedup ("is there a near-identical
    article?") and the v0 RAG retrieval both run as brute-force cosine via dot
    product. Swap to pgvector when row count makes scanning slow.
    """

    __tablename__ = "kb_articles"

    id: int | None = Field(default=None, primary_key=True)
    incident_id: str | None = Field(default=None, index=True)
    title: str
    summary: str = ""
    body: str  # redacted markdown
    service: str = Field(default="", index=True)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="pending_review", index=True)
    quality_score: float = 0.0
    related_runbook_id: str | None = None
    # HITL linkage: the approval request gating publication, and who approved.
    approval_id: str | None = Field(default=None, index=True)
    approved_by: str | None = None
    source: str = "PRS-007"
    embedding: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    embedding_text: str = ""
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )
    audit_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class RCAResultRow(SQLModel, table=True):
    """RCA verdict stored keyed by incident id.

    RCA is otherwise computed on demand and not persisted. The SNOW watcher and
    resolution verifier need the RCA context for a resolved ticket without
    re-running RCA, so the verdict is stashed here (populated additively at
    fix-apply time). One row per persistence; ``get_rca_result`` returns the
    most recent for an incident id.
    """

    __tablename__ = "rca_results"

    id: int | None = Field(default=None, primary_key=True)
    incident_id: str = Field(index=True)
    affected_service: str = Field(default="", index=True)
    verdict: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )


class EngineerRow(SQLModel, table=True):
    """An on-call engineer / SRE who can be paged.

    The ``slack_handle`` is the vendor-neutral mention RA-005 emits
    (``@chinmay``); ``slack_user_id`` is what the Slack adapter rewrites
    that handle to for a real ping (``<@U12345>``). Both are nullable so
    you can seed engineers without Slack and add it later via a single
    UPDATE.

    Skills are stored as a comma-separated string in ``skills_csv``. POC
    scale (5–50 engineers, a handful of skills each) doesn't need a
    join table. Promote to a proper many-to-many when the engineer count
    crosses a few hundred.
    """

    __tablename__ = "engineers"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    email: str = Field(index=True, unique=True)
    slack_handle: str | None = Field(default=None, index=True)  # "@chinmay" or "chinmay"
    slack_user_id: str | None = Field(default=None)  # "U12345"
    timezone: str = "UTC"
    primary_team: str = Field(index=True)
    skills_csv: str = ""  # "payments,kafka,kubernetes"
    active: bool = Field(default=True, index=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )

    @property
    def skills(self) -> list[str]:
        return [s.strip() for s in self.skills_csv.split(",") if s.strip()]


class ShiftRow(SQLModel, table=True):
    """A recurring weekly on-call shift.

    ``day_of_week`` is 0..6 with 0 = Monday (matches Python's
    ``datetime.weekday()``). ``start_hour_utc`` and ``end_hour_utc`` are
    integers in [0, 24]. Shifts that cross midnight UTC are represented
    as two rows (one ending at 24, the next starting at 0 on the next
    day) rather than allowing ``end < start`` — keeps the lookup query
    a simple range check.

    ``role``:
    - ``primary``            — first to be paged for this team's alerts.
    - ``secondary``          — page if primary is overloaded or unreachable.
    - ``manager_escalation`` — final fallback if no one else is on shift.
    """

    __tablename__ = "shifts"

    id: int | None = Field(default=None, primary_key=True)
    engineer_id: int = Field(foreign_key="engineers.id", index=True)
    team: str = Field(index=True)
    day_of_week: int = Field(index=True)  # 0=Mon..6=Sun
    start_hour_utc: int  # 0..24
    end_hour_utc: int  # 0..24, > start
    role: str = Field(default="primary", index=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )


class FailureCategoryRow(SQLModel, table=True):
    """A sub-domain within a team's responsibility.

    Examples for the Payments team:
    - ``payment-gateway`` — third-party gateway integration issues
    - ``payment-database`` — DB connection / query failures
    - ``payment-kafka`` — event-streaming bottlenecks

    Alerts are matched to categories by intersecting the alert's tokens
    (service + alert_summary + recommended_runbook) with ``keywords_csv``.
    ALL categories whose keyword set overlaps the alert are considered;
    each one's expertise score is then multiplied by its overlap count
    (see ``oncall_repository.find_best_for_team_and_category``), so a
    category matched on a specific term beats one matched only by the
    generic team marker (e.g. "payment"). If no category matches at
    all, routing falls back to the plain shift lookup so alerts are
    never dropped.

    ``team`` is stored as a plain string (matches ``EngineerRow.primary_team``)
    so we don't need a teams table at POC scale.
    """

    __tablename__ = "failure_categories"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # slug, e.g. "payment-gateway"
    display_name: str  # human-readable
    description: str = ""
    team: str = Field(index=True)  # which team owns this category
    keywords_csv: str = ""  # alert match keywords
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )

    @property
    def keywords(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords_csv.split(",") if k.strip()]


class EngineerExpertiseRow(SQLModel, table=True):
    """One row per (engineer, category) pair — the expertise ranking signal.

    Routing scoring formula (see ``_score_expertise`` in
    ``oncall_repository.py``)::

        expertise_score = proficiency_weight[proficiency_level]
                        + min(incidents_resolved, 25) * 2
                        + feedback_score * 20
                        + manual_priority * 50
        weighted_score  = expertise_score × keyword_overlap_count

    Higher weighted score wins; ties break on lowest engineer_id.

    ``feedback_score`` is the mean of post-incident review ratings on a
    1.0..5.0 scale. Default 3.0 (neutral) until a real rating arrives.

    ``last_resolved_at`` is stored for future recency-bonus weighting
    but not yet read by the scorer — the POC scoring intentionally
    leans on track record + feedback only so the formula stays simple
    enough to be explainable in a single screenshot.
    """

    __tablename__ = "engineer_expertise"

    engineer_id: int = Field(foreign_key="engineers.id", primary_key=True)
    category_id: int = Field(foreign_key="failure_categories.id", primary_key=True)
    # "novice" | "intermediate" | "expert" | "principal"
    proficiency_level: str = Field(default="intermediate", index=True)
    incidents_resolved: int = 0
    feedback_score: float = 3.0  # 1.0..5.0 (neutral default)
    last_resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    manual_priority: int = 0  # operator override
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )


__all__ = [
    "ClassificationRow",
    "ClusterRow",
    "EngineerExpertiseRow",
    "EngineerRow",
    "FailureCategoryRow",
    "HistoricalIncidentRow",
    "KBArticleRow",
    "NotificationRow",
    "RCAResultRow",
    "ShiftRow",
    "TicketRow",
    "VerdictRow",
]
