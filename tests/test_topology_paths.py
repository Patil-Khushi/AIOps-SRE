"""Tests for multi-hop dependency path discovery (Phase 3).

Phase 2 established *how far* a service is; this layer establishes *by which
route*. The route is the part an incident responder can act on:
``checkout -> payment -> currency`` names the services to inspect and the order a
fault would propagate in, where "currency is 2 hops away" names neither.

Three properties get the most coverage because they are the ones that fail
silently:

- **Determinism.** BFS over an unordered adjacency map returns different (equally
  valid) shortest paths run to run, which makes a decision trace irreproducible.
- **Cycle termination.** A loop must end the walk, not extend it forever — and
  must be *reported*, since a dependency cycle defeats "walk upstream to the
  origin" reasoning.
- **Inherited incompleteness.** A path set built from a gRPC-only graph is itself
  gRPC-only. If that caveat is dropped, a missing route reads as a proven absence.
"""

from __future__ import annotations

import pytest

from aiops.tools.topology import cache as topo_cache
from aiops.tools.topology.graph_builder import build_graph
from aiops.tools.topology.paths import (
    DependencyPath,
    analyze_paths,
    detect_cycles,
    find_all_paths,
    find_path,
    shortest_paths,
)

# The chain from the Phase 3 brief: frontend -> checkout -> payment -> redis -> db
_CHAIN = [
    ("frontend", "checkout", 5.0),
    ("checkout", "payment", 3.0),
    ("payment", "redis", 2.0),
    ("redis", "database", 1.0),
]


def _graph(root: str, edges=None, **kw):
    return build_graph(root, edges if edges is not None else _CHAIN, provider="otel", **kw)


@pytest.fixture(autouse=True)
def _clean_cache():
    topo_cache.clear()
    yield
    topo_cache.clear()


# ─── complete dependency chain ───────────────────────────────────────────────


def test_full_chain_is_discovered_end_to_end():
    """The brief's example: the whole 4-hop chain must be recovered in order."""
    g = _graph("frontend", max_depth=10)
    paths = shortest_paths(g)

    db = next(p for p in paths if p.target == "database")
    assert db.hops == ["frontend", "checkout", "payment", "redis", "database"]
    assert db.depth == 4


def test_chain_renders_readably():
    """This string is what lands in a decision trace or an LLM prompt."""
    g = _graph("frontend", max_depth=10)
    db = next(p for p in shortest_paths(g) if p.target == "database")
    assert db.chain == "frontend -> checkout -> payment -> redis -> database"


def test_intermediate_hops_each_get_their_own_path():
    g = _graph("frontend", max_depth=10)
    by_target = {p.target: p for p in shortest_paths(g)}

    assert by_target["checkout"].depth == 1
    assert by_target["payment"].depth == 2
    assert by_target["redis"].depth == 3
    assert by_target["database"].depth == 4


# ─── shortest path ───────────────────────────────────────────────────────────


def test_shortest_path_prefers_the_direct_route():
    """A shortcut must win over the long way round."""
    edges = [*_CHAIN, ("frontend", "database", 1.0)]
    g = _graph("frontend", edges, max_depth=10)

    db = next(p for p in shortest_paths(g) if p.target == "database")
    assert db.hops == ["frontend", "database"]
    assert db.depth == 1


def test_find_path_between_two_arbitrary_services():
    """Not everything of interest starts at the graph root — the failing service
    and the suspect are usually both mid-graph."""
    g = _graph("frontend", max_depth=10)
    p = find_path(g, "checkout", "database")

    assert p is not None
    assert p.hops == ["checkout", "payment", "redis", "database"]
    assert p.depth == 3


def test_find_path_returns_none_when_unreachable():
    g = _graph("frontend", max_depth=10)
    assert find_path(g, "database", "frontend") is None, "no downstream route back up"


def test_find_path_to_self_is_zero_depth():
    g = _graph("frontend", max_depth=10)
    p = find_path(g, "checkout", "checkout")
    assert p is not None and p.depth == 0 and p.hops == ["checkout"]


def test_find_path_normalizes_names():
    g = _graph("frontend", max_depth=10)
    p = find_path(g, "  CheckOut ", "PAYMENT")
    assert p is not None and p.hops == ["checkout", "payment"]


def test_find_path_rejects_blank_input():
    g = _graph("frontend", max_depth=10)
    assert find_path(g, "", "payment") is None
    assert find_path(g, "checkout", "  ") is None


def test_shortest_path_is_deterministic_across_runs():
    """Two equal-length routes must always yield the same one, or the decision
    trace changes between identical incidents."""
    edges = [("a", "b1", 1.0), ("a", "b2", 1.0), ("b1", "c", 1.0), ("b2", "c", 1.0)]
    first = find_path(_graph("a", edges, max_depth=10), "a", "c")
    for _ in range(5):
        again = find_path(_graph("a", edges, max_depth=10), "a", "c")
        assert again.hops == first.hops


# ─── downstream vs upstream ──────────────────────────────────────────────────


def test_downstream_paths_follow_call_direction():
    g = _graph("checkout", max_depth=10)
    targets = {p.target for p in shortest_paths(g, direction="downstream")}
    assert targets == {"payment", "redis", "database"}


def test_upstream_paths_invert_the_direction():
    g = _graph("payment", max_depth=10)
    up = shortest_paths(g, direction="upstream")
    by_target = {p.target: p for p in up}

    assert by_target["checkout"].hops == ["payment", "checkout"]
    assert by_target["frontend"].hops == ["payment", "checkout", "frontend"]
    assert all(p.direction == "upstream" for p in up)


def test_upstream_path_of_the_deepest_node_reaches_the_entry_point():
    g = _graph("database", max_depth=10)
    p = next(p for p in shortest_paths(g, direction="upstream") if p.target == "frontend")
    assert p.hops == ["database", "redis", "payment", "checkout", "frontend"]


# ─── path depth / max traversal depth ────────────────────────────────────────


def test_max_depth_limits_path_length():
    g = _graph("frontend", max_depth=10)
    paths = shortest_paths(g, max_depth=2)

    assert {p.target for p in paths} == {"checkout", "payment"}
    assert all(p.depth <= 2 for p in paths)


def test_find_path_honours_max_depth():
    g = _graph("frontend", max_depth=10)
    assert find_path(g, "frontend", "database", max_depth=2) is None
    assert find_path(g, "frontend", "database", max_depth=4) is not None


def test_max_depth_reached_is_reported():
    analysis = analyze_paths(_graph("frontend", max_depth=10))
    assert analysis.max_depth_reached == 4


# ─── cycle detection ─────────────────────────────────────────────────────────


def test_simple_cycle_is_detected_and_reported():
    """Loops are evidence, not just a hazard to survive: a cycle turns one fault
    into a cascade and breaks upstream-walking root-cause reasoning."""
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("c", "a", 1.0)]
    cycles = detect_cycles(_graph("a", edges, max_depth=10))

    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b", "c"}


def test_two_node_cycle_is_detected():
    cycles = detect_cycles(_graph("a", [("a", "b", 1.0), ("b", "a", 1.0)], max_depth=10))
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b"}


def test_same_cycle_is_not_reported_once_per_entry_point():
    """Normalising each loop to start at its smallest member is what stops
    a -> b -> c -> a appearing three times."""
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("c", "a", 1.0)]
    assert len(detect_cycles(_graph("a", edges, max_depth=10))) == 1


def test_acyclic_graph_reports_no_cycles():
    assert detect_cycles(_graph("frontend", max_depth=10)) == []


def test_path_traversal_terminates_on_a_cycle():
    """The termination guarantee: a loop must end the walk, not extend it."""
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("c", "a", 1.0), ("c", "d", 1.0)]
    paths = shortest_paths(_graph("a", edges, max_depth=10))

    assert {p.target for p in paths} == {"b", "c", "d"}
    for p in paths:
        assert len(p.hops) == len(set(p.hops)), f"path revisits a node: {p.hops}"


def test_large_ring_does_not_hang():
    edges = [(f"s{i}", f"s{(i + 1) % 40}", 1.0) for i in range(40)]
    g = build_graph("s0", edges, provider="otel", max_depth=50, max_nodes=100)
    assert detect_cycles(g, max_cycles=3) != []
    assert shortest_paths(g, max_depth=5) != []


def test_cycle_reporting_is_bounded():
    """Unbounded cycle enumeration on a dense graph is a self-inflicted DoS."""
    edges = [(f"n{i}", f"n{j}", 1.0) for i in range(6) for j in range(6) if i != j]
    assert (
        len(detect_cycles(build_graph("n0", edges, provider="otel", max_depth=8), max_cycles=4))
        <= 4
    )


# ─── multiple routes ─────────────────────────────────────────────────────────


def test_all_paths_finds_every_independent_route():
    """Two routes to a suspect mean cutting one does not isolate it — that is
    blast-radius information a single shortest path hides."""
    edges = [
        ("a", "b", 1.0),
        ("b", "d", 1.0),
        ("a", "c", 1.0),
        ("c", "d", 1.0),
    ]
    paths = find_all_paths(_graph("a", edges, max_depth=10), "a", "d")

    assert len(paths) == 2
    assert {tuple(p.hops) for p in paths} == {("a", "b", "d"), ("a", "c", "d")}


def test_all_paths_are_loop_free():
    edges = [("a", "b", 1.0), ("b", "a", 1.0), ("b", "c", 1.0)]
    for p in find_all_paths(_graph("a", edges, max_depth=10), "a", "c"):
        assert len(p.hops) == len(set(p.hops))


def test_all_paths_is_capped():
    edges = [(f"n{i}", f"n{j}", 1.0) for i in range(7) for j in range(7) if i != j]
    paths = find_all_paths(
        build_graph("n0", edges, provider="otel", max_depth=8), "n0", "n6", max_paths=3
    )
    assert len(paths) <= 3


def test_all_paths_sorted_shortest_first():
    edges = [("a", "b", 1.0), ("b", "d", 1.0), ("a", "d", 1.0)]
    paths = find_all_paths(_graph("a", edges, max_depth=10), "a", "d")
    assert paths[0].depth <= paths[-1].depth


# ─── edge metadata on paths ──────────────────────────────────────────────────


def test_path_carries_the_edges_it_traverses():
    """Rates and protocol along the route distinguish a hot path from an idle one."""
    g = _graph("frontend", max_depth=10)
    p = find_path(g, "frontend", "payment")

    assert len(p.edges) == 2
    assert p.edges[0].source == "frontend" and p.edges[0].target == "checkout"
    assert p.edges[0].metadata.call_rate == 5.0


def test_upstream_path_edges_keep_their_true_direction():
    """Traversing upstream must not invert the recorded edge: the call still goes
    checkout -> payment even when we walked it backwards."""
    g = _graph("payment", max_depth=10)
    p = next(x for x in shortest_paths(g, direction="upstream") if x.target == "checkout")

    assert p.hops == ["payment", "checkout"]
    assert p.edges[0].source == "checkout", "edge direction is a fact, not a walk artifact"
    assert p.edges[0].target == "payment"


# ─── analyze_paths, caching, inherited incompleteness ────────────────────────


def test_analyze_paths_returns_both_directions_and_cycles():
    a = analyze_paths(_graph("checkout", max_depth=10))

    assert {p.target for p in a.downstream} == {"payment", "redis", "database"}
    assert {p.target for p in a.upstream} == {"frontend"}
    assert a.cycles == []
    assert a.root == "checkout"


def test_analyze_paths_convenience_lookups():
    a = analyze_paths(_graph("checkout", max_depth=10))
    assert a.to("database").depth == 3
    assert a.frm("frontend").depth == 1
    assert a.to("nonexistent") is None


def test_analyze_paths_is_cached():
    g = _graph("checkout", max_depth=10)
    first = analyze_paths(g)
    second = analyze_paths(g)
    assert second is first, "identical analysis should come from cache"


def test_cache_can_be_bypassed():
    g = _graph("checkout", max_depth=10)
    first = analyze_paths(g)
    second = analyze_paths(g, use_cache=False)
    assert second is not first


def test_cache_key_distinguishes_depth():
    g = _graph("frontend", max_depth=10)
    shallow = analyze_paths(g, max_depth=1)
    deep = analyze_paths(g, max_depth=4)
    assert len(deep.downstream) > len(shallow.downstream)


def test_incomplete_coverage_is_inherited_by_the_path_set():
    """A path set built from a gRPC-only graph is itself gRPC-only. Losing that
    caveat would let an absent route read as a proven absence."""
    g = build_graph("checkout", _CHAIN, provider="otel", max_depth=10, coverage_note="gRPC only")
    a = analyze_paths(g)

    assert a.truncated is True
    assert a.coverage_note == "gRPC only"


def test_paths_are_json_serializable():
    a = analyze_paths(_graph("frontend", max_depth=10))
    dumped = a.model_dump(mode="json")

    assert dumped["root"] == "frontend"
    assert isinstance(dumped["downstream"], list)
    assert dumped["downstream"][0]["hops"]


def test_path_model_rejects_unknown_fields():
    with pytest.raises(Exception):
        DependencyPath(source="a", target="b", bogus=1)


# ─── backward compatibility ──────────────────────────────────────────────────


def test_service_graph_is_unchanged_by_path_discovery():
    """Phase 3 is additive: computing paths must not mutate the graph it read."""
    g = _graph("frontend", max_depth=10)
    before = g.model_dump(mode="json")

    analyze_paths(g)
    find_all_paths(g, "frontend", "database")
    detect_cycles(g)

    assert g.model_dump(mode="json") == before


def test_empty_graph_yields_empty_analysis():
    g = build_graph("lonely", [], provider="otel")
    a = analyze_paths(g)
    assert a.downstream == [] and a.upstream == [] and a.cycles == []
