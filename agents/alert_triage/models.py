"""Input/output Pydantic models for the Alert Triage agent (RA-001).

Wire shapes match the canonical Reactive-Active alert→incident contract:

- ``Alert``           — normalized incoming alert from any monitoring source
- ``TriageVerdict``   — structured verdict the agent emits (matches the
                        catalog's documented output JSON shape)
- ``AuditMetadata``   — provenance block carried in every verdict so the
                        decision is explainable end-to-end
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["Sev-1", "Sev-2", "Sev-3", "Sev-4"]
Status = Literal["Active", "Suppressed"]


class Alert(BaseModel):
    """Canonical alert shape used internally by the agent.

    Source-specific payloads (Datadog, Prometheus Alertmanager, CloudWatch)
    get coerced to this shape by ``aiops.tools.alerts.*`` adapters in batch 2.
    ``extra="allow"`` so source payloads can carry their native fields
    alongside the canonical ones without losing data.
    """

    model_config = ConfigDict(extra="allow")

    alert_id: str
    service: str
    metric: str
    value: float
    timestamp: datetime
    source: str = "unknown"

    # Optional fields some sources already provide
    severity_hint: str | None = None
    threshold: float | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_timestamp(cls, v: Any) -> datetime:
        """Accept ISO 8601 strings (incl. trailing ``Z``) and datetimes."""
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, str):
            normalized = v.replace("Z", "+00:00") if v.endswith("Z") else v
            return datetime.fromisoformat(normalized)
        raise TypeError(f"Unsupported timestamp type: {type(v).__name__}")

    @field_validator("alert_id", "service", "metric", mode="before")
    @classmethod
    def _require_nonempty_identifier(cls, v: Any) -> Any:
        """Strip surrounding whitespace and reject empty / whitespace-only
        identifiers. An empty service silently broke PromQL interpolation
        (it would query ``service_name=""``, get no rows, and the agent
        would proceed with empty context). Failing at construction makes the
        problem visible at the boundary."""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("must not be empty or whitespace-only")
            return stripped
        return v

    @field_validator("value", "threshold", mode="after")
    @classmethod
    def _must_be_finite(cls, v: float | None) -> float | None:
        """Reject NaN and ±Inf. They flow through the rule-based severity
        comparisons as silent ``False`` (NaN comparisons), so a malformed
        upstream payload would fall through to the LLM consult with no
        warning. Failing at construction surfaces it."""
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError(f"must be a finite number, got {v!r}")
        return v

    def cluster_key(self) -> str:
        """Stable hash used by step-4 dedup to group duplicate alerts.

        Two alerts collide on this key when they share (service, metric,
        label-subset). Embedding similarity (batch 2) layers on top of this
        key to catch near-duplicates that differ only in punctuation/wording.
        """
        canonical = json.dumps(
            {
                "service": self.service.lower(),
                "metric": self.metric.lower(),
                "labels": dict(sorted(self.labels.items())),
            },
            sort_keys=True,
        )
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


class AuditMetadata(BaseModel):
    """Provenance carried in every verdict.

    ``decision_trace`` is appended by each reasoning stage so the verdict
    explains itself without re-running the agent (CLAUDE.md principle #6,
    closed-loop learning).
    """

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "RA-001"
    source_alerts: list[str] = Field(default_factory=list)
    decision_trace: list[str] = Field(default_factory=list)


class TriageVerdict(BaseModel):
    """Structured output of the Alert Triage agent.

    Field names + JSON shape match the canonical Reactive-Active output schema.
    ``incident_id`` is populated by the downstream Auto-Ticketing agent — at
    Alert Triage's boundary it's always ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: str | None = None
    affected_service: str
    severity: Severity
    confidence_score: float = Field(ge=0.0, le=1.0)
    alert_summary: str
    assigned_team: str
    assigned_engineer: str | None = None
    recommended_runbook: str | None = None
    # Counts DELIVERIES of this alert into the agent — i.e. how many times
    # the source has refired the same condition. NOT the same as the number
    # of distinct alert_ids in the cluster; that's ``len(source_alerts)`` on
    # the audit metadata. Transport-layer duplicates (network retries within
    # the agent's idempotency window) do not increment this — they are
    # short-circuited before reaching dedup.
    duplicate_alert_count: int = Field(default=1, ge=1)
    status: Status = "Active"
    audit_metadata: AuditMetadata
