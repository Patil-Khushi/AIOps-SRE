"""Input/output Pydantic models for the RCA Agent (PRS-008).

The agent consumes a ``TriageVerdict`` (RA-001) plus an optional ``scenario_id``
hint, and emits an ``RCAVerdict`` with a ranked list of fix steps. Every fix
step carries ``blast_radius`` and ``rollback`` and is tagged
``requires_hitl=true`` — the platform-level HITL gate enforces approval at
the action boundary downstream.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BlastRadius(StrEnum):
    """Reversibility scale for a fix step.

    Catalog mapping:
        low    — one resource / one flag; instant rollback (e.g. feature-flag flip).
        medium — namespace-scoped (single-namespace deploy rollback, scale, restart).
        high   — cluster-wide, multi-service, or data-plane state (avoid in v0).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FixActionType(StrEnum):
    """How — and whether — the platform executor can carry out a fix step.

    This is the machine-readable counterpart to the human-readable
    ``description``. It exists so the platform executor (``aiops.tools.
    rca_remediation``) can *follow the RCA step* directly, instead of a UI
    re-deriving the action from a hardcoded service→flag map. The agent only
    annotates the step; execution still goes through the platform HITL gate
    (CLAUDE.md #3).

    Values:
        set_flag        — flip a flagd feature flag (``flag`` + ``variant``
                          required). The one action v0's executor implements.
        rollback_deploy — roll back a Helm/K8s deploy. Recognised but no
                          executor wired in v0 — surfaces as a manual step.
        manual          — no automated executor; a human SRE performs it.
                          Default for any un-annotated step.
    """

    SET_FLAG = "set_flag"
    ROLLBACK_DEPLOY = "rollback_deploy"
    MANUAL = "manual"


class RankedFixStep(BaseModel):
    """One ranked, reversible fix step.

    v0 invariant: ``requires_hitl`` is always ``True`` — the catalog marks
    every RCA fix step as Required-HITL (Solution Design slide 10). The
    platform gate (``aiops.policy``) enforces this at the action boundary; the
    agent does not self-gate. The ``Literal[True]`` type on the field makes
    the invariant uncircumventable at deserialization — an LLM-supplied
    ``"requires_hitl": false`` is rejected by pydantic before any caller can
    see it, even if a future caller skips ``_coerce_verdict`` and passes raw
    LLM JSON straight to ``RCAVerdict.model_validate``.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    blast_radius: BlastRadius
    rollback: str = Field(min_length=1)
    requires_hitl: Literal[True] = True
    # Machine-readable action the platform executor follows. Defaults to a
    # non-executable ``manual`` step, so the prose ``description`` is the
    # source of truth until the agent explicitly annotates an executable
    # action.
    action_type: FixActionType = FixActionType.MANUAL
    # Populated only for ``action_type == set_flag``: the flagd flag to flip
    # and the target variant (almost always "off" to disable an injected
    # failure). A ``None`` flag on a set_flag step means "couldn't resolve a
    # flag" — the executor treats it as non-executable.
    flag: str | None = None
    variant: str = "off"


class RCAAuditMetadata(BaseModel):
    """Provenance carried in every verdict. Mirrors the alert-triage shape so
    the dashboard's decision-trace renderer works against both."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "PRS-008"
    decision_trace: list[str] = Field(default_factory=list)


class RCAInput(BaseModel):
    """Eval-harness contract for ``run(input)``.

    ``triage_verdict`` is the dict-form RA-001 output; ``scenario_id`` is an
    optional hint the failure-injection runner sets so the agent can short-
    circuit to a deterministic verdict when no LLM is configured (CI path).
    """

    model_config = ConfigDict(extra="allow")

    triage_verdict: dict[str, Any]
    scenario_id: str | None = None
    # Optional upstream evidence pack from the Log Correlation agent (RA-007),
    # in its ``CorrelationResult`` dict form. When present, ``analyze`` folds
    # the suspect components, top signatures, and evidence summary into the
    # reasoning prompt. Optional + additive — RCA's behavior is unchanged when
    # it is omitted, so the contract stays backward-compatible.
    correlation: dict[str, Any] | None = None


class RCAVerdict(BaseModel):
    """Structured output of the RCA Agent.

    Wire shape locked by [DEMO_PLAN.md](../../DEMO_PLAN.md) v0:
    ``root_cause``, ``ranked_fix_steps``, ``confidence_score`` are the three
    fields the dashboard renders + the eval harness scores against. Order of
    ``ranked_fix_steps`` is semantically meaningful — index 0 is the highest-
    confidence remediation.
    """

    model_config = ConfigDict(extra="forbid")

    affected_service: str
    root_cause: str = Field(min_length=1)
    ranked_fix_steps: list[RankedFixStep] = Field(min_length=1)
    confidence_score: float = Field(ge=0.0, le=1.0)
    audit_metadata: RCAAuditMetadata
