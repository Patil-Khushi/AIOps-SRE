"""``IncidentContext`` — the immutable Context Pack.

This is the aggregate the whole layer exists to produce: one incident's evidence,
collected once, normalised, correlated, ranked, enriched, redacted and budgeted,
handed to every agent that needs it.

On the name
-----------
The design documents and the architecture diagram call this concept the **Context
Pack**. The class is called ``IncidentContext`` because "context pack" is already
taken in this codebase by public, dashboard-visible API:
``agents/notification_assembler/models.py`` has ``ContextPackItem`` and
``WarRoomAssembly.context_pack`` (rendered in Slack bodies, the JSONL audit log,
and ``demo/dashboard/src/types/api.ts``), and ``agents/incident_commander`` has
``_context_pack_body``. Renaming those to free up the word would break the
dashboard contract for a purely cosmetic gain. "Context Pack" remains the concept
name; ``IncidentContext`` is the code name.

Immutability
------------
Every model here is ``frozen=True`` **and** every collection field is a ``tuple``,
not a ``list``. Frozen alone only locks attribute rebinding — a ``list`` field
would still be mutable in place, so an agent could append to another agent's
evidence and nothing would stop it. Tuples make "agents may never modify the
context" a type error instead of a code review note.

The two exceptions are ``Observation.metadata`` and ``ContextSection.raw``, which
stay plain ``dict``/``Any``. That is the same compromise ``SupportingTelemetry``
and ``ChangeRecord`` already make in this repo: deep-freezing via
``MappingProxyType`` would be new machinery nothing else here uses, for containers
that only ever hold small, provider-echoed, read-only payloads.
``tests/test_context_models.py`` asserts the tuple discipline generically over
``model_fields``, so a future field added as a ``list`` fails CI rather than
quietly opening a hole.

Absent is not empty
-------------------
Every section carries a ``SectionStatus``. A consumer must branch on that, never
on ``len(section.observations) == 0``, because four different facts collapse into
"no observations": nobody asked (``NOT_REQUESTED``), we could not ask
(``UNAVAILABLE``), we asked and errored (``FAILED``), and we asked and the answer
is genuinely nothing (``EMPTY``). Only the last is evidence about the world. RA-007
and the RCA prompt both already depend on that distinction — RCA renders an
explicit "NONE — this signal was checked and was absent" line, which is only
truthful for ``EMPTY``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from aiops.context.models import Observation, SectionStatus, Source


class SourceProvenance(BaseModel):
    """How one section's data was obtained.

    Exists so a consumer can explain itself. An agent that says "no recent
    deployments" needs to be able to answer "how do you know?" with either "the
    SCM provider returned an empty commit list in 40ms" or "the SCM capability is
    not registered on this deployment" — and those warrant very different
    confidence in the resulting verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """Which backend actually answered — ``prometheus``, ``loki``, ``mock``. The
    vendor name lives here so ``Observation.source`` can stay vendor-neutral."""

    status: SectionStatus
    latency_ms: float = 0.0
    cached: bool = False
    """True when served from the intra-incident cache rather than a fresh call.
    Surfaced rather than hidden because it is the measurable proof that the
    deduplication this layer exists for is actually happening."""

    error: str | None = None
    """Verbatim provider error text. Kept unmodified because agent adapters
    reproduce legacy decision-trace lines that embed it, and a reworded error
    would change an operator-facing audit string."""

    coverage_note: str | None = None
    """Why an answer is partial or empty, in operator-readable words. The
    companion to ``EMPTY``: ``[]`` plus a note is a claim about the world,
    ``[]`` alone is ambiguous."""


class ContextSection(BaseModel):
    """One source's contribution to the context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SectionStatus
    observations: tuple[Observation, ...] = ()
    provenance: SourceProvenance

    raw: dict[str, object] | None = None
    """The provider's untouched payload (``ToolResult.data``), keyed by query id.

    This is the escape hatch that makes a byte-identical migration possible.
    RCA's prompt contains strings like ``f"pod {pod}: cpu={cores:.2f} cores
    (limit 1)"`` built from raw Prometheus rows, and RA-007's log truncation is
    *stream-grouping-order dependent* — it walks ``streams[:5]`` then
    ``values[:limit]`` and stops mid-loop. Reconstructing either from normalised
    ``Observation`` objects would silently change which lines an agent sees. So
    the normalised view and the raw payload both travel, and each adapter uses
    whichever preserves its existing behaviour.
    """

    @property
    def usable(self) -> bool:
        """Whether this section carries a trustworthy answer, empty or not."""
        return self.status.usable


class IncidentIdentity(BaseModel):
    """Which incident this context is about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    severity: str
    window_start: datetime
    window_end: datetime
    correlation_id: str
    """Deterministic id derived from service + window — see
    ``aiops.context.correlation``. Not a UUID, so two independent calls about the
    same incident agree on it without coordination, which is what lets a
    standalone agent invocation share the orchestrated run's cache."""

    alert_id: str | None = None
    alert_name: str | None = None


class RankedObservation(BaseModel):
    """One observation's position in the relevance ordering.

    Kept as a separate, side-car ranking rather than a ``score`` field on
    ``Observation`` for two reasons: the same observation can be ranked
    differently for different consumers, and an observation is a *fact* while a
    rank is a *judgement*. Keeping facts and judgements in separate objects is the
    same discipline ``Evidence`` follows by separating a claim from its supporting
    telemetry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    score: float
    rank: int
    rationale: str
    """Why this scored where it did, in words — e.g. ``"cross-source agreement
    (logs+traces); 4m old; 1 hop from checkout"``.

    Required, not optional. RA-007's ``confidence.py`` established the convention
    that a score handed to a human or a prompt must be able to explain itself; an
    unexplainable 0.83 is not reviewable, and a ranking nobody can audit is a
    ranking nobody should trust with an incident.
    """


class SecurityMetadata(BaseModel):
    """What the redaction stage did, and what the layer refused to fetch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    redaction_applied: bool
    redaction_counts: dict[str, int] = Field(default_factory=dict)
    """Per-pattern hit counts, e.g. ``{"github_token": 2}``. Counts rather than
    values, obviously — but non-zero counts are what let a reviewer confirm
    redaction ran and see what it caught, following the
    ``ToolResult.metadata["redactions"]`` convention ``scm/github.py`` already
    uses."""

    denied_capabilities: tuple[str, ...] = ()
    """Capabilities a caller requested that the layer refuses to serve. Recorded
    rather than silently dropped so a misuse is visible instead of mysterious."""


class TokenBudget(BaseModel):
    """The outcome of budgeting a context for one consumer.

    Present only after ``tokenizer.budget()`` has projected the context. A context
    with ``token_budget=None`` has not been trimmed for anyone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    """Which consumer this projection was budgeted for. The same context yields
    different projections for RCA (wants deep telemetry) and a summary agent
    (wants breadth), so the profile is recorded to stop a trimmed-for-summary
    context being mistaken for the full article."""

    max_tokens: int
    estimated_tokens: int
    truncated: bool
    dropped_sections: tuple[str, ...] = ()
    evicted_observation_ids: tuple[str, ...] = ()
    """Exactly what was removed.

    Populated whenever anything was dropped, because silent truncation is the
    failure mode that makes an LLM confidently wrong: a model handed a trimmed
    evidence set with no indication it was trimmed will reason as though it saw
    everything. A consumer must always be able to tell it got a partial view.
    """


class IncidentContext(BaseModel):
    """The immutable Context Pack: one incident's evidence, collected once.

    Serialisable via ``model_dump(mode="json")`` and round-trippable via
    ``model_validate``, so it can be cached, persisted, logged, or carried across
    a JSON boundary to an agent invoked over HTTP or MCP without any Python import.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    """Bumped when a field's meaning changes incompatibly.

    Present from the start because this object gets cached and serialised. Reading
    back a payload written by an older build without a version to check against is
    how a cache turns into a silent data-corruption bug.
    """

    incident: IncidentIdentity
    built_at: datetime

    # Telemetry — the evidence an agent reasons *from*.
    metrics: ContextSection
    logs: ContextSection
    traces: ContextSection
    k8s_events: ContextSection

    # Structure and change — what the failing service is connected to, and what
    # moved recently.
    topology: ContextSection
    dependencies: ContextSection
    deployments: ContextSection
    incident_history: ContextSection

    # Ownership and remediation. Not evidence about the failure, but retrieval all
    # the same — and the on-call lookup is the single most duplicated call in the
    # codebase (four times per incident for an answer that cannot change within
    # one). Collapsing it is the layer's clearest measurable win, so it needs a
    # home here rather than being left to the enrichment stage's metadata.
    oncall: ContextSection
    cmdb: ContextSection
    runbooks: ContextSection

    evidence_ranking: tuple[RankedObservation, ...] = ()
    security: SecurityMetadata
    token_budget: TokenBudget | None = None

    def section(self, source: Source) -> ContextSection:
        """Look up a section by its source name.

        Lets an adapter iterate sources generically instead of hard-coding eleven
        attribute accesses. Total over ``Source`` — every literal value has a
        section, which ``tests/test_context_models.py`` asserts so the two cannot
        drift apart.
        """
        try:
            return self.sections[source]
        except KeyError as exc:  # pragma: no cover - guarded by the Literal + a test
            raise KeyError(f"unknown context section: {source!r}") from exc

    @property
    def sections(self) -> dict[str, ContextSection]:
        """Every section keyed by source name. A fresh dict per call — mutating
        it cannot reach the context."""
        return {
            "metrics": self.metrics,
            "logs": self.logs,
            "traces": self.traces,
            "k8s_events": self.k8s_events,
            "topology": self.topology,
            "dependencies": self.dependencies,
            "deployments": self.deployments,
            "incident_history": self.incident_history,
            "oncall": self.oncall,
            "cmdb": self.cmdb,
            "runbooks": self.runbooks,
        }

    @property
    def observations(self) -> tuple[Observation, ...]:
        """Every observation from every section, in section order."""
        return tuple(obs for section in self.sections.values() for obs in section.observations)

    @property
    def usable_sources(self) -> tuple[str, ...]:
        """Sources that returned a trustworthy answer, empty or not."""
        return tuple(name for name, section in self.sections.items() if section.usable)

    @property
    def is_empty(self) -> bool:
        """True when no section produced a usable answer.

        The signal an adapter uses to hand back nothing and let its agent fall
        through to its legacy retrieval path, rather than presenting a context-shaped
        void as though it were evidence.
        """
        return not any(section.observations for section in self.sections.values())
