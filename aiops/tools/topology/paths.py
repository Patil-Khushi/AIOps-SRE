"""Multi-hop dependency path discovery over a ``ServiceGraph``.

What this adds beyond Phase 2
-----------------------------
``ServiceGraph`` already answers *how far* a service is (``GraphNode.depth``) and
*whether* it is reachable (``downstream`` / ``upstream``). It does not say *by
which route*. For an incident that distinction is the whole point: "currency is 2
hops from checkout" is trivia, while ``checkout -> payment -> currency`` names the
services a responder should look at and the order a fault would have propagated
in. This module produces those ordered chains.

Pure computation over an already-built graph
--------------------------------------------
Everything here operates on a ``ServiceGraph`` that was built elsewhere — no
registry, no HTTP, no clock. Building the graph costs one PromQL query; walking it
for paths costs nothing external, so path discovery is separated from graph
construction rather than folded into it. That also keeps this exhaustively
testable without a cluster.

Cycles are reported, not merely survived
----------------------------------------
Phase 2 made traversal cycle-*safe* (BFS with a visited set, so a loop cannot
hang it). Here cycles are a first-class output: a dependency loop such as
``a -> b -> a`` is an architectural fact worth surfacing to whoever reads the
evidence, because it turns a single fault into a cascade and it defeats naive
"walk upstream to the root cause" reasoning.

Backward compatibility
----------------------
Purely additive. ``ServiceGraph`` is unchanged, no existing field is touched, and
nothing here is wired into ``CorrelationResult`` — same posture as the Phase 2
graph, which stays internal to ``aiops.tools.topology`` until a consumer exists.
"""

from __future__ import annotations

import os
from collections import deque
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aiops.tools.topology import cache as _cache
from aiops.tools.topology.graph import GraphEdge, ServiceGraph

PathDirection = Literal["downstream", "upstream"]

# Enumerating *every* simple path is exponential in a dense graph, so the
# all-paths API is bounded. The default is generous for a service graph of this
# size while making a pathological input impossible to hang on.
_MAX_PATHS = int(os.environ.get("AIOPS_TOPOLOGY_MAX_PATHS", "25"))
_MAX_CYCLES = int(os.environ.get("AIOPS_TOPOLOGY_MAX_CYCLES", "10"))
_PATHS_TTL = float(os.environ.get("AIOPS_TOPOLOGY_PATHS_TTL", "60"))


class DependencyPath(BaseModel):
    """One ordered chain of services.

    ``hops`` is inclusive of both endpoints, so ``frontend -> checkout -> payment``
    is ``["frontend", "checkout", "payment"]`` and ``depth`` is 2. Storing the
    endpoints in the list (rather than implying them) means a path renders and
    reads correctly on its own, without the caller needing the graph to
    reconstruct it.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    hops: list[str] = Field(default_factory=list)
    depth: int = 0
    direction: PathDirection = "downstream"
    edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def chain(self) -> str:
        """Human-readable rendering, e.g. ``checkout -> payment -> currency``.

        Provided because this is what ends up in a decision trace or an LLM
        prompt, and every caller would otherwise re-implement the join.
        """
        return " -> ".join(self.hops)


class DependencyPaths(BaseModel):
    """All discovered paths radiating from one root, plus any cycles found."""

    model_config = ConfigDict(extra="forbid")

    root: str
    downstream: list[DependencyPath] = Field(default_factory=list)
    upstream: list[DependencyPath] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    max_depth_reached: int = 0
    truncated: bool = False
    coverage_note: str | None = None
    """Carried over from the source graph.

    A path set inherits its graph's blind spots: if the graph was built from
    gRPC-only edges then an absent upstream path means "no route observed", not
    "no route". Propagating the note keeps that caveat attached to the paths
    instead of losing it at the boundary."""

    def to(self, service: str) -> DependencyPath | None:
        """Shortest downstream path to ``service``, if one was found."""
        return next((p for p in self.downstream if p.target == service), None)

    def frm(self, service: str) -> DependencyPath | None:
        """Shortest upstream path from ``service`` into the root, if one exists."""
        return next((p for p in self.upstream if p.target == service), None)


def _adjacency(graph: ServiceGraph, direction: PathDirection) -> dict[str, list[str]]:
    """Build a name-only adjacency map in the requested direction.

    Deterministically ordered: BFS over a dict whose insertion order varies would
    return different (equally valid) shortest paths run to run, which makes tests
    flaky and decision traces irreproducible.
    """
    adj: dict[str, list[str]] = {}
    for edge in graph.edges:
        a, b = (
            (edge.source, edge.target) if direction == "downstream" else (edge.target, edge.source)
        )
        adj.setdefault(a, [])
        if b not in adj[a]:
            adj[a].append(b)
    for key in adj:
        adj[key].sort()
    return adj


def _edges_for(graph: ServiceGraph, hops: list[str], direction: PathDirection) -> list[GraphEdge]:
    """Resolve the ``GraphEdge`` objects a path traverses.

    Attached so a consumer gets the edge metadata — call rate, protocol,
    confidence — along the route, not just the service names. That is what lets
    downstream reasoning distinguish a hot path from an idle one.
    """
    found: list[GraphEdge] = []
    for a, b in pairwise(hops):
        src, tgt = (a, b) if direction == "downstream" else (b, a)
        edge = next((e for e in graph.edges if e.source == src and e.target == tgt), None)
        if edge is not None:
            found.append(edge)
    return found


def shortest_paths(
    graph: ServiceGraph,
    *,
    direction: PathDirection = "downstream",
    max_depth: int | None = None,
) -> list[DependencyPath]:
    """Shortest path from the graph root to every reachable service.

    BFS, so the first route reached is the shortest. ``visited`` is what makes it
    cycle-proof: a node already assigned a path is never re-expanded, so a loop
    back toward the root terminates rather than spinning.
    """
    root = graph.root
    adj = _adjacency(graph, direction)
    depth_cap = max_depth if max_depth is not None else len(graph.nodes) + 1

    paths: list[DependencyPath] = []
    visited = {root}
    queue: deque[list[str]] = deque([[root]])

    while queue:
        hops = queue.popleft()
        if len(hops) - 1 >= depth_cap:
            continue
        for neighbour in adj.get(hops[-1], []):
            if neighbour in visited:
                continue
            visited.add(neighbour)
            new_hops = [*hops, neighbour]
            paths.append(
                DependencyPath(
                    source=root,
                    target=neighbour,
                    hops=new_hops,
                    depth=len(new_hops) - 1,
                    direction=direction,
                    edges=_edges_for(graph, new_hops, direction),
                )
            )
            queue.append(new_hops)

    paths.sort(key=lambda p: (p.depth, p.target))
    return paths


def find_path(
    graph: ServiceGraph,
    source: str,
    target: str,
    *,
    direction: PathDirection = "downstream",
    max_depth: int | None = None,
) -> DependencyPath | None:
    """Shortest path between two arbitrary services, or ``None`` if unreachable.

    Unlike ``shortest_paths`` this does not assume the graph root is the origin,
    so it answers "how does the failing service reach the suspect?" for any pair
    present in the graph.
    """
    src = (source or "").strip().lower()
    tgt = (target or "").strip().lower()
    if not src or not tgt:
        return None
    if src == tgt:
        return DependencyPath(source=src, target=tgt, hops=[src], depth=0, direction=direction)

    adj = _adjacency(graph, direction)
    depth_cap = max_depth if max_depth is not None else len(graph.nodes) + 1

    visited = {src}
    queue: deque[list[str]] = deque([[src]])
    while queue:
        hops = queue.popleft()
        if len(hops) - 1 >= depth_cap:
            continue
        for neighbour in adj.get(hops[-1], []):
            if neighbour in visited:
                continue
            new_hops = [*hops, neighbour]
            if neighbour == tgt:
                return DependencyPath(
                    source=src,
                    target=tgt,
                    hops=new_hops,
                    depth=len(new_hops) - 1,
                    direction=direction,
                    edges=_edges_for(graph, new_hops, direction),
                )
            visited.add(neighbour)
            queue.append(new_hops)
    return None


def find_all_paths(
    graph: ServiceGraph,
    source: str,
    target: str,
    *,
    direction: PathDirection = "downstream",
    max_depth: int | None = None,
    max_paths: int | None = None,
) -> list[DependencyPath]:
    """Every simple (loop-free) route between two services, shortest first.

    Bounded by ``max_paths`` because enumerating all simple paths is exponential
    in a dense graph — an unbounded version is a denial-of-service on your own
    incident response. Multiple routes matter for blast radius: two independent
    paths to a suspect mean removing one does not isolate it.
    """
    src = (source or "").strip().lower()
    tgt = (target or "").strip().lower()
    if not src or not tgt or src == tgt:
        return []

    adj = _adjacency(graph, direction)
    depth_cap = max_depth if max_depth is not None else len(graph.nodes) + 1
    cap = max_paths if max_paths is not None else _MAX_PATHS

    results: list[DependencyPath] = []
    # Iterative DFS carrying its own visited set per branch, so a cycle cannot
    # produce an infinite path while distinct branches may still revisit a node.
    stack: list[tuple[list[str], set[str]]] = [([src], {src})]
    while stack and len(results) < cap:
        hops, seen = stack.pop()
        if len(hops) - 1 >= depth_cap:
            continue
        for neighbour in reversed(adj.get(hops[-1], [])):
            if neighbour in seen:
                continue
            new_hops = [*hops, neighbour]
            if neighbour == tgt:
                results.append(
                    DependencyPath(
                        source=src,
                        target=tgt,
                        hops=new_hops,
                        depth=len(new_hops) - 1,
                        direction=direction,
                        edges=_edges_for(graph, new_hops, direction),
                    )
                )
                if len(results) >= cap:
                    break
                continue
            stack.append((new_hops, seen | {neighbour}))

    results.sort(key=lambda p: (p.depth, p.hops))
    return results


def detect_cycles(graph: ServiceGraph, *, max_cycles: int | None = None) -> list[list[str]]:
    """Find dependency cycles, each returned as an ordered loop.

    Reported rather than merely survived: a loop such as ``a -> b -> a`` turns one
    fault into a cascade and breaks the "walk upstream until you find the origin"
    heuristic, so it is evidence in its own right.

    Each cycle is normalised to start at its lexicographically smallest member so
    the same loop discovered from different entry points deduplicates instead of
    appearing several times.
    """
    cap = max_cycles if max_cycles is not None else _MAX_CYCLES
    adj = _adjacency(graph, "downstream")
    nodes = sorted({n.service for n in graph.nodes} | set(adj))

    found: list[list[str]] = []
    seen_keys: set[tuple[str, ...]] = set()

    def normalise(loop: list[str]) -> tuple[str, ...]:
        if not loop:
            return ()
        i = loop.index(min(loop))
        return tuple(loop[i:] + loop[:i])

    for start in nodes:
        if len(found) >= cap:
            break
        # DFS tracking the active path; meeting a node already on the path is a
        # back-edge, i.e. a cycle.
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack and len(found) < cap:
            node, path = stack.pop()
            for neighbour in adj.get(node, []):
                if neighbour == start and len(path) > 1:
                    key = normalise(path)
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        found.append([*key])
                    continue
                if neighbour in path:
                    continue
                stack.append((neighbour, [*path, neighbour]))

    found.sort(key=lambda c: (len(c), c))
    return found


def analyze_paths(
    graph: ServiceGraph,
    *,
    max_depth: int | None = None,
    use_cache: bool = True,
) -> DependencyPaths:
    """Full path analysis for a graph's root: both directions plus cycles.

    Cached by (root, depth, edge count) with the same TTL machinery the resolver
    uses. Path finding is pure computation, but ``correlate()`` may ask for the
    same root repeatedly within one incident and the graph itself changes on
    deploy timescales, so a short TTL removes redundant work without serving a
    stale topology.

    ``truncated`` and ``coverage_note`` are inherited from the graph: a path set
    built from an incomplete edge set is itself incomplete, and dropping that
    caveat here would let a missing route read as a proven absence.
    """
    cache_key = f"paths:{graph.root}:{max_depth}:{len(graph.edges)}"
    if use_cache:
        hit = _cache.get(cache_key)
        if isinstance(hit, DependencyPaths):
            return hit

    downstream = shortest_paths(graph, direction="downstream", max_depth=max_depth)
    upstream = shortest_paths(graph, direction="upstream", max_depth=max_depth)
    cycles = detect_cycles(graph)

    result = DependencyPaths(
        root=graph.root,
        downstream=downstream,
        upstream=upstream,
        cycles=cycles,
        max_depth_reached=max([p.depth for p in downstream + upstream], default=0),
        truncated=graph.truncated,
        coverage_note=graph.coverage_note,
    )
    if use_cache:
        _cache.put(cache_key, result, _PATHS_TTL)
    return result
