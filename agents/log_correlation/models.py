"""Input/output Pydantic models for the Log Correlation agent (RA-007).

Contract (authoritative source: the RA-007 row of
``docs/Adaptive_AIOps_Agent_Catalog.xlsx``, Reactive-Active sheet):

- Inputs  — logs, traces, metrics, **topology**
- Outputs — **correlated evidence pack**, **suspect components**
- HITL    — None (read-only, like RA-001)

Coupling choice (CLAUDE.md principle #2 — agents couple only through declared
input/output schemas, and the platform is "modular and individually sellable"):
``triage_verdict`` and ``classification`` are carried as plain ``dict``s — the
JSON outputs of RA-001 / RA-002 — rather than importing those agents' Pydantic
classes. This mirrors the RCA agent's ``RCAInput.triage_verdict: dict`` and
keeps RA-007 licensable / runnable standalone: it depends on the upstream wire
contract, not on the upstream Python modules. Both fields are optional so the
agent also runs from a bare ``service`` + ``window``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SignalSource = Literal["logs", "traces", "metrics"]
# Provenance of the signals a verdict was built from. "live" = pulled from the
# observability backends via the registry; "synthetic" = deterministic fallback
# used when those backends are unreachable (CI / offline demo); "mixed" = some
# of each. Surfaced in the audit so a verdict is never mistaken for live data.
EvidenceProvenance = Literal["live", "synthetic", "mixed"]


def _coerce_timestamp(v: Any) -> datetime:
    """Accept ISO 8601 strings (incl. trailing ``Z``) and datetimes; normalize
    naive datetimes to UTC. Mirrors ``Alert._coerce_timestamp`` (RA-001)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str):
        normalized = v.replace("Z", "+00:00") if v.endswith("Z") else v
        return datetime.fromisoformat(normalized)
    raise TypeError(f"Unsupported timestamp type: {type(v).__name__}")


class TimeWindow(BaseModel):
    """The incident time window the correlation is scoped to.

    ``end`` must not precede ``start``. Loki/Jaeger/Prometheus queries are
    bounded by this window so the evidence pack is incident-scoped (the
    catalog's "incident-scoped retention" feature)."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> datetime:
        return _coerce_timestamp(v)

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end < self.start:
            raise ValueError("window.end must be >= window.start")
        return self


class CorrelationInput(BaseModel):
    """Input to ``correlate``.

    ``service`` + ``window`` are the minimum needed to scope a query. The
    optional upstream verdicts (``triage_verdict`` from RA-001, ``classification``
    from RA-002) are carried as dicts and used only to enrich the evidence pack
    and the LLM summary — never as instructions (see prompts.py). ``topology``
    is an optional ``service -> [downstream services]`` map; when omitted the
    agent resolves dependencies via the ``itsm.cmdb.dependencies`` capability so
    topology-aware joining still works.
    """

    model_config = ConfigDict(extra="allow")

    service: str
    window: TimeWindow
    triage_verdict: dict[str, Any] | None = None
    classification: dict[str, Any] | None = None
    topology: dict[str, list[str]] | None = None

    @field_validator("service", mode="before")
    @classmethod
    def _require_service(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            if not s:
                raise ValueError("service must not be empty or whitespace-only")
            return s
        return v


class CorrelatedSignal(BaseModel):
    """One observation on the shared timeline.

    A signal is the unit of the evidence pack: a single log line, trace span
    summary, or metric reading, reduced to an error *signature* (fingerprint)
    plus the raw ``sample`` it came from."""

    model_config = ConfigDict(extra="forbid")

    source: SignalSource
    signature: str
    timestamp: datetime
    severity: str = "info"
    sample: str = ""

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> datetime:
        return _coerce_timestamp(v)


class AuditMetadata(BaseModel):
    """Provenance carried in every correlation result.

    ``decision_trace`` is appended at each pipeline stage so the verdict
    explains itself without re-running the agent (CLAUDE.md principle #6).
    ``signal_source`` records whether the signals were live or synthetic."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "RA-007"
    signal_source: EvidenceProvenance = "live"
    decision_trace: list[str] = Field(default_factory=list)


class CorrelationResult(BaseModel):
    """The correlated evidence pack RA-007 emits (catalog output).

    This whole object *is* the "correlated evidence pack"; ``suspected_dependencies``
    is the catalog's "suspect components". It is designed to drop straight into
    the RCA agent as evidence (see ``agents.rca_agent``).

    ``summary`` is the LLM-ranked, human-readable evidence headline (with a
    deterministic fallback). ``timeline`` is ordered earliest-first so index 0
    is the first observed error.
    """

    model_config = ConfigDict(extra="forbid")

    service: str
    summary: str
    timeline: list[CorrelatedSignal] = Field(default_factory=list)
    top_signatures: list[str] = Field(default_factory=list)
    suspected_dependencies: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    audit_metadata: AuditMetadata
