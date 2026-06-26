"""Input/output models for the Notification Assembler agent (RA-005+006).

This agent merges the former Notification Router (RA-005) and War-Room
Assembler (RA-006) into one unit. Its *input* is a ``TriageVerdict`` from
RA-001 (Alert Triage), enriched with an ``incident_id`` by RA-003
(Auto-Ticketing). Its *output* is a single notification — a ``RoutingDecision``
describing where the one chatops message landed — plus, for severe incidents
(Sev-1/Sev-2), an optional ``WarRoomAssembly`` whose join link is folded into
that same message.

Why one combined agent instead of two:

- An operator gets exactly **one** notification per incident — the routing
  message *with* the war-room link inline — instead of two separate posts.
- The war room is only stood up for Sev-1/Sev-2 (unchanged threshold); lower
  severities get the plain notification and ``war_room`` is ``None``.

The sub-models (``RoutingDecision`` / ``WarRoomAssembly`` and friends) keep
their original shapes so persistence, the dashboard, and the eval harness see
a stable contract — only the *agent* merged, not the data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aiops.tools.chatops import DeliveryResult, Severity

# ─── Routing (was RA-005 Notification Router) ──────────────────────────────


class RoutingDecision(BaseModel):
    """Structured description of where the one notification was routed and why.

    The ``message`` content is serialized into ``body`` so this object survives
    JSON round-trips in evals and audit logs without depending on the chatops
    dataclass directly. ``audit_trace`` carries the reasoning steps so the
    decision is explainable end-to-end (CLAUDE.md principle #6).
    """

    model_config = ConfigDict(extra="forbid")

    chat_severity: Severity
    channel: str
    title: str
    body: str
    mentions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    """Logical actions taken — e.g. ['page_oncall', 'post_to_chat',
    'open_war_room']. Descriptive rather than prescriptive: every notification
    still flows through the single chatops seam; adapters inspect this to
    decide whether to escalate."""
    reason: str
    audit_trace: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    category_display: str | None = None
    """Human-readable failure sub-domain ("Payment Gateway") when the
    expertise-aware on-call lookup matched a category; ``None`` otherwise."""
    response_mode: str = "notify"
    """``"page"`` / ``"notify"`` / ``"log"`` — derived from severity + business
    hours; drives DM urgency and the dashboard badge."""
    assignee: str | None = None
    assignee_name: str | None = None
    assignee_email: str | None = None


# ─── War room (was RA-006 War-Room Assembler) ──────────────────────────────


class InvitedSME(BaseModel):
    """One subject-matter expert pulled into the war room.

    ``reason`` records *why* this person was chosen so SME-coverage can be
    audited.
    """

    model_config = ConfigDict(extra="forbid")

    handle: str
    name: str | None = None
    team: str | None = None
    reason: str
    source: str
    """How we found them: ``"oncall"``, ``"cmdb_owner"``, ``"dependency_owner"``."""
    slack_user_id: str | None = None
    invite_status: str | None = None
    """``invited`` / ``already_in`` / ``no_id`` / ``simulated`` / ``failed:<err>``."""


class ContextPackItem(BaseModel):
    """One line in the live context pack posted to the room."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    source: str | None = None


class TimelineEvent(BaseModel):
    """One timestamped entry in the running incident timeline."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    event: str


class WarRoomAssembly(BaseModel):
    """Structured plan/result for one incident's war room. ``decide`` returns
    this with no side effects; ``notify`` enriches it with the real Slack
    bridge once the room is created.
    """

    model_config = ConfigDict(extra="forbid")

    assembled: bool
    """``False`` for severities below Sev-2 (no war room warranted)."""
    channel: str
    title: str
    chat_severity: Severity
    invited: list[InvitedSME] = Field(default_factory=list)
    context_pack: list[ContextPackItem] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    reason: str
    audit_trace: list[str] = Field(default_factory=list)
    assembled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── Slack bridge (populated by ``notify``; ``decide`` leaves defaults) ──
    bridge_status: str = "pending"
    """``pending`` / ``created`` / ``simulated`` / ``failed`` / ``skipped``."""
    bridge_provider: str | None = None
    bridge_channel_id: str | None = None
    bridge_url: str | None = None
    """Deep link that opens the war-room channel — the "bridge link" SMEs click."""
    meeting_url: str | None = None
    """Click-to-join video-call link (Jitsi room) for the live bridge."""


# ─── Combined output ───────────────────────────────────────────────────────


class NotificationAssembly(BaseModel):
    """Pure ``decide`` output: the routing decision plus the war-room plan.

    ``war_room`` is ``None`` only when the routing was suppressed (RA-001
    deduped the alert). For Sev-3/Sev-4 it is present but ``assembled=False``.
    """

    model_config = ConfigDict(extra="forbid")

    decision: RoutingDecision
    war_room: WarRoomAssembly | None = None


class NotificationOutcome(BaseModel):
    """``notify`` result: the combined assembly plus what each chatops adapter
    did with the single emitted message."""

    model_config = ConfigDict(extra="forbid")

    decision: RoutingDecision
    war_room: WarRoomAssembly | None = None
    deliveries: dict[str, DeliveryResult] = Field(default_factory=dict)
