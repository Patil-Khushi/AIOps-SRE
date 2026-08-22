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
    # ── run-scoped lifecycle (§30) ───────────────────────────────────────────
    # Added for the production upgrade. These are the only events that are NOT
    # step-scoped: they describe the run as a whole, and carry ``step_id=""``.
    # Kept in the same enum (and the same append-only log) so an execution has one
    # ordered story rather than two logs a reader has to interleave.
    CANDIDATES_DISCOVERED = "CANDIDATES_DISCOVERED"
    RUNBOOK_SELECTED = "RUNBOOK_SELECTED"
    APPLICABILITY_CHECKED = "APPLICABILITY_CHECKED"
    PREREQUISITE_CHECKED = "PREREQUISITE_CHECKED"
    PARAMS_VALIDATED = "PARAMS_VALIDATED"
    RISK_ASSESSED = "RISK_ASSESSED"
    DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    STALE_INCIDENT_BLOCKED = "STALE_INCIDENT_BLOCKED"
    CONCURRENCY_BLOCKED = "CONCURRENCY_BLOCKED"
    HANDOFF_TO_VERIFIER = "HANDOFF_TO_VERIFIER"


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


# Parameter/metadata keys whose VALUE is never written to the audit trail (§30).
# Matched as a substring of the lower-cased key, so ``db_password`` and
# ``AIOPS_SERVICENOW_PASSWORD`` are both caught. The key itself is kept — knowing that
# a credential was passed is useful; knowing its value is a liability.
_SECRET_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth",
    "private_key",
    "webhook",
    "session",
    "cookie",
)

_REDACTED = "[redacted]"


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values with ``[redacted]``.

    Applied to anything the audit trail records that came from outside the executor —
    step parameters, provider payloads, approval context. Structure is preserved so the
    trail still shows the shape of what was passed.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_MARKERS):
                out[str(key)] = _REDACTED
            else:
                out[str(key)] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


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

    def emit_run(self, status: AuditEventType, *, reason: str = "", **extra: Any) -> AuditEvent:
        """Emit a run-scoped event (no step). Sugar for ``emit(..., step_id="")``."""
        return self.emit(status, step_id="", reason=reason, **extra)

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
                reason=reason,
                gate_type=gate_type,
                approval_id=approval_id,
                # Redact here rather than at every call site: one place to be right,
                # and a new emitter cannot forget it (§30 "never log secrets").
                **redact(extra),
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
