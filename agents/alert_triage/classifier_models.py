"""Classification models for the Alert Triage agent (incident-classification half).

Alert Triage does two things on one alert: it produces a ``TriageVerdict``
(severity / ownership / summary — see ``models.py``) and then classifies the
incident into one of five types. The classification contracts live here, in a
separate module from ``models.py``, only to avoid a name clash: both halves
define an ``AuditMetadata`` provenance block with different fields.

Contract:
- ``ClassificationInput`` carries both the original ``Alert`` (raw signal,
  needed for embedding) and the ``TriageVerdict`` produced earlier in the same
  agent run (LLM-cleaned context).
- ``Classification`` is what the classification step emits. Fields it fills via
  its own CMDB lookups (routing_team, on_call_engineer, recommended_runbook,
  dependencies) live here so the classification is self-contained.
- ``CombinedResult`` staples the verdict + classification together for the
  agent's one-shot ``triage_and_classify`` entry point.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.alert_triage.models import Alert, TriageVerdict

IncidentType = Literal[
    "infrastructure",
    "application",
    "network",
    "external_dependency",
    "change_related",
]


class ClassificationInput(BaseModel):
    """Input to the classification step. Carries the original alert + the
    triage verdict produced earlier in the same agent run.

    Why both? The raw ``Alert`` has technical keywords (metric names, label
    pairs, annotation text) that drive the embedding's signal. The
    ``TriageVerdict`` carries the LLM-cleaned ``alert_summary`` that's good
    natural-language input for the embedding plus the severity classification
    and dedup signal."""

    model_config = ConfigDict(extra="forbid")

    alert: Alert
    triage_verdict: TriageVerdict


class AuditMetadata(BaseModel):
    """Provenance carried in every classification.

    ``decision_trace`` is appended at each reasoning stage so the classification
    explains itself without re-running the agent (CLAUDE.md principle #6).
    ``similar_incidents`` is a debug snapshot of what the vector store returned,
    so a reviewer can see why the agent picked the type it did."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "RA-002"
    decision_trace: list[str] = Field(default_factory=list)
    similar_incidents: list[dict[str, Any]] = Field(default_factory=list)


class Classification(BaseModel):
    """Output of the incident-classification step.

    Severity is intentionally *not* on this model — it lives on the
    ``TriageVerdict`` and downstream consumers read both. Having two severity
    fields that can disagree is a footgun, not a feature.
    """

    model_config = ConfigDict(extra="forbid")

    incident_type: IncidentType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    tags: list[str] = Field(default_factory=list)
    probable_root_cause: str
    routing_team: str
    on_call_engineer: str | None = None
    recommended_runbook: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    similar_incident_ids: list[str] = Field(default_factory=list)
    audit_metadata: AuditMetadata


class CombinedResult(BaseModel):
    """Output of ``triage_and_classify`` — the agent's one-shot entry point.

    ``verdict`` is exactly what the triage step produced; ``classification`` is
    exactly what the classification step produced when fed that verdict. The
    combined entry point adds no fields to either — it runs them in sequence and
    staples the results together, so each half is identical to what the
    individual step would have emitted for the same input.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    affected_service: str
    verdict: TriageVerdict
    classification: Classification
    # The ``verdicts.id`` the triage step persisted, or ``None`` when
    # persistence failed (logged, non-fatal). Mirrors ``triage``'s second
    # return value.
    verdict_id: int | None = None
