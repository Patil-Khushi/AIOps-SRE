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


class Severity(StrEnum):
    """How loud a notification should be. Adapters map this to their own levels."""

    INFO = "info"
    P3 = "p3"
    P2 = "p2"
    P1 = "p1"
    P0 = "p0"


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
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


def to_record(msg: ChatMessage) -> dict[str, Any]:
    """Serialize a ``ChatMessage`` to a JSON-friendly dict.

    StrEnum + isoformat() so the output is portable across consumers (the
    JSON audit log, the WebSocket adapter, future Slack/Teams adapters,
    log shippers, the eval harness) without a custom JSON encoder. Keeping
    this one function shared also guarantees every sink sees the same
    wire shape — no silent divergence between adapters.
    """
    return {
        "timestamp": msg.timestamp.isoformat(),
        "channel": msg.channel,
        "severity": str(msg.severity),
        "title": msg.title,
        "body": msg.body,
        "incident_id": msg.incident_id,
        "service": msg.service,
        "mentions": list(msg.mentions),
    }
