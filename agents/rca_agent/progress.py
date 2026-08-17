"""Progress instrumentation for the RCA agent's ``analyze()`` pipeline.

Pure module — no asyncio, no FastAPI, no ``aiops`` import — so ``agents/rca_agent``
stays runnable standalone (CLAUDE.md principle 2: every agent is individually
sellable; it cannot depend on this demo's web server to run).

``ProgressSink`` is the seam: ``agent.py::analyze()`` wraps an optional external
sink in a ``RunProgress`` and calls ``run.emit(stage, label, ...)`` at real
pipeline-stage boundaries — the same boundaries already recorded in
``decision_trace``, so this is honest instrumentation, not decorative labels.
Production wires a sink that forwards events onto ``demo/ui/rca_progress.py``'s
SSE hub (``HubSink``); tests pass a plain recording sink; omitting ``progress``
entirely (the default) reproduces ``analyze()``'s output byte-for-byte — see
``RunProgress`` below.

Promotion trigger: if a second agent ever wants the same channel (Incident
Commander chaining RCA is the likely candidate), move this module to
``aiops/progress/`` unchanged, and leave ``RcaStage`` behind — it is RCA-specific,
the sink protocol and bookkeeping are not.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RcaStage(StrEnum):
    """Real boundaries inside ``agent.py``'s ``analyze()`` / ``_investigate()``.

    Each one sits beside an existing ``decision_trace.append`` call. Deliberately
    NOT instrumented inside ``investigation/pipeline.py``'s internal stages
    (scope/timeline/baseline/matrices/scoring/blast-radius/recovery/verification):
    they are pure, sub-100ms functions over already-collected facts (see that
    module's own docstring) — emitting progress there would be decorative, which
    is exactly what "must be driven by REAL backend progress" rules out. One
    ``HYPOTHESES`` event carrying the *result* of all nine is the honest
    granularity; the real time is in the I/O boundaries below and the LLM call.

    ``COMPLETE``/``FAILED`` are never emitted from here — see
    ``demo/ui/server.py::_push_terminal_progress``. The terminal event reflects
    the whole HTTP request (remediation composition + persistence happen after
    ``analyze()`` returns), not just the analysis half of it.
    """

    RECEIVED = "received"
    CHANGE_CORRELATION = "change_correlation"
    EVIDENCE = "evidence"
    CONTEXT_PACK = "context_pack"
    MEMORY_RECALL = "memory_recall"
    ACTION_VOCABULARY = "action_vocabulary"
    HYPOTHESES = "hypotheses"
    EXPLAINING = "explaining"
    COMPLETE = "complete"
    FAILED = "failed"


class StageOutcome(StrEnum):
    STARTED = "started"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class StageEvent(BaseModel):
    """One progress frame — sent verbatim (via ``model_dump(mode="json")``) as
    an SSE ``data:`` payload by ``demo/ui/rca_progress.py``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seq: int
    stage: RcaStage
    outcome: StageOutcome
    label: str
    detail: str = ""
    elapsed_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProgressSink(Protocol):
    """What a ``RunProgress`` forwards a finished ``StageEvent`` to.
    Implementations: ``demo/ui/rca_progress.py::HubSink`` (production) or a
    plain recording sink (tests)."""

    def emit(self, event: StageEvent) -> None: ...


class RunProgress:
    """Wraps one ``analyze()`` call's ``run_id`` + an optional external
    ``ProgressSink``, and is itself the ``progress`` object threaded through
    ``agent.py`` — ``run.emit(stage, label, **data)`` builds the ``StageEvent``
    (assigning ``seq``/``elapsed_ms``) and forwards it to the wrapped sink.

    With no sink (the common case — no ``run_id`` on the request), ``emit()``
    is a cheap no-op: it returns before building anything, so
    ``analyze(triage_verdict)`` with no ``progress`` reproduces today's
    behaviour exactly. Swallows any exception the wrapped sink raises — a
    broken listener must never affect the verdict, the same posture as every
    other enrichment in this module (an unreachable Prometheus costs evidence,
    not the RCA; a broken progress sink costs a UI update, not the RCA).
    """

    def __init__(self, run_id: str, sink: ProgressSink | None) -> None:
        self.run_id = run_id
        self._sink = sink
        self._seq = 0
        self._started_at = datetime.now(UTC)

    def emit(
        self,
        stage: RcaStage,
        label: str,
        *,
        outcome: StageOutcome = StageOutcome.STARTED,
        **data: Any,
    ) -> None:
        if self._sink is None:
            return
        self._seq += 1
        now = datetime.now(UTC)
        event = StageEvent(
            run_id=self.run_id,
            seq=self._seq,
            stage=stage,
            outcome=outcome,
            label=label,
            data=data,
            elapsed_ms=int((now - self._started_at).total_seconds() * 1000),
            at=now,
        )
        with contextlib.suppress(Exception):
            self._sink.emit(event)
