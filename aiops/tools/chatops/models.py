"""Canonical chat-message model — the standard ticket the chatops seam routes.

Why this exists:
    Solution Design §2 (vendor-neutral by default). Agents produce a single
    ChatMessage shape and never know whether it ends up in the demo
    dashboard, a JSON audit log, Slack, Teams, or PagerDuty. Adapters
    translate this model to the wire format of each sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from pydantic import BaseModel


class Severity(StrEnum):
    """How loud a notification should be. Adapters map this to their own levels."""

    INFO = "info"
    P3 = "p3"
    P2 = "p2"
    P1 = "p1"
    P0 = "p0"


@dataclass
class InteractivePrompt:
    """Vendor-neutral interactive payload — currently used only by the HITL
    approval flow (issue #77).

    Adapters that support buttons / actions (Slack, the dashboard WebSocket)
    render approve/deny controls keyed by ``approval_id``.  Adapters that
    don't (the JSONL audit log) write the payload as data and treat the
    message as informational.

    ``prompt_kind`` exists so future interactive flows (e.g. runbook param
    confirmation, knowledge-publish review) can reuse the seam without
    overloading the approval semantics.
    """

    approval_id: str
    action: str
    expires_at: datetime
    prompt_kind: str = "hitl_approval"


@dataclass
class ChatMessage:
    """A single notification routed through the chatops seam.

    Fields are vendor-neutral on purpose. Each adapter picks the subset it
    needs and ignores the rest.
    """

    channel: str
    severity: Severity
    title: str
    body: str = ""
    incident_id: str | None = None
    service: str | None = None
    mentions: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    """Routing intents the agent attached (e.g. ``"page_oncall"``,
    ``"post_to_chat"``). Adapters filter on these to decide whether they
    should act on a given message. CHAT-5 (#85): the PagerDuty adapter
    only fires when ``"page_oncall"`` is present; CHAT-1 Slack posts
    everything. Keeps routing policy in RA-005 and out of adapters.
    """
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    interactive: InteractivePrompt | None = None


@dataclass
class DeliveryResult(BaseModel):
    adapter: str
    ok: bool
    error: str | None = None
    latency_ms: int | None = None


def to_record(msg: ChatMessage) -> dict[str, Any]:
    """Serialize a ``ChatMessage`` to a JSON-friendly dict.

    StrEnum + isoformat() so the output is portable across consumers (the
    JSON audit log, the WebSocket adapter, future Slack/Teams adapters,
    log shippers, the eval harness) without a custom JSON encoder. Keeping
    this one function shared also guarantees every sink sees the same
    wire shape — no silent divergence between adapters.
    """
    interactive: dict[str, Any] | None = None
    if msg.interactive is not None:
        interactive = {
            "approval_id": msg.interactive.approval_id,
            "action": msg.interactive.action,
            "expires_at": msg.interactive.expires_at.isoformat(),
            "prompt_kind": msg.interactive.prompt_kind,
        }
    return {
        "timestamp": msg.timestamp.isoformat(),
        "channel": msg.channel,
        "severity": str(msg.severity),
        "title": msg.title,
        "body": msg.body,
        "incident_id": msg.incident_id,
        "service": msg.service,
        "mentions": list(msg.mentions),
        "actions": list(msg.actions),
        "interactive": interactive,
    }
