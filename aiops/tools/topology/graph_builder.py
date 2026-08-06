"""Build a ``ServiceGraph`` from an edge list.

Two layers, deliberately separate:

- ``build_graph`` is a **pure function** over an explicit edge list. No I/O, no
  registry, no clock. That makes cycles, diamonds, depth caps and truncation
  testable exhaustively without a cluster — which matters here, because the
  graph is internal-only for now and its unit tests carry the entire
  verification burden.
- ``build_service_graph`` is the thin I/O wrapper that fetches edges from the
  configured source and calls the pure function.

Traversal is breadth-first with a visited set, in both directions from the
root. BFS rather than recursive DFS for two reasons: real service graphs
contain cycles (``frontend -> checkout -> ... -> frontend`` is entirely
plausible) and a recursive walk would not terminate on one, and BFS naturally
yields the *shortest* hop distance, which is the only sensible reading of
"dependency depth" when several paths exist.
"""

from __future__ import annotations

import logging
import os
from collections import deque

from aiops.tools.topology.base import ProviderStatus
from aiops.tools.topology.graph import (
    EdgeMetadata,
    GraphEdge,
    GraphNode,
    ServiceGraph,
)

logger = logging.getLogger(__name__)

_MAX_DEPTH = int(os.environ.get("AIOPS_TOPOLOGY_GRAPH_MAX_DEPTH", "3"))
_MAX_NODES = int(os.environ.get("AIOPS_TOPOLOGY_GRAPH_MAX_NODES", "50"))

# How much to trust an edge by the kind of source that asserted it. Observed
# traffic beats a declared relationship, which beats a static demo table.
_OTEL_COVERAGE_NOTE = (
    "gRPC client metrics only (rpc_client_*); HTTP callers are not observable, "
    "so upstream is 'none observed' rather than 'none'"
)

_PROVIDER_CONFIDENCE = {
    "otel": 0.9,
    "snow": 0.7,
    "cmdb": 0.5,
    "mock": 0.3,
}


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def build_graph(
    root: str,
    edges: list[tuple[str, str, float]],
    *,
    provider: str = "unknown",
    protocol: str | None = None,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    reverse_known: bool = True,
    coverage_note: str | None = None,
    root_answered: bool = True,
) -> ServiceGraph:
    """Assemble a root-centred graph from ``(caller, callee, call_rate)`` triples.

    ``reverse_known`` records whether the edge source could see *incoming* calls
    at all. A per-service source that only answers "what does X depend on"
    cannot populate ``upstream``; passing ``False`` marks the graph ``truncated``
    so an empty ``upstream`` is never mistaken for "nothing calls the root".

    ``coverage_note`` covers the subtler case: the source *can* see reverse
    edges, but only across part of the traffic. The OTel tier reads gRPC client
    metrics, so it finds every gRPC caller and no HTTP caller — which is how
    ``checkout`` came back with ``upstream=[]`` in a live run despite
    ``frontend`` calling it over HTTP. Supplying a note marks the graph
    ``truncated`` and records the limitation, so downstream reasoning can tell
    "no callers" from "no callers *we can see*".

    Never raises on malformed input: unusable edges are skipped, because a graph
    missing one edge is degraded evidence while a raised exception would take
    down the correlation that asked for it.
    """
    depth_cap = _MAX_DEPTH if max_depth is None else max_depth
    node_cap = _MAX_NODES if max_nodes is None else max_nodes
    root_key = _normalize(root)

    # Adjacency in both directions, built once so each BFS is O(E).
    out_adj: dict[str, list[tuple[str, float]]] = {}
    in_adj: dict[str, list[tuple[str, float]]] = {}
    clean_edges: list[tuple[str, str, float]] = []
    for raw in edges:
        try:
            caller, callee, rate = raw
        except (TypeError, ValueError):
            logger.debug("graph: skipping malformed edge %r", raw)
            continue
        c, t = _normalize(caller), _normalize(callee)
        if not c or not t or c == t:
            # Self-edges are instrumentation artifacts, not dependencies.
            continue
        clean_edges.append((c, t, rate))
        out_adj.setdefault(c, []).append((t, rate))
        in_adj.setdefault(t, []).append((c, rate))

    # Either kind of incompleteness makes the node/edge set non-exhaustive, so
    # both must surface as ``truncated`` — that flag is what stops a consumer
    # reading an empty ``upstream`` as a confident "nothing calls this".
    truncated = (not reverse_known) or coverage_note is not None

    def walk(adjacency: dict[str, list[tuple[str, float]]]) -> dict[str, int]:
        """BFS from the root, returning ``service -> shortest depth``.

        ``seen`` is what makes this cycle-safe: a node already assigned a depth
        is never re-enqueued, so a loop back to the root terminates instead of
        spinning.
        """
        nonlocal truncated
        depths: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(root_key, 0)])
        seen = {root_key}
        while queue:
            current, depth = queue.popleft()
            if depth > 0:
                depths[current] = depth
            if depth >= depth_cap:
                if adjacency.get(current):
                    truncated = True
                continue
            for neighbour, _rate in adjacency.get(current, []):
                if neighbour in seen:
                    continue
                if len(seen) >= node_cap:
                    truncated = True
                    return depths
                seen.add(neighbour)
                queue.append((neighbour, depth + 1))
        return depths

    downstream_depths = walk(out_adj)
    upstream_depths = walk(in_adj) if reverse_known else {}

    # Merge into one node per service. A service reachable both ways is a single
    # node with relation "both" and the shorter of the two distances.
    nodes: list[GraphNode] = [GraphNode(service=root_key, depth=0, relation="root")]
    for service in sorted(set(downstream_depths) | set(upstream_depths)):
        if service == root_key:
            continue
        d_down = downstream_depths.get(service)
        d_up = upstream_depths.get(service)
        if d_down is not None and d_up is not None:
            relation, depth = "both", min(d_down, d_up)
        elif d_down is not None:
            relation, depth = "downstream", d_down
        else:
            relation, depth = "upstream", d_up or 1
        nodes.append(GraphNode(service=service, depth=depth, relation=relation))

    in_graph = {n.service for n in nodes}
    confidence = _PROVIDER_CONFIDENCE.get(provider)
    graph_edges = [
        GraphEdge(
            source=c,
            target=t,
            metadata=EdgeMetadata(
                provider=provider,
                protocol=protocol,
                call_rate=rate,
                confidence=confidence,
            ),
        )
        # Only edges whose endpoints both survived the traversal caps: an edge
        # pointing at a node excluded by the depth limit would dangle.
        for c, t, rate in clean_edges
        if c in in_graph and t in in_graph
    ]

    return ServiceGraph(
        root=root_key,
        nodes=nodes,
        edges=graph_edges,
        downstream=sorted(downstream_depths),
        upstream=sorted(upstream_depths),
        max_depth_reached=max([n.depth for n in nodes], default=0),
        truncated=truncated,
        coverage_note=coverage_note,
        provider=provider,
        root_answered=root_answered,
    )


def build_service_graph(service: str, *, max_depth: int | None = None) -> ServiceGraph:
    """Fetch edges from the OTel source and build a graph for ``service``.

    Uses the OTel tier because it is the only source that returns the *whole*
    edge set in one query — the property that makes an N-node graph cost one
    round-trip instead of N — and the only one with observed call rates. If it
    is unavailable the result is an empty, ``truncated`` graph rather than an
    exception: a caller that cannot get a graph should continue without one.
    """
    from aiops.tools.topology.providers import otel

    try:
        edges, error = otel.fetch_edges()
    except Exception as exc:  # provider contract says it shouldn't, but defend
        logger.warning("graph: edge fetch raised for %r: %s", service, exc)
        edges, error = [], f"{type(exc).__name__}"

    if error is not None:
        logger.warning("graph: edge fetch failed for %r: %s", service, error)
        return ServiceGraph(
            root=_normalize(service),
            truncated=True,
            coverage_note=f"edge source unavailable: {error}",
            provider="otel",
        )

    return build_graph(
        service,
        edges,
        provider="otel",
        protocol="grpc",
        max_depth=max_depth,
        # The OTel tier derives edges from rpc_client_* metrics, which cover gRPC
        # only. HTTP callers are invisible to it: a live run had `frontend` call
        # `checkout` over HTTP and the graph still reported `upstream=[]`. Declare
        # the limitation so that empty list reads as "none observed", not "none".
        coverage_note=_OTEL_COVERAGE_NOTE,
    )


def build_resolved_graph(
    service: str,
    *,
    max_depth: int | None = None,
    max_nodes: int | None = None,
) -> ServiceGraph:
    """Assemble a multi-hop graph by walking the active provider chain.

    The counterpart to :func:`build_service_graph`. That one needs a source that
    can hand over every edge in one query, which only the OTel tier does. This one
    works with the *per-service* tiers (CMDB, ServiceNow, Kubernetes, mock) by
    resolving one node at a time and following what comes back — so a graph is
    available on the default ``cmdb,mock`` chain, where OTel is off.

    The trade is round-trips: N nodes cost N resolutions rather than one. That is
    free for the in-process tiers and emphatically not for a remote one, hence
    ``max_nodes``. Resolutions are cached by the resolver, so re-walking a shared
    subtree is cheap.

    Per-service tiers answer "what does X call", never "what calls X", so
    ``reverse_known=False``: an empty ``upstream`` here means *unobservable*, not
    absent.
    """
    from aiops.tools.topology.resolver import resolve

    depth_cap = _MAX_DEPTH if max_depth is None else max_depth
    node_cap = _MAX_NODES if max_nodes is None else max_nodes

    root = _normalize(service)
    edges: list[tuple[str, str, float]] = []
    seen: set[str] = {root}
    providers: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    hit_cap = False
    # Whether any tier actually answered for the ROOT specifically. An edgeless
    # graph is only readable with this: "a tier answered and this is a leaf" and
    # "nothing could answer, so dependencies are unknown" are opposite facts that
    # both produce zero edges. Tracked for the root alone because that is the
    # service the verdict is about — a deeper node failing to resolve truncates the
    # graph, which ``hit_cap``/``coverage_note`` already cover.
    root_answered = False

    while queue:
        current, depth = queue.popleft()
        if depth >= depth_cap:
            continue
        try:
            res = resolve(current)
        except Exception as exc:  # resolver is defensive, but never let a walk raise
            logger.warning("graph: resolve raised for %r during walk: %s", current, exc)
            continue
        if current == root:
            # What counts as an answer *about this service*:
            #   RESOLVED                   → dependencies found
            #   EMPTY with payload_present → a record exists and lists none
            # Both are positive statements. Deliberately excluded:
            #   EMPTY without payload_present → "I have no record of this service"
            #   UNAVAILABLE / FAILED          → never got a usable reply
            #
            # The payload_present half is load-bearing. Counting a bare EMPTY as an
            # answer let a service no source had ever heard of be reported as a
            # positive "leaf service with no dependencies" — the same
            # unknown-treated-as-absent conflation this flag exists to prevent, just
            # one level further in.
            root_answered = any(
                a.status is ProviderStatus.RESOLVED
                or (a.status is ProviderStatus.EMPTY and a.payload_present)
                for a in res.attempts
            )
        if res.winning_provider:
            providers.add(res.winning_provider)
        for dep in res.dependencies:
            target = _normalize(dep)
            if target == current:
                continue  # a service listed as its own dependency is not an edge
            # Record the edge even when the target is already known: that is how
            # diamonds and shared dependencies survive into the picture.
            edges.append((current, target, 0.0))
            if target in seen:
                continue
            if len(seen) >= node_cap:
                hit_cap = True
                continue
            seen.add(target)
            queue.append((target, depth + 1))

    if hit_cap:
        logger.warning("graph: node cap %d reached walking %r; graph truncated", node_cap, root)

    note = (
        "per-service resolution: upstream callers are not observable from this "
        "source, so an empty upstream means unknown, not none"
    )
    if hit_cap:
        note = f"{note}; node cap {node_cap} reached — graph is partial"
    if not root_answered:
        note = (
            f"no topology tier answered for {root!r}, so its dependencies are "
            f"unknown rather than absent; {note}"
        )

    return build_graph(
        root,
        edges,
        provider=",".join(sorted(providers)) or "none",
        max_depth=depth_cap,
        max_nodes=node_cap,
        reverse_known=False,
        coverage_note=note,
        root_answered=root_answered,
    )
