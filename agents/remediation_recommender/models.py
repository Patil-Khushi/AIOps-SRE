"""Input / output Pydantic models for the Remediation Recommender (PRS-001).

The agent sits **between** the RCA Agent (PRS-008) and the Auto-Healer
(PRS-002 / ``auto_healer_lite``). It consumes an :class:`RCAVerdict` (the
upstream root-cause + ranked fix steps) plus the original triage context,
and emits a :class:`RemediationVerdict` containing a *ranked decision set*
of remediation options. Each option carries enough metadata (blast radius,
rollback, MTTR estimate, tool capability + args) for the operator to pick
one and for the platform executor to safely act on the chosen option once
the HITL gate clears.

Why a separate verdict shape from ``RCAVerdict``:

- RCA produces fix steps *for the diagnosed cause*. Remediation Recommender
  produces operator-facing **options** — including alternative
  interventions that don't directly address the cause but mitigate the
  symptom (rate-limit, fail-over, circuit-breaker).
- Each option carries a numeric ``blast_radius_score`` (1..5) so the UI
  can sort visually without re-deriving from the enum.
- Each option declares its **tool capability + args** so the chosen one
  flows straight into Auto-Healer through the existing tool seam — no
  re-derivation of "how to execute this".
- The agent declares an autonomy level (``recommendation``) separate from
  the execution gate. Recommendation is **Optional HITL** (an operator
  *may* skip it and approve the auto-selected top option). Execution is
  **Required HITL** — gated at the platform layer when Auto-Healer calls
  the tool. Catalog reference: Solution Design slide 10.

CLAUDE.md non-negotiables this model preserves:
- #2 modular contract: ``RemediationInput`` / ``RemediationVerdict`` are
  the stable interface; Day-1 stub and future LLM-driven v1 share them.
- #3 HITL is platform-enforced. ``requires_hitl: Literal[True]`` on every
  ``RemediationOption`` and on the verdict itself makes the invariant
  uncircumventable at deserialization. The agent does NOT gate; the
  platform does.
- #5 safe autonomy primitives. Every option ships with a ``rollback``
  string and a ``rollback_tested`` flag so the executor can refuse to
  fire an option whose reverse hasn't been verified.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BlastRadius(StrEnum):
    """Reversibility scale for a remediation option.

    Mirrors :class:`agents.rca_agent.models.BlastRadius` so options
    derived from RCA fix-steps can pass the enum through unchanged.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionType(StrEnum):
    """Machine-readable executor hint.

    The Day-1 stub recognises ``set_flag`` and ``rollback_deploy`` (mirrors
    RCA) plus three options the recommender adds on its own — ``scale``,
    ``restart``, and ``circuit_breaker``. ``manual`` is the catch-all for
    anything the executor can't carry out automatically; Auto-Healer
    surfaces those as instructions rather than firing them.

    A future v1 may extend this; consumers MUST tolerate unknown values
    (treat as ``manual``) to keep the agent forward-compatible.
    """

    SET_FLAG = "set_flag"
    ROLLBACK_DEPLOY = "rollback_deploy"
    SCALE = "scale"
    RESTART = "restart"
    CIRCUIT_BREAKER = "circuit_breaker"
    MANUAL = "manual"


class OptionSource(StrEnum):
    """Where this option came from — for audit + future learning signal.

    - ``rca_fix_step``    — derived 1:1 from one of ``RCAVerdict.ranked_fix_steps``
    - ``playbook_pattern`` — added from the recommender's own catalog
                              (symptom-driven mitigations RCA didn't propose)
    - ``operator_seeded`` — manually added (e.g. a runbook author marked a
                              "preferred" option for this service / category)
    """

    RCA_FIX_STEP = "rca_fix_step"
    PLAYBOOK_PATTERN = "playbook_pattern"
    OPERATOR_SEEDED = "operator_seeded"


_BLAST_RADIUS_SCORE: dict[BlastRadius, int] = {
    BlastRadius.LOW: 1,
    BlastRadius.MEDIUM: 3,
    BlastRadius.HIGH: 5,
}


def blast_radius_score(radius: BlastRadius) -> int:
    """1-5 numeric score (lower = safer). Stable across enum value strings.

    Exposed for callers that want to re-sort the options client-side
    without re-deriving from the enum.
    """
    return _BLAST_RADIUS_SCORE[radius]


class RemediationOption(BaseModel):
    """One ranked, reversible remediation option.

    ``requires_hitl`` is invariantly ``True`` — the platform HITL gate
    enforces approval before Auto-Healer actually fires the option's
    tool capability. The ``Literal[True]`` type makes the invariant
    uncircumventable at deserialization (same defensive pattern as
    :class:`agents.rca_agent.models.RankedFixStep`).
    """

    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    action_type: ActionType = ActionType.MANUAL
    blast_radius: BlastRadius
    blast_radius_score: int = Field(ge=1, le=5)
    rollback: str = Field(min_length=1)
    rollback_tested: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    estimated_mttr_minutes: int = Field(ge=0)
    requires_hitl: Literal[True] = True
    rationale: str = Field(min_length=1)
    # Tool seam handoff: what capability would execute this, and with
    # what args. Populated only when ``action_type`` is automatable;
    # ``manual`` options leave these as None / {}.
    tool_capability: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    source: OptionSource = OptionSource.RCA_FIX_STEP


class RecoAuditMetadata(BaseModel):
    """Provenance carried in every verdict.

    ``created_by`` defaults to the catalog ID so dashboard / audit lines
    show which agent produced the recommendation.
    """

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "PRS-001"
    decision_trace: list[str] = Field(default_factory=list)


Environment = Literal["production", "staging", "dev"]


class RemediationInput(BaseModel):
    """Eval-harness contract for ``run(input)``.

    The agent accepts dict-form upstream verdicts because eval cases are
    written as JSON. The ``triage_verdict`` is optional — RA-001 may not
    have run for cases the operator is constructing manually — but the
    ``rca_verdict`` is required because PRS-001's whole purpose is to
    rank remediation options for a diagnosed cause.
    """

    model_config = ConfigDict(extra="allow")

    rca_verdict: dict[str, Any]
    triage_verdict: dict[str, Any] | None = None
    environment: Environment = "production"
    # Future v1: operator preferences (prefer_safe, prefer_cost_optimised,
    # prefer_fast). Day-1 stub honours ``prefer_safe`` only when set.
    operator_preferences: dict[str, Any] = Field(default_factory=dict)


class RemediationVerdict(BaseModel):
    """Ranked decision set + auto-pick hint.

    ``options`` is sorted by a composite score (lower blast_radius +
    higher confidence + closer match to operator preference = higher
    rank). Index 0 is the recommender's top pick.

    ``auto_pick_eligible`` is ``False`` in Day-1 — surfacing the
    recommendation always requires human approval before Auto-Healer
    can act. A future v1 may set it ``True`` for blast_radius=low +
    confidence>0.9 + rollback_tested options where the platform policy
    explicitly allows pre-approved auto-pick.
    """

    model_config = ConfigDict(extra="forbid")

    affected_service: str = Field(min_length=1)
    incident_summary: str = Field(min_length=1)
    options: list[RemediationOption] = Field(min_length=1)
    recommended_option_id: str = Field(min_length=1)
    auto_pick_eligible: Literal[False] = False
    confidence_score: float = Field(ge=0.0, le=1.0)
    requires_hitl: Literal[True] = True
    rationale: str = Field(min_length=1)
    audit_metadata: RecoAuditMetadata


__all__ = [
    "ActionType",
    "BlastRadius",
    "Environment",
    "OptionSource",
    "RecoAuditMetadata",
    "RemediationInput",
    "RemediationOption",
    "RemediationVerdict",
    "blast_radius_score",
]
