"""Serializable service-topology graph model.

The flat ``dependencies`` list a provider returns answers "what does X call?".
This module answers the questions that list cannot: how far away is a service,
what calls *into* X, and what is known about each hop.

Pydantic rather than the dataclasses in ``base.py``
---------------------------------------------------
``base.py`` types are internal plumbing between the resolver and its providers.
A graph is different: it is intended to be handed to another agent (the RCA
agent, eventually) and to cross a JSON boundary intact. Pydantic gives that for
free via ``model_dump(mode="json")`` — datetimes become ISO strings, nested
models flatten — and matches how every other cross-agent contract in this repo
is declared.

This IS a published contract — treat field changes accordingly
---------------------------------------------------------------
It was internal-only when written. It no longer is: ``CorrelationResult`` declares
``dependency_graph: ServiceGraph | None`` (``agents/log_correlation/models.py``), so
this model is serialized on every ``POST /api/correlate`` response and rendered by
``DependencyGraphPanel`` in the dashboard.

Consequences, since the previous note here said the opposite and would mislead
anyone reasoning about a change:

- Renaming or removing a field breaks the console. ``root_answered`` and
  ``coverage_note`` in particular are branched on there, not merely displayed —
  ``root_answered=False`` is what stops an unresolvable service being rendered as a
  positive "has no dependencies".
- Adding a field is cheap (consumers ignore unknown keys) but costs payload weight
  on every correlation.
- ``model_config = ConfigDict(extra="forbid")`` means a consumer that re-validates a
  dumped graph will reject an unrecognised key, so additions still need the
  TypeScript side updated (``demo/dashboard/src/types/api.ts``).

Depth and direction
-------------------
``depth`` is an unsigned hop count from the root and ``relation`` carries the
direction. The alternative — signed depth, negative for upstream — reads neatly
in a diagram but forces every consumer to remember the convention, and breaks
outright for a node that is both upstream and downstream of the root (common
with diamonds and cycles). A node appears exactly once, keyed by service, with
``relation="both"`` in that case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeRelation = Literal["root", "downstream", "upstream", "both"]
HealthState = Literal["healthy", "degraded", "unhealthy", "unknown"]


class ServiceMetadata(BaseModel):
    """Deployment facts about a service.

    Every field is optional because sources differ in what they can supply: the
    OTel tier knows nothing about namespaces, a Kubernetes tier would know
    little about call rates. Absent is represented as ``None``, never as a
    guessed default — a wrong namespace is worse than no namespace.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    cluster: str | None = None
    version: str | None = None
    environment: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class NodeHealth(BaseModel):
    """Health of one node at build time.

    Defaults to ``unknown`` on purpose. If the health source is unreachable the
    honest answer is "we do not know", and defaulting to ``healthy`` would let a
    dead dependency look fine to whatever reads this graph.
    """

    model_config = ConfigDict(extra="forbid")

    status: HealthState = "unknown"
    error_rate: float | None = None
    detail: str | None = None
    checked_at: datetime | None = None


class EdgeMetadata(BaseModel):
    """Provenance and observed characteristics of one directed edge.

    ``confidence`` encodes how much the edge is worth trusting by source kind:
    an edge *observed* in live traffic (OTel) is stronger evidence than one
    *declared* in a CMDB, which is in turn stronger than one read from a static
    demo table. Downstream reasoning can weight suspects accordingly instead of
    treating every edge as equally true.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    protocol: str | None = None
    call_rate: float | None = None
    error_rate: float | None = None
    latency_p95_ms: float | None = None
    observed_at: datetime | None = None
    confidence: float | None = None


class GraphEdge(BaseModel):
    """A directed dependency: ``source`` calls ``target``."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    metadata: EdgeMetadata


class GraphNode(BaseModel):
    """One service in the graph.

    ``depth`` is the shortest hop distance from the root (0 for the root
    itself), regardless of direction; ``relation`` says which side it sits on.
    """

    model_config = ConfigDict(extra="forbid")

    service: str
    depth: int = 0
    relation: NodeRelation = "downstream"
    health: NodeHealth | None = None
    metadata: ServiceMetadata | None = None


class ServiceGraph(BaseModel):
    """A root-centred view of the service topology.

    ``downstream`` and ``upstream`` are materialised as plain name lists
    alongside ``nodes`` because they are the two questions callers ask most, and
    deriving them requires knowing the ``relation``/``depth`` convention. Cheap
    to carry, and it keeps consumers from re-implementing the traversal.

    ``truncated`` is important to read: it is set when a depth or node cap was
    hit **or** when the edge source could not supply reverse edges. In the
    latter case ``upstream`` is empty because it is *unknown*, not because
    nothing calls the root — a distinction that would otherwise be invisible and
    badly misleading.
    """

    model_config = ConfigDict(extra="forbid")

    root: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    max_depth_reached: int = 0
    truncated: bool = False
    coverage_note: str | None = None
    """Why the edge set is incomplete, when it is.

    Set whenever the source cannot see the whole graph — for example the OTel
    tier derives edges from gRPC client metrics only, so a service reached over
    HTTP is invisible to it. Without this, ``upstream: []`` from a gRPC-only
    source is indistinguishable from a genuine "nothing calls this service",
    and a consumer (eventually the RCA agent) would treat an unobserved caller
    as an absent one. Paired with ``truncated=True``."""

    provider: str | None = None
    built_at: datetime | None = None

    root_answered: bool = True
    """Whether any topology tier actually answered for the root service.

    This is what makes an edgeless graph readable. Without it, ``edges == []``
    means two opposite things — "a tier answered and this service genuinely has no
    downstream dependencies" (a leaf) and "no tier could answer, so its
    dependencies are unknown" — and a consumer rendering the second as the first
    turns a resolution failure into a positive claim.

    ``provider`` cannot carry this: it is populated from ``winning_provider``,
    which is only set when a tier resolves dependencies, so a genuine leaf and a
    total resolution failure both report no provider.

    Defaults ``True`` because a source that hands over a whole edge set (the OTel
    tier) has by definition answered. The per-service walk sets it explicitly.
    """

    @property
    def upstream_complete(self) -> bool:
        """Whether ``upstream`` can be read as exhaustive.

        False when the edge source was protocol- or scope-limited. Callers that
        reason about blast radius should check this before concluding a service
        has no callers.
        """
        return not self.truncated and self.coverage_note is None

    def node(self, service: str) -> GraphNode | None:
        """Look up a node by service name, or ``None``."""
        return next((n for n in self.nodes if n.service == service), None)
