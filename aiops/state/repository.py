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
    ClusterRow,
    NotificationRow,
    TicketRow,
    VerdictRow,
)


def _session() -> Session:
    return Session(get_engine())


# ─── verdicts ──────────────────────────────────────────────────────────────


def save_verdict(verdict: Any, cluster_key: str) -> int:
    """Persist a triage verdict. Accepts the agent's Pydantic ``TriageVerdict``
    and the cluster_key it was assigned to. Returns the row id."""
    audit = verdict.audit_metadata
    row = VerdictRow(
        cluster_key=cluster_key,
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
) -> dict[str, Any]:
    """Append ``alert_id`` to the cluster (creating it on first sighting).
    Returns the up-to-date cluster as a dict."""
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
            )
            s.add(row)
        else:
            existing = list(row.source_alerts or [])
            if alert_id not in existing:
                existing.append(alert_id)
            row.source_alerts = existing
            row.alert_count = len(existing)
            row.last_seen = seen_at
            s.add(row)
        s.commit()
        s.refresh(row)
        return _cluster_row_to_dict(row)


def list_active_clusters(window: timedelta) -> list[dict[str, Any]]:
    """All clusters whose last_seen is within ``window``. Used by the
    embedding dedup path on warm restart to know which cluster_keys to seed."""
    cutoff = datetime.now(UTC) - window
    with _session() as s:
        rows = s.exec(
            select(ClusterRow).where(ClusterRow.last_seen >= cutoff)
        ).all()
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


def save_notification(
    *,
    verdict_id: int,
    channel: str,
    target: str,
    status: str = "sent",
    detail: dict[str, Any] | None = None,
) -> int:
    row = NotificationRow(
        verdict_id=verdict_id,
        channel=channel,
        target=target,
        status=status,
        detail=detail or {},
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


__all__ = [
    "delete_all_clusters",
    "evict_expired_clusters",
    "find_active_cluster",
    "get_verdict",
    "list_active_clusters",
    "list_verdicts",
    "save_notification",
    "save_ticket",
    "save_verdict",
    "upsert_cluster",
]
