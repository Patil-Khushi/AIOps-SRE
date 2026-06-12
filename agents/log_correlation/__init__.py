"""Log Correlation agent (RA-007) — Reactive-Active phase.

Fills the evidence gap in the POC chain:

    Alert Triage (RA-001) → Incident Classifier (RA-002) → Auto-Ticketing
    (RA-003) → **Log Correlation (RA-007)** → RCA Agent (PRS-008)

Takes a triaged/classified incident (service + time window), pulls logs (Loki),
traces (Jaeger), and metrics (Prometheus) for that window, correlates them on a
shared timeline, and emits a ``CorrelationResult`` — the catalog's "correlated
evidence pack" + "suspect components" — that becomes the RCA agent's evidence
input. Read-only; HITL level None.

Public surface::

    from agents.log_correlation import (
        CorrelatedSignal, CorrelationInput, CorrelationResult, TimeWindow,
        correlate, run, reset_state,
    )
"""

from agents.log_correlation.agent import correlate, reset_state, run
from agents.log_correlation.models import (
    AuditMetadata,
    CorrelatedSignal,
    CorrelationInput,
    CorrelationResult,
    TimeWindow,
)

__all__ = [
    "AuditMetadata",
    "CorrelatedSignal",
    "CorrelationInput",
    "CorrelationResult",
    "TimeWindow",
    "correlate",
    "reset_state",
    "run",
]
