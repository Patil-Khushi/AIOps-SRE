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
    ExecutionRow,
    HistoricalIncidentRow,
    IncidentResolverRow,
    KBArticleRow,
    NotificationRow,
    RCAOutcomeRow,
    RCAResultRow,
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
        customer_facing=getattr(verdict, "customer_facing", False),
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
        "customer_facing": bool(getattr(row, "customer_facing", False)),
        "severity": row.severity,
        "confidence_score": row.confidence_score,
        "alert_summary": row.alert_summary,
        "assigned_team": row.assigned_team,
        "assigned_engineer": row.assigned_engineer,
        "recommended_runbook": row.recommended_runbook,
        "duplicate_alert_count": row.duplicate_alert_count,
        "status": row.status,
        "audit_metadata": {
            # _aware() stamps UTC so the ISO string carries an offset. Without it
            # SQLite returns a naive datetime, isoformat() omits the zone, and the
            # browser parses it as *local* time — showing a fresh incident hours
            # in the past for non-UTC users.
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
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


# ─── assignment history (sticky + load-aware on-call selection) ────────────


def find_last_assigned_engineer(service: str, *, window: timedelta) -> str | None:
    """Most recent non-null ``assigned_engineer`` for ``service`` in ``window``.

    The sticky-assignment source: an alert that re-fires for a service with
    an in-flight incident should page the engineer who already owns it, not
    whoever the fresh lookup would pick. Suppressed verdicts count — they
    name the incident owner just as well as Active ones do.
    """
    if not service:
        return None
    cutoff = datetime.now(UTC) - window
    stmt = (
        select(VerdictRow.assigned_engineer)
        .where(VerdictRow.affected_service == service)
        .where(VerdictRow.assigned_engineer.is_not(None))  # type: ignore[union-attr]
        .where(VerdictRow.created_at >= cutoff)  # type: ignore[arg-type]
        .order_by(VerdictRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    with _session() as s:
        engineer = s.exec(stmt).first()
    return engineer or None


def count_recent_assignments(window: timedelta) -> dict[str, int]:
    """Per-engineer verdict counts (keyed by engineer email) within ``window``.

    Backs the load-aware tie-break in ``aiops.state.oncall_repository`` —
    the "oncall_load table (v2)" its in-code TODO promised, served straight
    from the verdicts we already write. POC scale: the group-by runs
    unindexed over a bounded window; fine well past demo volume.
    """
    cutoff = datetime.now(UTC) - window
    stmt = (
        select(VerdictRow.assigned_engineer)
        .where(VerdictRow.assigned_engineer.is_not(None))  # type: ignore[union-attr]
        .where(VerdictRow.created_at >= cutoff)  # type: ignore[arg-type]
    )
    with _session() as s:
        engineers = s.exec(stmt).all()
    counts: dict[str, int] = {}
    for email in engineers:
        if email:
            counts[email] = counts.get(email, 0) + 1
    return counts


def clear_clusters_for_service(service: str) -> int:
    """Delete dedup clusters for one service. Returns rows removed.

    Scenario-reset hook: when an operator resets an injected failure, the
    incident is over by definition — the next inject of the same scenario
    must triage as a NEW incident (Active verdict → chatops emit), not get
    Suppressed against the previous run's still-warm cluster. Scoped to one
    service so resets don't break dedup for unrelated still-firing alerts.
    """
    if not service:
        return 0
    with _session() as s:
        rows = s.exec(select(ClusterRow).where(ClusterRow.service == service)).all()
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
        response_mode=getattr(decision, "response_mode", None),
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
        "response_mode": row.response_mode,
        "title": row.title,
        "body": row.body,
        "service": row.service,
        # ``actions`` is nullable (CHAT-3 #83 will populate); surface None as
        # [] so dashboard code doesn't need a null-check on every row.
        "actions": list(row.actions) if row.actions else [],
        "reason": row.reason,
        "audit_trace": list(row.audit_trace or []),
    }


# ─── incident resolvers (RA-005+006 institutional memory) ──────────────────


def save_incident_resolver(
    *,
    affected_service: str,
    resolver_handle: str,
    category: str | None = None,
    resolver_name: str | None = None,
    resolver_email: str | None = None,
    incident_id: str | None = None,
) -> int:
    """Record that ``resolver_handle`` helped resolve an incident on
    ``affected_service`` (optionally scoped to a failure sub-domain
    ``category``). Returns the row id.

    Written when a war room is marked ``resolved``. Callers should record one
    row per resolver (the SMEs who joined, or the on-call as a fallback). This
    is append-only history — the same engineer may accrue many rows over time;
    :func:`list_incident_resolvers` de-dups on read (most-recent-first)."""
    row = IncidentResolverRow(
        affected_service=affected_service,
        category=category,
        resolver_handle=resolver_handle,
        resolver_name=resolver_name,
        resolver_email=resolver_email,
        incident_id=incident_id,
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id or 0


def list_incident_resolvers(
    *,
    affected_service: str,
    category: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return the most recent distinct resolvers for a service, newest first.

    Matching (per the product decision): when ``category`` is given, return
    resolvers of incidents on ``affected_service`` **with that same failure
    sub-domain**; when ``category`` is ``None``, fall back to service-wide
    (any sub-domain). De-dups by ``resolver_handle`` keeping the most recent,
    and caps at ``limit`` so a war room isn't flooded with historical names."""
    stmt = (
        select(IncidentResolverRow)
        .where(IncidentResolverRow.affected_service == affected_service)
        .order_by(IncidentResolverRow.resolved_at.desc())  # type: ignore[attr-defined]
    )
    if category:
        stmt = stmt.where(IncidentResolverRow.category == category)
    with _session() as s:
        rows = s.exec(stmt).all()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        if r.resolver_handle in seen:
            continue
        seen.add(r.resolver_handle)
        out.append(
            {
                "resolver_handle": r.resolver_handle,
                "resolver_name": r.resolver_name,
                "resolver_email": r.resolver_email,
                "category": r.category,
                "incident_id": r.incident_id,
                "resolved_at": _aware(r.resolved_at).isoformat() if r.resolved_at else None,
            }
        )
        if len(out) >= limit:
            break
    return out


# ─── executions (PRS-002 Auto-Healer Lite output) ──────────────────────────


def save_execution(verdict: Any) -> int:
    """Persist one Auto-Healer Lite ``ExecutionVerdict``. Returns the row id.

    Called by ``agents.auto_healer_lite.execute`` after every attempt
    (REFUSED / BLOCKED / DRY_RUN_OK / EXECUTED / EXECUTION_FAILED) so
    the dashboard's history view + future historical-effectiveness feed
    to PRS-001 both have a single source of truth.

    The ``decision`` and ``audit_trace`` columns are JSON so this can
    accept whatever the upstream gate / executor emits without a schema
    migration each release. ``request_id`` is uniquely indexed — a
    second save with the same id raises an IntegrityError, which is the
    right behaviour because the request_id is generated per call.
    """
    audit = verdict.audit_metadata
    row = ExecutionRow(
        request_id=verdict.request_id,
        option_id=verdict.option_id,
        incident_id=getattr(verdict, "incident_id", None),
        affected_service=verdict.affected_service,
        status=str(verdict.status.value if hasattr(verdict.status, "value") else verdict.status),
        dry_run=bool(verdict.dry_run),
        tool_capability=verdict.tool_capability,
        tool_args=dict(verdict.tool_args or {}),
        tool_result=dict(verdict.tool_result) if verdict.tool_result else None,
        decision=verdict.decision.model_dump(mode="json")
        if hasattr(verdict.decision, "model_dump")
        else dict(verdict.decision or {}),
        operator=getattr(verdict, "operator", None),
        error=getattr(verdict, "error", None),
        rationale=verdict.rationale,
        decision_trace=list(getattr(audit, "decision_trace", []) or []),
        created_at=audit.created_at,
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def list_executions(
    *,
    limit: int = 50,
    affected_service: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first list of persisted executions. Filters by service and/or
    status. Returns plain dicts safe to serialize to JSON.
    """
    stmt = (
        select(ExecutionRow)
        .order_by(ExecutionRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    if affected_service:
        stmt = stmt.where(ExecutionRow.affected_service == affected_service)
    if status:
        stmt = stmt.where(ExecutionRow.status == status)
    with _session() as s:
        rows = s.exec(stmt).all()
    return [_execution_row_to_dict(r) for r in rows]


def _execution_row_to_dict(row: ExecutionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "option_id": row.option_id,
        "incident_id": row.incident_id,
        "affected_service": row.affected_service,
        "status": row.status,
        "dry_run": row.dry_run,
        "tool_capability": row.tool_capability,
        "tool_args": row.tool_args,
        "tool_result": row.tool_result,
        "decision": row.decision,
        "operator": row.operator,
        "error": row.error,
        "rationale": row.rationale,
        "decision_trace": row.decision_trace,
        "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
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


# ─── KB articles (PRS-007 Knowledge Synthesizer output) ─────────────────────


def save_kb_article(
    *,
    title: str,
    body: str,
    incident_id: str | None = None,
    summary: str = "",
    service: str = "",
    tags: list[str] | None = None,
    status: str = "pending_review",
    quality_score: float = 0.0,
    related_runbook_id: str | None = None,
    approval_id: str | None = None,
    approved_by: str | None = None,
    source: str = "PRS-007",
    embedding: list[float] | None = None,
    embedding_text: str = "",
    created_at: datetime | None = None,
    audit_metadata: dict[str, Any] | None = None,
) -> int:
    """Persist a KB article (draft or published). Returns the row id.

    ``body`` is expected to be already PII/secret-redacted by the caller —
    this layer does not redact. ``embedding`` should be L2-normalized so
    ``nearest_kb_articles`` can use a plain dot product.
    """
    now = datetime.now(UTC)
    row = KBArticleRow(
        incident_id=incident_id,
        title=title,
        summary=summary,
        body=body,
        service=service,
        tags=list(tags or []),
        status=status,
        quality_score=quality_score,
        related_runbook_id=related_runbook_id,
        approval_id=approval_id,
        approved_by=approved_by,
        source=source,
        embedding=list(embedding) if embedding is not None else [],
        embedding_text=embedding_text,
        created_at=created_at or now,
        updated_at=now,
        audit_metadata=audit_metadata or {},
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def get_kb_article(article_id: int) -> dict[str, Any] | None:
    with _session() as s:
        row = s.get(KBArticleRow, article_id)
    return _kb_row_to_dict(row) if row else None


def find_kb_by_incident_id(incident_id: str) -> dict[str, Any] | None:
    """Most recent KB article for ``incident_id``, or None. The synthesizer's
    idempotency guard: if this returns a row, the incident was already
    synthesized and we don't create a duplicate article.

    Empty ``incident_id`` is rejected so malformed inputs don't all collide
    on the same lookup (matches ``find_recent_verdict_by_alert_id``)."""
    if not incident_id:
        return None
    stmt = (
        select(KBArticleRow)
        .where(KBArticleRow.incident_id == incident_id)
        .order_by(KBArticleRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    with _session() as s:
        row = s.exec(stmt).first()
    return _kb_row_to_dict(row) if row else None


def list_kb_articles(
    *,
    limit: int = 50,
    status: str | None = None,
    service: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first list of KB articles, optionally filtered by status/service."""
    stmt = (
        select(KBArticleRow)
        .order_by(KBArticleRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    if status:
        stmt = stmt.where(KBArticleRow.status == status)
    if service:
        stmt = stmt.where(KBArticleRow.service == service)
    with _session() as s:
        rows = s.exec(stmt).all()
    return [_kb_row_to_dict(r) for r in rows]


def update_kb_status(
    article_id: int,
    status: str,
    *,
    approval_id: str | None = None,
    approved_by: str | None = None,
) -> dict[str, Any] | None:
    """Transition an article's review status (e.g. pending_review → published).

    Sets ``approval_id`` / ``approved_by`` when supplied (the HITL outcome) and
    always bumps ``updated_at``. Returns the updated article, or None if the id
    doesn't exist."""
    with _session() as s:
        row = s.get(KBArticleRow, article_id)
        if row is None:
            return None
        row.status = status
        if approval_id is not None:
            row.approval_id = approval_id
        if approved_by is not None:
            row.approved_by = approved_by
        row.updated_at = datetime.now(UTC)
        s.add(row)
        s.commit()
        s.refresh(row)
        return _kb_row_to_dict(row)


def nearest_kb_articles(
    *,
    embedding: list[float],
    k: int = 5,
    min_similarity: float = 0.6,
    statuses: set[str] | None = None,
    exclude_id: int | None = None,
) -> list[dict[str, Any]]:
    """Brute-force cosine nearest-K over KB-article embeddings — the shared
    backend for dedup ("is a near-identical article already here?") and the v0
    RAG retrieval other agents query.

    Requires ``embedding`` to be L2-normalized (so are the stored vectors), so
    the dot product is cosine similarity. ``statuses`` restricts the candidate
    pool (e.g. ``{"published", "pending_review"}`` to skip rejected drafts);
    ``exclude_id`` drops a row from the results (skip self on an update).

    Mirrors ``nearest_historical_incidents`` — same in-memory scan, fine up to
    a few thousand rows; the schema is ready to swap to pgvector when it isn't.
    """
    if not embedding:
        return []
    with _session() as s:
        rows = s.exec(select(KBArticleRow)).all()
    scored: list[tuple[float, KBArticleRow]] = []
    for r in rows:
        if exclude_id is not None and r.id == exclude_id:
            continue
        if statuses is not None and r.status not in statuses:
            continue
        v = r.embedding or []
        if len(v) != len(embedding):
            continue
        sim = 0.0
        for a, b in zip(embedding, v, strict=False):
            sim += a * b
        if sim >= min_similarity:
            scored.append((sim, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict[str, Any]] = []
    for sim, r in scored[:k]:
        d = _kb_row_to_dict(r)
        d["similarity"] = sim
        out.append(d)
    return out


def count_kb_articles() -> int:
    with _session() as s:
        rows = s.exec(select(KBArticleRow)).all()
    return len(rows)


def delete_all_kb_articles() -> int:
    """Wipe every KB article row. Eval/test hook; not a production path."""
    with _session() as s:
        rows = s.exec(select(KBArticleRow)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def tag_kb_article_source(article_id: int, source: str) -> dict[str, Any] | None:
    """Merge a ``source`` marker into a KB article's audit_metadata (e.g.
    ``ticket_only`` for watcher-synthesized articles with no RCA on record).
    Additive — does not touch the agent. Returns the updated article or None."""
    with _session() as s:
        row = s.get(KBArticleRow, article_id)
        if row is None:
            return None
        meta = dict(row.audit_metadata or {})
        meta["source"] = source
        row.audit_metadata = meta
        s.add(row)
        s.commit()
        s.refresh(row)
        return _kb_row_to_dict(row)


# ─── RCA results (keyed by incident id for the watcher / verifier) ──────────


def save_rca_result(
    *, incident_id: str, verdict: dict[str, Any], affected_service: str = ""
) -> int:
    """Persist an RCA verdict keyed by incident id. Returns the row id."""
    row = RCAResultRow(
        incident_id=incident_id,
        affected_service=affected_service or str(verdict.get("affected_service", "")),
        verdict=dict(verdict or {}),
    )
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def list_rca_results(
    *, limit: int = 200, exclude_incident_id: str | None = None
) -> list[dict[str, Any]]:
    """Every persisted RCA verdict, newest first — the corpus for the chat's
    read-only "similar past incidents" feature (agents/rca_agent/incident_rag.py).

    Deliberately a *different* corpus from the truth-file eval fixtures
    (aiops/tools/incident_history's embedding provider): these are incidents
    this deployment actually processed, not hand-authored test fixtures, so a
    chat answer citing one describes something that really happened here.
    ``exclude_incident_id`` keeps the current incident out of its own
    "similar incidents" list.
    """
    stmt = select(RCAResultRow).order_by(RCAResultRow.created_at.desc()).limit(max(1, limit))  # type: ignore[attr-defined]
    with _session() as s:
        rows = s.exec(stmt).all()
    return [
        {
            "id": row.id,
            "incident_id": row.incident_id,
            "affected_service": row.affected_service,
            "verdict": dict(row.verdict or {}),
            "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
        }
        for row in rows
        if row.incident_id != exclude_incident_id
    ]


def get_rca_result(incident_id: str) -> dict[str, Any] | None:
    """Most recent stored RCA verdict for ``incident_id``, or None."""
    if not incident_id:
        return None
    stmt = (
        select(RCAResultRow)
        .where(RCAResultRow.incident_id == incident_id)
        .order_by(RCAResultRow.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    with _session() as s:
        row = s.exec(stmt).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "affected_service": row.affected_service,
        "verdict": dict(row.verdict or {}),
        "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
    }


def delete_all_rca_results() -> int:
    """Eval/test hook."""
    with _session() as s:
        rows = s.exec(select(RCAResultRow)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


# ─── RCA outcomes (the only population historical memory may recall) ────────
#
# Kept as primitives in and primitives out. ``aiops/`` may not import ``agents/``
# (tests/test_layering.py, AST-checked), so this layer cannot take an ``RCAOutcome``
# model — the agent maps its own type onto these keyword arguments.

# Only these two lifecycle states may be recalled as a prior. Duplicated as a
# literal here rather than imported from the agent's ``MemoryStatus`` for the
# layering reason above; ``tests/test_rca_memory.py`` asserts the two agree, so the
# duplication cannot drift silently.
RECALLABLE_MEMORY_STATUSES: tuple[str, ...] = ("verified", "trusted")


def save_rca_outcome(
    *,
    incident_id: str,
    affected_service: str = "",
    predicted_root_cause: str = "",
    predicted_status: str = "",
    confidence: float = 0.0,
    selected_hypothesis_id: str | None = None,
    selected_hypothesis_class: str | None = None,
    action_key: str | None = None,
    human_decision: str = "not_requested",
    verification_result: str = "not_run",
    human_corrected_root_cause: str | None = None,
    memory_status: str = "new",
    signatures: list[str] | None = None,
    outcome: dict[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> int:
    """Persist one RCA outcome. Returns the row id.

    Writing a row does **not** make it recallable — ``memory_status`` governs that,
    and the caller is expected to pass ``new``/``unverified`` until a verifier has
    confirmed recovery. This function deliberately does not infer a status from
    ``verification_result``: promotion is a policy decision that belongs with the
    agent's lifecycle rules, not with the storage layer.
    """
    row = RCAOutcomeRow(
        incident_id=incident_id,
        affected_service=affected_service,
        predicted_root_cause=predicted_root_cause,
        predicted_status=predicted_status,
        confidence=float(confidence),
        selected_hypothesis_id=selected_hypothesis_id,
        selected_hypothesis_class=selected_hypothesis_class,
        action_key=action_key,
        human_decision=human_decision,
        verification_result=verification_result,
        human_corrected_root_cause=human_corrected_root_cause,
        memory_status=memory_status,
        signatures=list(signatures or []),
        outcome=dict(outcome or {}),
    )
    if recorded_at is not None:
        row.recorded_at = recorded_at
    with _session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        return int(row.id)  # type: ignore[arg-type]


def list_rca_outcomes(
    *,
    service: str | None = None,
    statuses: tuple[str, ...] | None = None,
    exclude_incident_ids: tuple[str, ...] = (),
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Outcomes newest-first, optionally filtered.

    ``statuses=None`` returns every state, which is what a lifecycle or audit view
    wants. A *recall* must pass ``RECALLABLE_MEMORY_STATUSES`` — that filtering is
    the caller's to request, so a future caller cannot get unverified rows by
    forgetting an argument and never noticing.

    ``exclude_incident_ids`` supports leave-one-out evaluation: scoring a scenario
    against memory that contains its own outcome measures nothing.
    """
    stmt = select(RCAOutcomeRow)
    if service:
        stmt = stmt.where(RCAOutcomeRow.affected_service == service)
    if statuses:
        stmt = stmt.where(RCAOutcomeRow.memory_status.in_(statuses))  # type: ignore[attr-defined]
    stmt = stmt.order_by(RCAOutcomeRow.recorded_at.desc()).limit(  # type: ignore[attr-defined]
        max(1, int(limit))
    )
    with _session() as s:
        rows = list(s.exec(stmt).all())
    excluded = {i for i in exclude_incident_ids if i}
    return [_rca_outcome_row_to_dict(r) for r in rows if r.incident_id not in excluded]


def get_rca_outcome(row_id: int) -> dict[str, Any] | None:
    with _session() as s:
        row = s.get(RCAOutcomeRow, row_id)
        return _rca_outcome_row_to_dict(row) if row is not None else None


def update_rca_outcome_memory_status(
    row_id: int, memory_status: str, *, superseded_by: str | None = None
) -> dict[str, Any] | None:
    """Advance (or retract) one outcome's lifecycle state.

    Retraction is an update, never a delete: ``invalidated`` knowledge is retained
    because deleting it destroys the evidence that it was ever used to reach a
    conclusion.
    """
    with _session() as s:
        row = s.get(RCAOutcomeRow, row_id)
        if row is None:
            return None
        row.memory_status = memory_status
        if superseded_by is not None:
            row.superseded_by = superseded_by
        s.add(row)
        s.commit()
        s.refresh(row)
        return _rca_outcome_row_to_dict(row)


def count_rca_outcomes(*, statuses: tuple[str, ...] | None = None) -> int:
    stmt = select(RCAOutcomeRow)
    if statuses:
        stmt = stmt.where(RCAOutcomeRow.memory_status.in_(statuses))  # type: ignore[attr-defined]
    with _session() as s:
        return len(list(s.exec(stmt).all()))


def delete_all_rca_outcomes() -> int:
    """Eval/test hook."""
    with _session() as s:
        rows = list(s.exec(select(RCAOutcomeRow)).all())
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def _rca_outcome_row_to_dict(row: RCAOutcomeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "affected_service": row.affected_service,
        "predicted_root_cause": row.predicted_root_cause,
        "predicted_status": row.predicted_status,
        "confidence": row.confidence,
        "selected_hypothesis_id": row.selected_hypothesis_id,
        "selected_hypothesis_class": row.selected_hypothesis_class,
        "action_key": row.action_key,
        "human_decision": row.human_decision,
        "verification_result": row.verification_result,
        "human_corrected_root_cause": row.human_corrected_root_cause,
        "memory_status": row.memory_status,
        "superseded_by": row.superseded_by,
        "signatures": list(row.signatures or []),
        "outcome": dict(row.outcome or {}),
        "recorded_at": _aware(row.recorded_at).isoformat() if row.recorded_at else None,
    }


def _kb_row_to_dict(row: KBArticleRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "title": row.title,
        "summary": row.summary,
        "body": row.body,
        "service": row.service,
        "tags": list(row.tags or []),
        "status": row.status,
        "quality_score": row.quality_score,
        "related_runbook_id": row.related_runbook_id,
        "approval_id": row.approval_id,
        "approved_by": row.approved_by,
        "source": row.source,
        "embedding": list(row.embedding or []),
        "embedding_text": row.embedding_text,
        "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
        "updated_at": _aware(row.updated_at).isoformat() if row.updated_at else None,
        "audit_metadata": dict(row.audit_metadata or {}),
    }


__all__ = [
    "average_classification_confidence",
    "count_classifications",
    "count_historical_incidents",
    "count_kb_articles",
    "count_notifications",
    "delete_all_clusters",
    "delete_all_historical_incidents",
    "delete_all_kb_articles",
    "delete_all_rca_results",
    "delete_live_historical_incidents",
    "evict_expired_clusters",
    "find_active_cluster",
    "find_kb_by_incident_id",
    "get_classification",
    "get_kb_article",
    "get_rca_result",
    "get_verdict",
    "list_active_clusters",
    "list_classifications",
    "list_kb_articles",
    "list_notifications",
    "list_rca_results",
    "list_verdicts",
    "nearest_historical_incidents",
    "nearest_kb_articles",
    "save_classification",
    "save_historical_incident",
    "save_kb_article",
    "save_notification",
    "save_rca_result",
    "save_ticket",
    "save_verdict",
    "tag_kb_article_source",
    "update_kb_status",
    "upsert_cluster",
]
