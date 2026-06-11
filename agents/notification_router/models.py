"""Input/output models for the Notification Router agent (RA-005).

The agent's *input* is a ``TriageVerdict`` produced upstream by RA-001
(Alert Triage). The *output* is a ``RoutingDecision`` — a structured
description of where the notification was routed and why, plus the
``ChatMessage`` that was (or would be) emitted through the chatops seam.

Why a separate decision object instead of just returning ``ChatMessage``:

- Tests can assert routing logic without touching the seam.
- ``audit_trace`` carries the reasoning steps so the verdict is
  explainable end-to-end (CLAUDE.md principle #6).
- Future variants (escalation ladders, multi-channel fan-out, suppression
  windows) can extend this model without breaking the chatops contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aiops.tools.chatops import DeliveryResult, Severity


class RoutingDecision(BaseModel):
    """Structured output of the Notification Router.

    The agent emits one of these per TriageVerdict. The ``message`` field is
    a serialized form of the ``ChatMessage`` (dict-shape) so this object
    survives JSON round-trips in evals and audit logs without depending on
    the dataclass directly.
    """

    model_config = ConfigDict(extra="forbid")

    chat_severity: Severity
    channel: str
    title: str
    body: str
    mentions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    """Logical actions taken — e.g. ['page_oncall', 'post_to_chat'].

    These are descriptive rather than prescriptive: every notification still
    flows through the single chatops seam. Downstream adapters (future
    PagerDuty integration, mobile push) inspect ``actions`` and ``chat_severity``
    to decide whether to escalate.
    """
    reason: str
    audit_trace: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    category_display: str | None = None
    """Human-readable failure sub-domain ("Payment Gateway"). Populated
    when the expertise-aware on-call lookup matched a category; ``None``
    when no match (no keywords, off-shift specialist, mock provider)."""


class RoutingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: RoutingDecision
    deliveries: dict[str, DeliveryResult] = Field(default_factory=dict)
