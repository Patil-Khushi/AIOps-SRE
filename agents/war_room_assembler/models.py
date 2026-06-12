"""Input/output models for the War-Room Assembler agent (RA-006).

The agent's *input* is a ``TriageVerdict`` produced upstream by RA-001
(Alert Triage) and enriched with an ``incident_id`` by RA-003 (Auto-Ticketing).
The *output* is a ``WarRoomAssembly`` — a structured description of the war
room the agent stood up: the bridge channel, the SMEs it invited (and why),
the live context pack it posted, and the seed timeline for RCA.

Why a separate assembly object instead of just emitting chat messages
(mirrors RA-005's RoutingDecision rationale):

- Tests can assert *who would be invited* and *what the context pack contains*
  without touching the chatops seam.
- ``audit_trace`` carries the reasoning steps so the assembly is explainable
  end-to-end (CLAUDE.md principle #6).
- Downstream agents (RA-007 Log Correlation, RCA Agent) read the timeline and
  context pack as their starting surface — a stable model keeps that contract
  intact as the assembler grows richer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from aiops.tools.chatops import DeliveryResult, Severity


class InvitedSME(BaseModel):
    """One subject-matter expert pulled into the war room.

    ``reason`` records *why* this person was chosen (impacted CI, on-call
    rotation, owning team) so SME-coverage can be audited — the agent's KPI
    is partly "SME coverage %", which is meaningless without the why.
    """

    model_config = ConfigDict(extra="forbid")

    handle: str
    """Chat handle to @-mention (``@chinmay``) or, if no handle is on record,
    the engineer's email — same fallback rule as RA-005's mentions."""
    name: str | None = None
    team: str | None = None
    reason: str
    source: str
    """How we found them: ``"oncall"``, ``"cmdb_owner"``, ``"dependency_owner"``."""


class ContextPackItem(BaseModel):
    """One line in the live context pack posted to the room.

    Deliberately generic (label + value + optional source) so metrics, recent
    traces, and recent changes all serialize the same way and render
    identically in chat, the dashboard, and the JSON audit log.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    source: str | None = None


class TimelineEvent(BaseModel):
    """One timestamped entry in the running incident timeline.

    The assembler seeds the timeline (incident opened, room created, SMEs
    invited); RA-007 and the Incident Commander append to it later.
    """

    model_config = ConfigDict(extra="forbid")

    at: datetime
    event: str


class WarRoomAssembly(BaseModel):
    """Structured output of the War-Room Assembler — the plan/result for one
    incident. ``decide`` returns this with no side effects; ``assemble``
    returns it alongside the actual chatops deliveries.
    """

    model_config = ConfigDict(extra="forbid")

    assembled: bool
    """``False`` for severities below Sev-2 (no war room warranted) — the
    no-op case, analogous to RA-005's Suppressed short-circuit."""
    channel: str
    """The war-room channel name, e.g. ``war-room-INC0012345``. Stands in for
    the vendor "bridge link" until a real Teams/Zoom adapter exists."""
    title: str
    chat_severity: Severity
    invited: list[InvitedSME] = Field(default_factory=list)
    context_pack: list[ContextPackItem] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    reason: str
    audit_trace: list[str] = Field(default_factory=list)
    assembled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WarRoomOutcome(BaseModel):
    """``assemble`` result: the assembly plus what each chatops adapter did."""

    model_config = ConfigDict(extra="forbid")

    assembly: WarRoomAssembly
    deliveries: dict[str, DeliveryResult] = Field(default_factory=dict)
