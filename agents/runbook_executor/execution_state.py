"""Durable execution state, idempotency, concurrency leases and step policy (§19–§25).

Three things live here, all of them about *not doing the same production action twice*:

**The state machine.** ``ExecutionState`` is the durable lifecycle; ``TRANSITIONS``
declares which moves are legal and :func:`assert_transition` refuses the rest, so an
execution cannot go from COMPLETED back to EXECUTING because some retry path forgot to
check. Terminal states are terminal.

**Identity.** :func:`idempotency_key` derives one stable key from
(incident, runbook, version, plan hash). The same incident asking for the same plan
produces the same key, and ``repository.claim_runbook_execution`` turns that into a
database-level uniqueness guarantee: a retried request joins the existing execution
instead of starting a second one. The plan hash is in the key on purpose — an operator
who edits the plan is asking for a *different* execution, and should get one.

**Step policy.** :func:`step_policy` decides timeout/retry per step, and the rule that
matters is: only actions the registry marks ``retry_safe`` may be retried. A timeout is
not proof that a call did not land, so re-issuing a non-idempotent mutation after one is
how you restart a deployment twice.

Persistence is delegated to ``aiops.state.repository`` — this module never touches SQL,
and the repository never imports the agent (``tests/test_layering.py``).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from agents.runbook_executor.actions import ActionSpec
from aiops.tools.resilience import ResiliencePolicy

_DEFAULT_STEP_TIMEOUT = 60.0
_DEFAULT_MAX_RETRIES = 1
_DEFAULT_RETRY_BACKOFF = 0.5
_DEFAULT_BREAKER_SECONDS = 30.0
_DEFAULT_EXECUTION_TIMEOUT = 900.0
_DEFAULT_LEASE_SECONDS = 900


class ExecutionState(StrEnum):
    """§19's durable states. Terminal states never transition again."""

    PLANNED = "planned"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    PAUSED = "paused"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ABORTED = "aborted"


TERMINAL_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
        ExecutionState.ROLLED_BACK,
        ExecutionState.ABORTED,
    }
)

# Legal moves. Anything absent is a bug in the caller, not a state to tolerate.
TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.PLANNED: frozenset(
        {
            ExecutionState.WAITING_APPROVAL,
            ExecutionState.APPROVED,
            ExecutionState.EXECUTING,
            ExecutionState.ABORTED,
        }
    ),
    ExecutionState.WAITING_APPROVAL: frozenset(
        {ExecutionState.APPROVED, ExecutionState.ABORTED, ExecutionState.FAILED}
    ),
    ExecutionState.APPROVED: frozenset(
        {ExecutionState.EXECUTING, ExecutionState.ABORTED, ExecutionState.FAILED}
    ),
    ExecutionState.EXECUTING: frozenset(
        {
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.PAUSED,
            ExecutionState.ROLLING_BACK,
            ExecutionState.ABORTED,
        }
    ),
    ExecutionState.PAUSED: frozenset(
        {ExecutionState.EXECUTING, ExecutionState.ROLLING_BACK, ExecutionState.ABORTED}
    ),
    ExecutionState.ROLLING_BACK: frozenset({ExecutionState.ROLLED_BACK, ExecutionState.FAILED}),
    ExecutionState.COMPLETED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.ROLLED_BACK: frozenset(),
    ExecutionState.ABORTED: frozenset(),
}


class ExecutorStatus(StrEnum):
    """§27's result contract — what the orchestrator branches on.

    Note what is *not* here: there is no ``RESOLVED``. The executor reports that the
    procedure ran (``EXECUTED``); whether the incident recovered is the Resolution
    Verifier's call, and this enum is deliberately incapable of expressing it.
    """

    EXECUTED = "EXECUTED"
    NO_RUNBOOK = "NO_RUNBOOK"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class NextAction(StrEnum):
    """Where the incident goes next. ``VERIFY`` only ever follows ``EXECUTED``."""

    VERIFY = "VERIFY"
    RCA = "RCA"
    ESCALATE = "ESCALATE"


class UiState(StrEnum):
    """§34's frontend state model, produced by the backend so the UI never infers it."""

    DISCOVERING_RUNBOOKS = "DISCOVERING_RUNBOOKS"
    RUNBOOKS_FOUND = "RUNBOOKS_FOUND"
    RUNBOOK_SELECTED = "RUNBOOK_SELECTED"
    VALIDATING = "VALIDATING"
    BLOCKED = "BLOCKED"
    DRY_RUN_READY = "DRY_RUN_READY"
    DRY_RUN_BLOCKED = "DRY_RUN_BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    PAUSED = "PAUSED"
    ROLLING_BACK = "ROLLING_BACK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    WAITING_VERIFICATION = "WAITING_VERIFICATION"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    ESCALATED_TO_RCA = "ESCALATED_TO_RCA"
    NO_RUNBOOK = "NO_RUNBOOK"


# The status a terminal state reports, and where the incident goes from there.
_STATE_STATUS: dict[ExecutionState, tuple[ExecutorStatus, NextAction]] = {
    ExecutionState.COMPLETED: (ExecutorStatus.EXECUTED, NextAction.VERIFY),
    ExecutionState.FAILED: (ExecutorStatus.FAILED, NextAction.RCA),
    ExecutionState.ROLLED_BACK: (ExecutorStatus.ROLLED_BACK, NextAction.RCA),
    ExecutionState.ABORTED: (ExecutorStatus.BLOCKED, NextAction.RCA),
}


class StateTransitionError(RuntimeError):
    """An illegal execution-state move was attempted."""


def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    """Raise unless ``current -> target`` is a declared transition."""
    if target not in TRANSITIONS.get(current, frozenset()):
        allowed = sorted(s.value for s in TRANSITIONS.get(current, frozenset()))
        raise StateTransitionError(
            f"illegal execution-state transition {current.value!r} -> {target.value!r}; "
            f"allowed from {current.value!r}: {allowed or 'none (terminal)'}"
        )


def terminal_outcome(state: ExecutionState) -> tuple[ExecutorStatus, NextAction] | None:
    """The (status, next_action) a terminal state reports, or None if not terminal."""
    return _STATE_STATUS.get(state)


def new_execution_id() -> str:
    """A fresh handle. Prefixed so it is recognisable in a log line."""
    return f"EXEC-{uuid.uuid4().hex[:12]}"


def idempotency_key(
    *, incident_id: str, runbook_id: str, runbook_version: int, plan_hash: str
) -> str:
    """The §20 duplicate-protection key.

    An incident id is required for real deduplication; without one (an ad-hoc run from
    the CLI) the key falls back to the execution's own uniqueness by including a random
    component, because two ad-hoc runs of the same plan are two deliberate runs.
    """
    incident = (incident_id or "").strip()
    if not incident:
        return f"adhoc:{runbook_id}:v{runbook_version}:{plan_hash}:{uuid.uuid4().hex[:8]}"
    return f"{incident}:{runbook_id}:v{runbook_version}:{plan_hash}"


def resource_key(*, namespace: str, service: str) -> str:
    """The §25 lease key: one remediation at a time per namespace/service."""
    return f"{(namespace or 'default').strip()}/{(service or 'unknown').strip()}"


def lease_seconds() -> int:
    raw = os.environ.get("AIOPS_RUNBOOK_LEASE_SECONDS", "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_LEASE_SECONDS
    except ValueError:
        return _DEFAULT_LEASE_SECONDS


def execution_timeout() -> float:
    raw = os.environ.get("AIOPS_RUNBOOK_EXECUTION_TIMEOUT", "").strip()
    try:
        return max(1.0, float(raw)) if raw else _DEFAULT_EXECUTION_TIMEOUT
    except ValueError:
        return _DEFAULT_EXECUTION_TIMEOUT


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0, int(raw)) if raw else default
    except ValueError:
        return default


def step_policy(spec: ActionSpec | None) -> ResiliencePolicy:
    """Timeout / retry / breaker policy for one step (§23).

    Retries are allowed **only** for actions the registry marks ``retry_safe``. A
    timeout tells you the call did not answer, not that it did not happen, so
    re-issuing a non-idempotent mutation risks doing it twice — the exact failure §23
    calls out. The shared ``guard`` wrapper is reused rather than hand-rolling this
    (``aiops/tools/resilience.py``), and caching is never enabled: a cached mutation
    result would report success for a call that never ran.
    """
    retries = _int_env("AIOPS_RUNBOOK_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
    return ResiliencePolicy(
        timeout=_float_env("AIOPS_RUNBOOK_STEP_TIMEOUT", _DEFAULT_STEP_TIMEOUT),
        retries=retries if (spec is not None and spec.retry_safe) else 0,
        backoff=_float_env("AIOPS_RUNBOOK_RETRY_BACKOFF", _DEFAULT_RETRY_BACKOFF),
        breaker_seconds=_float_env("AIOPS_RUNBOOK_STEP_BREAKER_SECONDS", _DEFAULT_BREAKER_SECONDS),
        cache_ttl=0.0,
        cache_empty_ttl=0.0,
    )


class ExecutionRecord(BaseModel):
    """Typed view of one persisted execution row.

    The repository speaks dicts (it must not import agent models); this is the shape
    the agent and the API work with. ``from_row`` tolerates missing keys so an older
    row cannot crash a newer reader.
    """

    execution_id: str
    idempotency_key: str = ""
    incident_id: str = ""
    runbook_id: str = ""
    runbook_version: int = 1
    plan_hash: str = ""
    service: str = ""
    environment: str = ""
    state: ExecutionState = ExecutionState.PLANNED
    status: str = ""
    next_action: str = ""
    risk_level: str = ""
    hitl_required: bool = True
    approval_id: str | None = None
    approver: str | None = None
    selected_by: str = ""
    selection_reason: str = ""
    match_score: float | None = None
    candidates: list[dict] = Field(default_factory=list)
    dry_run: dict = Field(default_factory=dict)
    # Per-step parameter overrides the plan was authorized with, keyed by step name.
    overrides: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list)
    audit_events: list[dict] = Field(default_factory=list)
    rollback_status: str = "not_required"
    reason: str = ""
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> ExecutionRecord:
        payload = {k: v for k, v in (row or {}).items() if k in cls.model_fields}
        payload.setdefault("execution_id", "")
        if payload.get("incident_id") is None:
            payload["incident_id"] = ""
        return cls.model_validate(payload)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def utcnow() -> datetime:
    """One place to take the clock, so tests can monkeypatch a single symbol."""
    return datetime.now(UTC)


def ui_state_for(
    *,
    state: ExecutionState | None = None,
    dry_run_ready: bool | None = None,
    verification: str | None = None,
) -> UiState:
    """Derive §34's UI state. The backend owns this so the frontend never guesses.

    Verification wins when present: once the Resolution Verifier has spoken, that is
    the truest thing known about the incident. Between a completed execution and a
    verifier verdict the state is ``WAITING_VERIFICATION`` — never ``COMPLETED`` alone,
    because "completed" plus nothing else reads as "resolved" to a human.
    """
    verdict = (verification or "").strip().lower()
    if verdict in ("pass", "passed"):
        return UiState.VERIFICATION_PASSED
    if verdict in ("fail", "failed"):
        return UiState.VERIFICATION_FAILED
    if state is ExecutionState.COMPLETED:
        return UiState.WAITING_VERIFICATION
    mapping = {
        ExecutionState.PLANNED: UiState.DRY_RUN_READY,
        ExecutionState.WAITING_APPROVAL: UiState.WAITING_APPROVAL,
        ExecutionState.APPROVED: UiState.APPROVED,
        ExecutionState.EXECUTING: UiState.EXECUTING,
        ExecutionState.PAUSED: UiState.PAUSED,
        ExecutionState.ROLLING_BACK: UiState.ROLLING_BACK,
        ExecutionState.FAILED: UiState.FAILED,
        ExecutionState.ROLLED_BACK: UiState.ROLLED_BACK,
        ExecutionState.ABORTED: UiState.BLOCKED,
    }
    if state is not None:
        return mapping.get(state, UiState.VALIDATING)
    if dry_run_ready is True:
        return UiState.DRY_RUN_READY
    if dry_run_ready is False:
        return UiState.DRY_RUN_BLOCKED
    return UiState.VALIDATING


__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ExecutionRecord",
    "ExecutionState",
    "ExecutorStatus",
    "NextAction",
    "StateTransitionError",
    "UiState",
    "assert_transition",
    "execution_timeout",
    "idempotency_key",
    "lease_seconds",
    "new_execution_id",
    "resource_key",
    "step_policy",
    "terminal_outcome",
    "ui_state_for",
    "utcnow",
]
