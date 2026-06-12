"""Input/output models for the Knowledge Synthesizer (PRS-007).

The agent consumes a *resolved incident bundle* (the triage verdict, the RCA
verdict, and optionally the classification / ticket / change records) and emits
a :class:`SynthesisResult` — a drafted postmortem, a runbook suggestion, and a
KB article persisted as ``pending_review`` for human approval.

Scoreability: the eval harness scores a flat ``expected`` dict against the
top-level keys of ``run()``'s output (see ``evals/scoring.py``). So
:class:`SynthesisResult` lifts the fields worth asserting (affected_service,
status, root_cause, dedup_action, runbook_mode, …) to the top level, with the
full structured objects nested underneath.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aiops.runbooks import ReviewStatus

# Re-export so callers can do ``from agents.knowledge_synthesizer.models import
# ReviewStatus`` without reaching into the runbooks seam. The KB review
# lifecycle is intentionally the same vocabulary as the runbook one.
__all__ = [
    "DedupDecision",
    "KBArticle",
    "Postmortem",
    "ReviewStatus",
    "RunbookSuggestion",
    "SynthesisInput",
    "SynthesisResult",
    "TimelineEntry",
]


class SynthesisInput(BaseModel):
    """Eval-harness contract for ``run(input)`` — the resolved incident bundle.

    Only ``triage_verdict`` and ``rca_verdict`` are required; the rest are
    additive context. ``extra="allow"`` so a richer bundle from the
    orchestrator doesn't break the contract.
    """

    model_config = ConfigDict(extra="allow")

    triage_verdict: dict[str, Any]
    rca_verdict: dict[str, Any]
    classification: dict[str, Any] | None = None
    ticket: dict[str, Any] | None = None
    # Deploy/config history around the incident. Optional + stubbed in v0 (no
    # change-record store exists yet) — folded into the postmortem when present.
    change_records: list[dict[str, Any]] = Field(default_factory=list)
    incident_id: str | None = None
    resolved_at: str | None = None
    scenario_id: str | None = None


class TimelineEntry(BaseModel):
    """One moment in the incident timeline. Assembled from the cross-agent
    ``audit_metadata.created_at`` timestamps — RCA's verdict carries no
    timeline field, so we reconstruct it rather than change RCA's contract."""

    model_config = ConfigDict(extra="forbid")

    ts: str | None = None
    event: str
    source_agent: str | None = None


class Postmortem(BaseModel):
    """Structured postmortem: what broke, root cause, timeline, fix, impact."""

    model_config = ConfigDict(extra="forbid")

    affected_service: str
    what_broke: str
    root_cause: str
    timeline: list[TimelineEntry] = Field(default_factory=list)
    fix: str
    impact: str
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.0)


class RunbookSuggestion(BaseModel):
    """A proposed new-or-updated runbook derived from the resolution steps.

    The body follows the locked runbook structure (symptoms → diagnosis →
    resolution → verification → rollback). It is *suggested*, not written —
    the runbook file is only persisted after a human approves (Checkpoint 5).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["new", "update"]
    target_id: str  # the runbook id to create or update
    title: str
    body_markdown: str


class KBArticle(BaseModel):
    """The KB article drafted from the postmortem (already redacted)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str | None = None
    title: str
    summary: str
    body: str  # redacted markdown
    service: str
    tags: list[str] = Field(default_factory=list)
    quality_score: float = Field(ge=0.0, le=1.0, default=0.0)
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    related_runbook_id: str | None = None


class DedupDecision(BaseModel):
    """Outcome of the dedup check against existing KB articles."""

    model_config = ConfigDict(extra="forbid")

    # create           — no near-duplicate; a new article was persisted.
    # duplicate         — a near-identical article exists; no new row created.
    # skip_idempotent   — this incident was already synthesized before.
    action: Literal["create", "duplicate", "skip_idempotent"]
    matched_article_id: int | None = None
    similarity: float = 0.0
    method: Literal["embedding", "signature", "incident_id"] = "signature"


class SynthesisResult(BaseModel):
    """Top-level result. Flat scoreable fields + nested detail objects."""

    model_config = ConfigDict(extra="forbid")

    # ── flat, scoreable ──
    incident_id: str | None
    affected_service: str
    status: ReviewStatus
    root_cause: str
    dedup_action: str
    runbook_mode: str
    related_runbook_id: str | None
    kb_article_id: int | None
    quality_score: float
    redaction_summary: str
    created_at: datetime
    # ── nested detail ──
    postmortem: Postmortem
    kb_article: KBArticle
    runbook_suggestion: RunbookSuggestion
    dedup: DedupDecision
