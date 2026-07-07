"""Append-only audit event log for the Runbook Executor (RA-004).

RA-004 produces a final :class:`~agents.runbook_executor.models.RunbookExecution`
summary at the end of a run. This module adds a stream of discrete, ordered,
immutable events emitted *during* execution, so the full lifecycle of a run can
be reconstructed step-by-step without the final summary object.

Design guarantees (issue #213):

- **Append-only.** :class:`EventLog` exposes exactly one writer — :meth:`emit`
  (append). There is no update / delete / replace method anywhere. The backing
  list is private; reads return an immutable snapshot.
- **Immutable events.** :class:`AuditEvent` is ``frozen`` — an emitted event can
  never be mutated in place, even by a holder of the reference.
- **Deterministic ordering.** Each event carries a monotonic ``seq`` (assigned
  from the current log length), so ordering is guaranteed independent of
  timestamp collisions. ``seq`` is the ordering key; ``timestamp`` is provenance.
- **Observe, never enforce.** Nothing here consults the HITL gate. Gate-related
  events are reconstructed from the ``ToolResult`` the registry already returned
  (``aiops/tools/registry.py`` remains the single enforcement point, CLAUDE.md #3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventType(StrEnum):
    """The discrete state transitions RA-004 emits during a run.

    Step-scoped by design — every event names the ``step_id`` it concerns.
    ``STEP_SIMULATED`` fires in the dry-run preview pass; the rest fire in the
    execution / rollback passes. Blocked and failed are kept distinct:
    ``STEP_BLOCKED`` is a HITL-gate refusal, ``STEP_FAILED`` is a tool that ran
    and returned an error.
    """

    STEP_STARTED = "STEP_STARTED"
    STEP_SIMULATED = "STEP_SIMULATED"
    GATE_CHECKED = "GATE_CHECKED"
    HITL_REQUESTED = "HITL_REQUESTED"
    HITL_APPROVED = "HITL_APPROVED"
    STEP_EXECUTED = "STEP_EXECUTED"
    STEP_FAILED = "STEP_FAILED"
    STEP_BLOCKED = "STEP_BLOCKED"
    STEP_ROLLED_BACK = "STEP_ROLLED_BACK"


class AuditEventMetadata(BaseModel):
    """Structured metadata for an :class:`AuditEvent`.

    The three documented keys (issue #213) are always present; ``extra="allow"``
    keeps the shape forward-compatible so emitters can attach context
    (capability, level, …) without a schema change.

    ``frozen=True`` so the metadata can't be mutated after construction — the
    parent :class:`AuditEvent` is frozen too, but ``frozen`` does not propagate
    to nested models, so without this an emitted event's ``metadata.reason``
    could still be reassigned in place (the append-only/immutability guarantee).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    reason: str = ""
    gate_type: str = ""  # "" | "none" | "required"  (AutonomyLevel.value)
    approval_id: str = ""


class AuditEvent(BaseModel):
    """One immutable, ordered audit event.

    ``frozen=True`` makes the model hashable and read-only after construction —
    the immutability half of the append-only guarantee.
    """

    model_config = ConfigDict(frozen=True)

    seq: int  # monotonic per-run ordering key (0-based)
    incident_id: str
    runbook_id: str
    step_id: str
    timestamp: datetime
    status: AuditEventType
    metadata: AuditEventMetadata = Field(default_factory=AuditEventMetadata)


class EventLog:
    """An append-only, ordered event log for a single runbook execution.

    The only mutation is :meth:`emit` (append). No update / delete / replace
    method is exposed. ``incident_id`` and ``runbook_id`` are fixed at
    construction so callers only pass per-event detail.
    """

    def __init__(self, *, incident_id: str, runbook_id: str) -> None:
        self._incident_id = incident_id
        self._runbook_id = runbook_id
        self._events: list[AuditEvent] = []

    def emit(
        self,
        status: AuditEventType,
        *,
        step_id: str,
        reason: str = "",
        gate_type: str = "",
        approval_id: str = "",
        **extra: Any,
    ) -> AuditEvent:
        """Construct and append one event; return it. ``seq`` is assigned from
        the current log length, so events are strictly ordered and gap-free."""
        event = AuditEvent(
            seq=len(self._events),
            incident_id=self._incident_id,
            runbook_id=self._runbook_id,
            step_id=step_id,
            timestamp=datetime.now(UTC),
            status=status,
            metadata=AuditEventMetadata(
                reason=reason, gate_type=gate_type, approval_id=approval_id, **extra
            ),
        )
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        """An immutable snapshot of the log in emission order."""
        return tuple(self._events)

    def as_list(self) -> list[AuditEvent]:
        """A fresh list copy of the log (for serialising onto the result)."""
        return list(self._events)
