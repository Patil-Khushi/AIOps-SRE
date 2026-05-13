"""Input/output Pydantic models for the Incident Classifier agent (RA-002).

Contract:
- ``ClassificationInput`` carries both the original ``Alert`` (raw signal,
  needed for embedding) and the upstream ``TriageVerdict`` from RA-001 (LLM-
  cleaned context). The TriageVerdict is the documented seam between the two
  agents (CLAUDE.md principle #2: agents couple only through declared
  input/output schemas).
- ``Classification`` is what this agent emits. Fields that the classifier
  fills via its own CMDB lookups (routing_team, on_call_engineer,
  recommended_runbook, dependencies) live here so the verdict is
  self-contained — downstream agents do not have to read both objects to
  route the incident.
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
    """Input to ``classify``. Carries the original alert + the RA-001 verdict.

    Why both? The raw ``Alert`` has technical keywords (metric names, label
    pairs, annotation text) that drive the embedding's signal. The
    ``TriageVerdict`` carries the LLM-cleaned ``alert_summary`` that's good
    natural-language input for the embedding plus the upstream severity
    classification and dedup signal."""

    model_config = ConfigDict(extra="forbid")

    alert: Alert
    triage_verdict: TriageVerdict


class AuditMetadata(BaseModel):
    """Provenance carried in every classification.

    ``decision_trace`` is appended at each reasoning stage so the verdict
    explains itself without re-running the agent (CLAUDE.md principle #6).
    ``similar_incidents`` is a debug snapshot of what the vector store
    returned, so a reviewer can see why the agent picked the type it did."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "RA-002"
    decision_trace: list[str] = Field(default_factory=list)
    similar_incidents: list[dict[str, Any]] = Field(default_factory=list)


class Classification(BaseModel):
    """Output of the Incident Classifier.

    Severity is intentionally *not* on this model — it lives on the upstream
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
