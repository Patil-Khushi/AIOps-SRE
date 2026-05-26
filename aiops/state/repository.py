"""Repository — the agent-facing API for persistent state.

Agents and the demo server only import functions from this module; they
never see a Session, an Engine, or a row class directly. That keeps the
"agents don't know about the database" invariant the same way
``aiops.llm`` keeps "agents don't know about the LLM SDK".

Everything here is sync. The demo server already wraps sync work in
``asyncio.to_thread`` for the live-triage endpoint, so adding async
sessions on top would be net complexity without a payoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from aiops.state import get_engine
from aiops.state.models import (
    ClassificationRow,
    ClusterRow,
    HistoricalIncidentRow,
    NotificationRow,
    TicketRow,
    VerdictRow,
)


def _session() -> Session:
    return Session(get_engine())


# ─── verdicts ──────────────────────────────────────────────────────────────


def save_verdict(
    verdict: Any,
    cluster_key: str,
    *,
    alert_id: str | None = None,
) -> int:
    """Persist a triage verdict. Accepts the agent's Pydantic ``TriageVerdict``
    and the cluster_key it was assigned to. ``alert_id`` (the originating
    alert for this verdict) is written for the transport-layer idempotency
    lookup. Returns the row id."""
    audit = verdict.audit_metadata
    row = VerdictRow(
        cluster_key=cluster_key,
        alert_id=alert_id,
        affected_service=verdict.affected_service,
        severity=verdict.severity,
        confidence_score=verdict.confidence_score,
        alert_summary=verdict.alert_summary,
        assigned_team=verdict.assigned_team,
        assigned_engineer=verdict.assigned_engineer,
        recommended_runbook=verdict.recommended_runbook,
        duplicate_alert_count=verdict.duplicate_alert_count,
        status=verdict.status,
        incident_id=verdict.incident_id,
        created_at=audit.created_at,
        audit_metadata={
            "created_by": audit.created_by,
            "source_alerts": list(audit.source_alerts),
            "decision_trace": list(audit.decision_trace),
        },
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def find_recent_verdict_by_alert_id(alert_id: str, *, window: timedelta) -> dict[str, Any] | None:
    """Return the most recently created verdict for ``alert_id`` within
    ``window``, or ``None`` if none exists. Used by the alert_triage agent's
    transport-layer idempotency check: a duplicate delivery of the same
    alert_id returns the cached verdict instead of re-running the pipeline.

    Empty ``alert_id`` is rejected to avoid grouping malformed inputs together.
    """
    if not alert_id:
        return None
    cutoff = datetime.now(UTC) - window
    stmt = (
        select(VerdictRow)
        .where(VerdictRow.alert_id == alert_id)
        .where(VerdictRow.created_at >= cutoff)  # type: ignore[arg-type]
        .order_by(VerdictRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    with _session() as s:
        row = s.exec(stmt).first()
    return _verdict_row_to_dict(row) if row else None


def list_verdicts(
    *,
    limit: int = 50,
    service: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first list of persisted verdicts, rendered as plain dicts that
    serialize directly to JSON (matches the canonical TriageVerdict shape)."""
    stmt = select(VerdictRow).order_by(VerdictRow.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    if service:
        stmt = stmt.where(VerdictRow.affected_service == service)
    if severity:
        stmt = stmt.where(VerdictRow.severity == severity)
    with _session() as s:
        rows = s.exec(stmt).all()
    return [_verdict_row_to_dict(r) for r in rows]


def get_verdict(verdict_id: int) -> dict[str, Any] | None:
    with _session() as s:
        row = s.get(VerdictRow, verdict_id)
    return _verdict_row_to_dict(row) if row else None


def _verdict_row_to_dict(row: VerdictRow) -> dict[str, Any]:
    audit = dict(row.audit_metadata or {})
    return {
        "id": row.id,
        "cluster_key": row.cluster_key,
        "incident_id": row.incident_id,
        "affected_service": row.affected_service,
        "severity": row.severity,
        "confidence_score": row.confidence_score,
        "alert_summary": row.alert_summary,
        "assigned_team": row.assigned_team,
        "assigned_engineer": row.assigned_engineer,
        "recommended_runbook": row.recommended_runbook,
        "duplicate_alert_count": row.duplicate_alert_count,
        "status": row.status,
        "audit_metadata": {
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "created_by": audit.get("created_by", "RA-001"),
            "source_alerts": audit.get("source_alerts", []),
            "decision_trace": audit.get("decision_trace", []),
        },
    }


# ─── clusters (dedup) ──────────────────────────────────────────────────────


def find_active_cluster(cluster_key: str, *, window: timedelta) -> dict[str, Any] | None:
    """Return the cluster row if it exists and its ``last_seen`` is within the
    sliding window. Otherwise None — caller will create a new cluster."""
    cutoff = datetime.now(UTC) - window
    with _session() as s:
        row = s.get(ClusterRow, cluster_key)
        if row is None:
            return None
        last_seen = _aware(row.last_seen)
        if last_seen < cutoff:
            return None
        return _cluster_row_to_dict(row)


def upsert_cluster(
    *,
    cluster_key: str,
    service: str,
    metric: str,
    alert_id: str,
    seen_at: datetime,
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    """Append ``alert_id`` to the cluster (creating it on first sighting).
    Returns the up-to-date cluster as a dict.

    ``embedding``, when provided, is the L2-normalized centroid the caller
    wants persisted. Pass it on the new-cluster path to seed, and on each
    similarity-match path to update with the running mean — see
    ``agents.alert_triage.agent._dedup``.
    """
    with _session() as s:
        row = s.get(ClusterRow, cluster_key)
        if row is None:
            row = ClusterRow(
                cluster_key=cluster_key,
                service=service,
                metric=metric,
                first_seen=seen_at,
                last_seen=seen_at,
                alert_count=1,
                source_alerts=[alert_id],
                embedding=list(embedding) if embedding is not None else [],
            )
            s.add(row)
        else:
            existing = list(row.source_alerts or [])
            if alert_id not in existing:
                existing.append(alert_id)
            row.source_alerts = existing
            # alert_count tracks DELIVERIES (how many times the agent has been
            # called for this cluster), not distinct alert_ids — that count
            # is already exposed via len(source_alerts). The transport-layer
            # idempotency window above this layer (see triage()) absorbs
            # network retries, so every increment here is a genuine refire
            # the cluster should be credited with.
            row.alert_count = (row.alert_count or 0) + 1
            row.last_seen = seen_at
            if embedding is not None:
                row.embedding = list(embedding)
            s.add(row)
        s.commit()
        s.refresh(row)
        return _cluster_row_to_dict(row)


def list_active_clusters(window: timedelta) -> list[dict[str, Any]]:
    """All clusters whose last_seen is within ``window``. Used by the
    embedding dedup path on warm restart to know which cluster_keys to seed."""
    cutoff = datetime.now(UTC) - window
    with _session() as s:
        rows = s.exec(select(ClusterRow).where(ClusterRow.last_seen >= cutoff)).all()
    return [_cluster_row_to_dict(r) for r in rows]


def evict_expired_clusters(window: timedelta) -> int:
    """Delete clusters whose last_seen has aged out of the window. Returns
    the number of rows removed. Cheap to call on every triage."""
    cutoff = datetime.now(UTC) - window
    with _session() as s:
        rows = s.exec(select(ClusterRow).where(ClusterRow.last_seen < cutoff)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def delete_all_clusters() -> int:
    """Wipe every cluster row. Eval-harness hook; not a production path."""
    with _session() as s:
        rows = s.exec(select(ClusterRow)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def delete_all_verdicts() -> int:
    """Wipe every verdict row. Eval-harness hook; not a production path.
    Needed because alert_triage's idempotency layer reads from VerdictRow —
    leaving stale rows would silently short-circuit subsequent golden cases
    that happen to reuse an alert_id."""
    with _session() as s:
        rows = s.exec(select(VerdictRow)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def _aware(dt: datetime) -> datetime:
    """SQLite round-trips TIMESTAMP as naive UTC. Re-attach the tzinfo so
    comparisons against ``datetime.now(timezone.utc)`` don't blow up."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cluster_row_to_dict(row: ClusterRow) -> dict[str, Any]:
    return {
        "cluster_key": row.cluster_key,
        "service": row.service,
        "metric": row.metric,
        "first_seen": _aware(row.first_seen).isoformat(),
        "last_seen": _aware(row.last_seen).isoformat(),
        "alert_count": row.alert_count,
        "source_alerts": list(row.source_alerts or []),
        "embedding": list(row.embedding or []),
    }


# ─── tickets / notifications (Phase-1 follow-ups stub here so RA-003/005
# have a home without touching this file again) ────────────────────────────


def save_ticket(
    *,
    verdict_id: int,
    system: str,
    external_id: str | None,
    state: str = "open",
    payload: dict[str, Any] | None = None,
) -> int:
    row = TicketRow(
        verdict_id=verdict_id,
        system=system,
        external_id=external_id,
        state=state,
        payload=payload or {},
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def save_notification(decision: Any, verdict_id: int) -> int:
    """Persist an RA-005 ``RoutingDecision``. Returns the row id.

    ``decision`` is the agent's Pydantic ``RoutingDecision`` (channel, severity,
    title, body, actions, audit_trace, …). ``verdict_id`` is the row id of the
    originating RA-001 verdict from a prior ``save_verdict`` call — required so
    the dashboard can join notification → verdict → classification → ticket.

    ``service`` is read from the upstream verdict (not the decision — the
    agent's RoutingDecision doesn't carry the service field). We look it up
    here so the caller doesn't have to pass it as a third arg.

    CHAT-2 (#82): the JSONL audit log keeps writing in parallel; this is an
    additive structured row, not a replacement.
    """
    service: str | None = None
    with _session() as s:
        v = s.get(VerdictRow, verdict_id)
        if v is not None:
            service = v.affected_service

    row = NotificationRow(
        verdict_id=verdict_id,
        target=f"chatops:{decision.channel}",
        routed_at=getattr(decision, "decided_at", None) or datetime.now(UTC),
        channel=decision.channel,
        # ``chat_severity`` is the chatops enum — accept either the enum or
        # its string value so this is robust to whatever the agent emits.
        chat_severity=getattr(decision.chat_severity, "value", str(decision.chat_severity)),
        title=decision.title,
        body=decision.body,
        service=service,
        # TODO(CHAT-3 #83): once the agent settles on a stable action vocabulary,
        # populate from ``decision.actions``. For now we store the list verbatim
        # if non-empty, else NULL — keeps the column honestly "unpopulated".
        actions=list(decision.actions) if getattr(decision, "actions", None) else None,
        reason=decision.reason,
        audit_trace=list(getattr(decision, "audit_trace", []) or []),
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def list_notifications(
    *,
    limit: int = 50,
    service: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first list of persisted notifications. Optional filter by
    service. Renders rows as plain dicts safe to serialize to JSON."""
    stmt = (
        select(NotificationRow)
        .order_by(NotificationRow.routed_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    if service:
        stmt = stmt.where(NotificationRow.service == service)
    with _session() as s:
        rows = s.exec(stmt).all()
    return [_notification_row_to_dict(r) for r in rows]


def count_notifications() -> int:
    with _session() as s:
        rows = s.exec(select(NotificationRow)).all()
    return len(rows)


def _notification_row_to_dict(row: NotificationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "verdict_id": row.verdict_id,
        "routed_at": _aware(row.routed_at).isoformat() if row.routed_at else None,
        "channel": row.channel,
        "target": row.target,
        "chat_severity": row.chat_severity,
        "title": row.title,
        "body": row.body,
        "service": row.service,
        # ``actions`` is nullable (CHAT-3 #83 will populate); surface None as
        # [] so dashboard code doesn't need a null-check on every row.
        "actions": list(row.actions) if row.actions else [],
        "reason": row.reason,
        "audit_trace": list(row.audit_trace or []),
    }


# ─── classifications (RA-002 output) ───────────────────────────────────────


def save_classification(
    classification: Any,
    *,
    verdict_id: int | None = None,
) -> int:
    """Persist an RA-002 ``Classification``. Returns the row id.

    ``verdict_id`` links back to the RA-001 ``VerdictRow`` id from a prior
    ``save_verdict`` call. Pass ``None`` when RA-002 is used standalone
    (no upstream triage row in state).
    """
    audit = classification.audit_metadata
    row = ClassificationRow(
        verdict_id=verdict_id,
        incident_type=classification.incident_type,
        confidence=classification.confidence,
        rationale=classification.rationale,
        tags=list(classification.tags),
        probable_root_cause=classification.probable_root_cause,
        routing_team=classification.routing_team,
        on_call_engineer=classification.on_call_engineer,
        recommended_runbook=classification.recommended_runbook,
        dependencies=list(classification.dependencies),
        similar_incident_ids=list(classification.similar_incident_ids),
        created_at=audit.created_at,
        audit_metadata={
            "created_by": audit.created_by,
            "decision_trace": list(audit.decision_trace),
            "similar_incidents": list(audit.similar_incidents),
        },
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def get_classification(classification_id: int) -> dict[str, Any] | None:
    """Read-back counterpart to ``save_classification``. Matches the
    ``get_verdict`` shape so the dashboard can render both with one helper."""
    with _session() as s:
        row = s.get(ClassificationRow, classification_id)
    return _classification_row_to_dict(row) if row else None


def list_classifications(
    *,
    limit: int = 50,
    incident_type: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first list of persisted classifications, rendered as plain dicts.
    Optional filter by ``incident_type``."""
    stmt = (
        select(ClassificationRow)
        .order_by(ClassificationRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    if incident_type:
        stmt = stmt.where(ClassificationRow.incident_type == incident_type)
    with _session() as s:
        rows = s.exec(stmt).all()
    return [_classification_row_to_dict(r) for r in rows]


def count_classifications() -> int:
    with _session() as s:
        rows = s.exec(select(ClassificationRow)).all()
    return len(rows)


def average_classification_confidence() -> float | None:
    """Mean confidence across all persisted classifications, or None if empty."""
    with _session() as s:
        rows = s.exec(select(ClassificationRow.confidence)).all()
    if not rows:
        return None
    return sum(float(r) for r in rows) / len(rows)


def _classification_row_to_dict(row: ClassificationRow) -> dict[str, Any]:
    audit = dict(row.audit_metadata or {})
    return {
        "id": row.id,
        "verdict_id": row.verdict_id,
        "incident_type": row.incident_type,
        "confidence": row.confidence,
        "rationale": row.rationale,
        "tags": list(row.tags or []),
        "probable_root_cause": row.probable_root_cause,
        "routing_team": row.routing_team,
        "on_call_engineer": row.on_call_engineer,
        "recommended_runbook": row.recommended_runbook,
        "dependencies": list(row.dependencies or []),
        "similar_incident_ids": list(row.similar_incident_ids or []),
        "audit_metadata": {
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "created_by": audit.get("created_by", "RA-002"),
            "decision_trace": audit.get("decision_trace", []),
            "similar_incidents": audit.get("similar_incidents", []),
        },
    }


# ─── historical incidents (RA-002 similarity store) ────────────────────────


def count_historical_incidents() -> int:
    with _session() as s:
        rows = s.exec(select(HistoricalIncidentRow)).all()
    return len(rows)


def save_historical_incident(
    *,
    incident_key: str,
    incident_type: str,
    affected_service: str,
    severity: str,
    summary: str,
    probable_root_cause: str,
    recommended_runbook: str | None,
    tags: list[str],
    embedding: list[float],
    embedding_text: str,
    source: str = "live",
    created_at: datetime | None = None,
) -> int:
    """Append a row to the historical store. ``embedding`` should be L2-normalized
    so nearest_historical_incidents can use a plain dot product."""
    row = HistoricalIncidentRow(
        incident_key=incident_key,
        incident_type=incident_type,
        affected_service=affected_service,
        severity=severity,
        summary=summary,
        probable_root_cause=probable_root_cause,
        recommended_runbook=recommended_runbook,
        tags=list(tags),
        embedding=list(embedding),
        embedding_text=embedding_text,
        source=source,
        created_at=created_at or datetime.now(UTC),
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def nearest_historical_incidents(
    *,
    embedding: list[float],
    k: int = 5,
    min_similarity: float = 0.6,
) -> list[dict[str, Any]]:
    """Brute-force cosine nearest-K. Requires ``embedding`` to be L2-normalized
    (so are the stored vectors). Loads the table in memory — fine up to a few
    thousand rows; the schema is ready to be swapped to pgvector when this
    stops being fine.

    Returns rows with an added ``similarity`` field, descending by similarity,
    filtered to ``similarity >= min_similarity``.
    """
    if not embedding:
        return []
    with _session() as s:
        rows = s.exec(select(HistoricalIncidentRow)).all()
    scored: list[tuple[float, HistoricalIncidentRow]] = []
    for r in rows:
        v = r.embedding or []
        if len(v) != len(embedding):
            continue
        # L2-normalized vectors → dot product == cosine similarity.
        sim = 0.0
        for a, b in zip(embedding, v, strict=False):
            sim += a * b
        if sim >= min_similarity:
            scored.append((sim, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict[str, Any]] = []
    for sim, r in scored[:k]:
        out.append(
            {
                "incident_key": r.incident_key,
                "incident_type": r.incident_type,
                "affected_service": r.affected_service,
                "severity": r.severity,
                "summary": r.summary,
                "probable_root_cause": r.probable_root_cause,
                "recommended_runbook": r.recommended_runbook,
                "tags": list(r.tags or []),
                "similarity": sim,
                "source": r.source,
            }
        )
    return out


def delete_all_historical_incidents() -> int:
    """Eval/test hook. Not a production path."""
    with _session() as s:
        rows = s.exec(select(HistoricalIncidentRow)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def delete_live_historical_incidents() -> int:
    """Eval-harness hook — wipe rows that RA-002 inserted from live
    classifications (source != 'seed'), keeping the seed baseline intact so
    each golden case starts from the same retrieval surface."""
    with _session() as s:
        rows = s.exec(
            select(HistoricalIncidentRow).where(HistoricalIncidentRow.source != "seed")
        ).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


__all__ = [
    "average_classification_confidence",
    "count_classifications",
    "count_historical_incidents",
    "count_notifications",
    "delete_all_clusters",
    "delete_all_historical_incidents",
    "delete_live_historical_incidents",
    "evict_expired_clusters",
    "find_active_cluster",
    "get_classification",
    "get_verdict",
    "list_active_clusters",
    "list_classifications",
    "list_notifications",
    "list_verdicts",
    "nearest_historical_incidents",
    "save_classification",
    "save_historical_incident",
    "save_notification",
    "save_ticket",
    "save_verdict",
    "upsert_cluster",
]
