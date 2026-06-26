"""Input/output models for the Incident Commander agent (RA-008, SRE).

RA-008 coordinates Sev-1/Sev-2 incident response. It does not introduce a new
incident contract — it consumes the Reactive-Active flow's output
(``aiops.runtime.orchestrator.ReactiveFlowResult``) plus the RCA verdict
(``agents.rca_agent``), and emits a coordination artifact:

- ``timeline``         — the running incident timeline the IC scribes.
- ``postmortem_seed``  — a facts-only postmortem skeleton pre-filled from the
                         verdict / classification / ticket / RCA.
- ``handoff_requested``— whether a human-IC handoff was posted to chatops.

Everything is plain JSON-serializable data: RA-008 v0 takes no destructive
action, so there is no executable payload here. The catalog's status-page sync
and timed comms-cadence are deferred (no seam exists yet); see the agent
docstring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TimelineEntry(BaseModel):
    """One line in the incident timeline the IC scribes.

    ``stage`` is a short machine-friendly label (``triage``, ``rca``,
    ``handoff``); ``detail`` is the human-readable note. Order is meaningful —
    entries are appended as the flow progresses.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    stage: str
    detail: str


class IncidentMetrics(BaseModel):
    """Derived MTTA/MTTR-style response durations, all measured from detection
    (the alert's own timestamp = T0, the cheat-sheet's MTTD anchor).

    A field is ``None`` when the stage did not run on this incident (e.g.
    ``time_to_handoff_seconds`` on a non-engaged Sev-3/4). Durations are in
    seconds and clamped at 0 so clock skew between agents can never produce a
    negative time on the postmortem.
    """

    model_config = ConfigDict(extra="forbid")

    detected_at: datetime
    time_to_triage_seconds: float | None = None
    time_to_notify_seconds: float | None = None  # detect → on-call paged (MTTA)
    time_to_handoff_seconds: float | None = None
    total_coordination_seconds: float | None = None


class PostmortemSeed(BaseModel):
    """Facts-only postmortem skeleton (catalog: "postmortem template pre-filled
    with facts"). RA-008 fills the *facts* it already has from the pipeline; the
    narrative / action-items are left for a human (or a later Knowledge
    Synthesizer agent) to complete.
    """

    model_config = ConfigDict(extra="forbid")

    affected_service: str
    severity: str
    incident_summary: str
    incident_type: str | None = None
    ticket_id: str | None = None
    root_cause: str | None = None
    confidence_score: float | None = None
    ranked_fix_steps: list[dict[str, Any]] = Field(default_factory=list)
    contributing_signals: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    metrics: IncidentMetrics | None = None


class ICAuditMetadata(BaseModel):
    """Provenance for the coordination decision. Mirrors the other agents'
    audit shape so the dashboard's decision-trace renderer works unchanged."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "RA-008"
    decision_trace: list[str] = Field(default_factory=list)


class IncidentCommandResult(BaseModel):
    """Output of ``command``.

    ``engaged`` is the headline: ``True`` only for Sev-1/Sev-2, where RA-008
    runs RCA, posts comms, seeds the postmortem, and requests a human-IC
    handoff. For lower severities the reactive pipeline still ran (``reactive``
    is populated) but coordination is a no-op (``rca``/``postmortem_seed`` are
    ``None``).

    ``reactive`` is the orchestrator's ``to_api_dict()`` (the same shape
    ``POST /api/triage`` returns) and ``rca`` is the RCA verdict dump — both
    carried as plain dicts so this result serializes cleanly for the eval
    harness and the HTTP boundary without re-validating nested models.
    """

    model_config = ConfigDict(extra="forbid")

    engaged: bool
    severity: str
    affected_service: str
    reactive: dict[str, Any]
    rca: dict[str, Any] | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    metrics: IncidentMetrics | None = None
    postmortem_seed: PostmortemSeed | None = None
    handoff_requested: bool = False
    audit_metadata: ICAuditMetadata
