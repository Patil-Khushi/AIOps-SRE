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

from agents.log_correlation.confidence import ConfidenceBreakdown
from agents.log_correlation.evidence import Evidence
from agents.log_correlation.history import SimilarIncidents
from agents.log_correlation.timeline import IncidentTimeline
from aiops.tools.change_context import ChangeContext
from aiops.tools.topology.graph import ServiceGraph

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
    # Optional shared IncidentContext (dict form) from the Context Engineering
    # Layer. Additive, same contract as `triage_verdict`/`classification` above:
    # omitted, `correlate` fetches its own live evidence exactly as before.
    context: dict[str, Any] | None = None

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
    evidence: list[Evidence] | None = None
    """Structured, immutable findings derived from ``timeline``.

    Additive and optional — every pre-existing field above is unchanged.
    ``timeline`` remains the raw signal list it always was; this is a richer *view*
    over the same observations, adding what a reasoning consumer needs and a raw
    signal lacks: a stable identity, a per-finding confidence, a signal type
    distinct from its source, and topology context.

    ``None`` means the build did not produce a result — it was skipped or it
    raised. An empty list means it ran and there was genuinely nothing to derive.
    Those are different facts and this field keeps them apart, as the rest of the
    result does: defaulting to ``[]`` made a caught exception indistinguishable
    from a clean no-signal verdict, which is exactly the ambiguity the "absent is
    not empty" rule exists to prevent. ``audit_metadata.decision_trace`` records
    which of the two happened.

    Consumers that ignore it are unaffected. The RCA agent reads this payload as
    a plain ``dict`` and pulls three keys by name
    (``suspected_dependencies`` / ``top_signatures`` / ``summary``), so a new key
    is invisible to it until it opts in.
    """

    incident_timeline: IncidentTimeline | None = None
    """Merged, grouped, chronological account of the incident.

    Distinct from ``timeline`` above, which is and remains the raw
    ``CorrelatedSignal`` list. This one unifies six sources — logs, metrics,
    traces, topology, deployment and configuration — so a reader can see a
    rollout at 10:02 sitting immediately before the error burst at 10:03. That
    ordering is the part telemetry alone cannot supply.

    Optional and defaults to ``None``: absent means "not built", which is
    different from an empty timeline meaning "nothing happened".
    """

    confidence_breakdown: ConfidenceBreakdown | None = None
    """Derivation of ``confidence`` above — same number, now explained.

    ``confidence: 0.82`` alone is unactionable: it does not say whether the score
    came from three signal sources agreeing or from one weak heuristic, so neither
    a responder nor the RCA agent can weigh it. This records, per rule, the
    increment applied, why it applied, and which evidence triggered it — plus the
    rules that did *not* fire, which is usually the more useful half ("confidence
    is 0.6 because only one signal source was present" names the missing evidence).

    The numeric algorithm is untouched. ``confidence`` and
    ``confidence_breakdown.score`` come from a single implementation, so they
    cannot diverge.
    """

    similar_incidents: SimilarIncidents | None = None
    """Past incidents resembling this one — retrieval evidence, not a conclusion.

    Carries no claim about the *current* incident: no probable cause, no ranked
    hypothesis, no recommended action. Each match records what happened to a past
    incident and why it matched; the RCA agent decides whether any of it is
    relevant. Keeping that inference on the consumer side is what keeps it
    attributable instead of buried in a similarity score.

    ``None`` means retrieval was not attempted — it is opt-in via
    ``AIOPS_INCIDENT_HISTORY``. An empty ``matches`` list *with* a
    ``coverage_note`` means it was attempted and genuinely found nothing, which is
    a different and much stronger statement.
    """

    dependency_graph: ServiceGraph | None = None
    """The multi-hop service map around the incident service.

    Distinct from ``suspected_dependencies`` in both content and meaning.
    ``suspected_dependencies`` is a *suspect list* — one hop, filtered by what the
    evidence implicates. This is the *topology* — every service reachable from the
    root within the depth cap, whether implicated or not. Conflating them is how a
    leaf service ends up drawn as depending on itself, because the sole suspect
    for a self-contained incident is the service itself.

    ``upstream`` is empty whenever the resolving tier answers per-service, since
    such a tier can say what X calls but never what calls X. ``truncated`` and
    ``coverage_note`` record that, so an empty ``upstream`` reads as "not
    observable here", never "nothing calls this".

    ``None`` means no graph was produced — the walk was skipped or it raised. It
    does **not** mean "no dependencies": a graph with an empty ``edges`` list is
    returned as a real result.

    Zero edges is then two further facts, kept apart by ``root_answered``:

    - ``root_answered=True`` — a tier answered and this service genuinely has no
      downstream dependencies. A leaf.
    - ``root_answered=False`` — no tier could answer, so the dependencies are
      *unknown*. ``coverage_note`` says so.

    Collapsing those would be worse than the ambiguous ``None`` this replaced,
    because a consumer would render a resolution failure as a positive "nothing
    depends on this" claim. ``provider`` cannot carry the distinction: it comes from
    ``winning_provider``, unset for a genuine leaf too.

    Whether an edgeless graph is worth *drawing* is a separate, rendering-layer
    question — the console falls back to the suspect list and names which of the
    three cases it is looking at.

    The walk costs one resolution per node, so it is capped by
    ``AIOPS_TOPOLOGY_GRAPH_MAX_DEPTH`` / ``_MAX_NODES``; ``truncated`` records when
    a cap was hit.
    """

    deployment_context: ChangeContext | None = None
    """What changed around this incident — deployments, commits, flags, config.

    Facts only. Records are ordered chronologically, never by suspicion, and
    nothing here asserts that a change caused the incident. Most outages do follow
    a change, which is precisely why the ordering must stay factual: sorting by
    "most likely culprit" would make the RCA agent's judgement invisibly and
    without accountability.

    ``sources_unavailable`` matters as much as the records: an empty list means
    "nothing changed" only when every source actually answered. ``None`` means
    collection was not attempted (opt-in via ``AIOPS_CHANGE_CONTEXT``).
    """
