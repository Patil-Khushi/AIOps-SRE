"""Tests for the service-topology graph builder.

These carry the whole verification burden for the graph: it is internal to
``aiops.tools.topology`` for now, so unlike the resolver it has no eval, no
dashboard and no downstream agent exercising it. Nothing else will notice if
BFS regresses.

The two properties worth the most attention:

- **Cycle safety.** Real service graphs loop. A recursive walk would not
  terminate; BFS with a visited set must.
- **Honest emptiness.** An empty ``upstream`` from a source that cannot see
  reverse edges means "unknown", not "nothing calls this service". Conflating
  those would tell the RCA agent a service has no callers when it may have many.
"""

from __future__ import annotations

import pytest

from aiops.tools.topology.graph import NodeHealth, ServiceGraph, ServiceMetadata
from aiops.tools.topology.graph_builder import build_graph

# (caller, callee, call_rate) — the shape otel.fetch_edges returns.
_DEMO_EDGES = [
    ("frontend", "checkout", 5.0),
    ("checkout", "payment", 3.0),
    ("checkout", "cart", 2.0),
    ("payment", "currency", 1.0),
    ("cart", "product-catalog", 4.0),
]


# ─── downstream traversal and depth ──────────────────────────────────────────


def test_direct_dependencies_are_depth_one():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel")
    assert g.node("payment").depth == 1
    assert g.node("cart").depth == 1


def test_transitive_dependencies_get_increasing_depth():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel")
    assert g.node("currency").depth == 2, "checkout -> payment -> currency"
    assert g.node("product-catalog").depth == 2, "checkout -> cart -> product-catalog"


def test_root_is_depth_zero_with_root_relation():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel")
    root = g.node("checkout")
    assert root.depth == 0
    assert root.relation == "root"


def test_downstream_list_matches_reachable_set():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel")
    assert set(g.downstream) == {"payment", "cart", "currency", "product-catalog"}


def test_shortest_path_wins_when_multiple_routes_exist():
    """A diamond must report the *shortest* distance, not whichever BFS hit last."""
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("a", "c", 1.0)]
    g = build_graph("a", edges, provider="otel")
    assert g.node("c").depth == 1, "direct a->c beats a->b->c"


# ─── upstream traversal ──────────────────────────────────────────────────────


def test_upstream_finds_callers_of_the_root():
    g = build_graph("payment", _DEMO_EDGES, provider="otel")
    assert "checkout" in g.upstream
    assert g.node("checkout").relation == "upstream"


def test_upstream_is_transitive():
    g = build_graph("currency", _DEMO_EDGES, provider="otel")
    assert set(g.upstream) >= {"payment", "checkout"}
    assert g.node("checkout").depth == 2, "currency <- payment <- checkout"


def test_node_reachable_both_ways_is_marked_both():
    """In a cycle a node is upstream *and* downstream of the root; it must appear
    once with relation 'both', not twice with contradictory relations."""
    edges = [("a", "b", 1.0), ("b", "a", 1.0)]
    g = build_graph("a", edges, provider="otel")
    b = g.node("b")
    assert b is not None
    assert b.relation == "both"
    assert len([n for n in g.nodes if n.service == "b"]) == 1


# ─── cycle safety ────────────────────────────────────────────────────────────


def test_simple_cycle_terminates():
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("c", "a", 1.0)]
    g = build_graph("a", edges, provider="otel", max_depth=10)
    assert {n.service for n in g.nodes} == {"a", "b", "c"}


def test_self_referential_edge_is_dropped():
    """A service calling its own interface is instrumentation noise; keeping it
    would make every service its own dependency."""
    g = build_graph("a", [("a", "a", 1.0), ("a", "b", 1.0)], provider="otel")
    assert g.downstream == ["b"]
    assert all(e.source != e.target for e in g.edges)


def test_large_cycle_does_not_hang():
    """Ring of 30 nodes with a depth cap — must complete, not recurse forever."""
    edges = [(f"s{i}", f"s{(i + 1) % 30}", 1.0) for i in range(30)]
    g = build_graph("s0", edges, provider="otel", max_depth=5)
    assert g.node("s0").relation == "root"
    assert g.truncated is True


# ─── caps and truncation ─────────────────────────────────────────────────────


def test_depth_cap_limits_traversal_and_sets_truncated():
    g = build_graph("frontend", _DEMO_EDGES, provider="otel", max_depth=1)
    assert g.downstream == ["checkout"]
    assert g.truncated is True, "more graph existed beyond the cap"


def test_depth_cap_not_marked_truncated_when_graph_actually_ends():
    """Reaching the natural end of the graph is not truncation."""
    g = build_graph("currency", _DEMO_EDGES, provider="otel", max_depth=3)
    assert g.downstream == []
    assert g.truncated is False


def test_node_cap_sets_truncated():
    edges = [("root", f"svc{i}", 1.0) for i in range(40)]
    g = build_graph("root", edges, provider="otel", max_nodes=10)
    assert g.truncated is True
    assert len(g.nodes) <= 12


def test_protocol_limited_source_marks_upstream_as_not_exhaustive():
    """Regression guard for a live-observed false claim.

    The OTel tier reads gRPC client metrics, so it sees every gRPC caller and no
    HTTP caller. A live run built a graph for ``checkout`` with ``upstream=[]``
    and ``truncated=False`` — asserting nothing calls checkout, while
    ``frontend`` was calling it over HTTP the whole time. That claim would have
    reached the RCA agent as evidence.
    """
    g = build_graph(
        "checkout", _DEMO_EDGES, provider="otel", coverage_note="gRPC only; HTTP invisible"
    )
    assert g.truncated is True
    assert g.coverage_note is not None
    assert g.upstream_complete is False, "an empty upstream here means 'none observed'"


def test_unlimited_source_reports_upstream_as_complete():
    """The flag must not fire spuriously — a full edge set is still trustworthy."""
    g = build_graph("payment", _DEMO_EDGES, provider="otel")
    assert g.truncated is False
    assert g.coverage_note is None
    assert g.upstream_complete is True


def test_build_service_graph_declares_its_grpc_limitation(monkeypatch):
    from aiops.tools.topology.providers import otel

    monkeypatch.setattr(otel, "fetch_edges", lambda: (_DEMO_EDGES, None))
    from aiops.tools.topology.graph_builder import build_service_graph

    g = build_service_graph("checkout")

    assert g.truncated is True
    assert "gRPC" in (g.coverage_note or "")
    assert g.upstream_complete is False, (
        "the OTel tier cannot see HTTP callers, so upstream must never read as exhaustive"
    )


def test_fetch_error_graph_also_records_why(monkeypatch):
    from aiops.tools.topology.providers import otel

    monkeypatch.setattr(otel, "fetch_edges", lambda: ([], "prometheus unreachable"))
    from aiops.tools.topology.graph_builder import build_service_graph

    g = build_service_graph("checkout")
    assert g.truncated is True
    assert "unavailable" in (g.coverage_note or "")


def test_unknown_reverse_edges_marks_truncated_and_empty_upstream():
    """The distinction that matters: upstream is empty because it is UNKNOWN.

    A per-service provider cannot see incoming calls. Reporting an empty
    upstream without the truncated flag would read as 'nothing calls this
    service' — a confident claim the data does not support.
    """
    g = build_graph("checkout", _DEMO_EDGES, provider="cmdb", reverse_known=False)
    assert g.upstream == []
    assert g.truncated is True


# ─── edges ───────────────────────────────────────────────────────────────────


def test_edges_are_directed_caller_to_callee():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel")
    e = next(e for e in g.edges if e.source == "checkout" and e.target == "payment")
    assert e.metadata.call_rate == 3.0


def test_edges_outside_the_traversal_are_excluded():
    """An edge pointing at a node the depth cap excluded would dangle."""
    g = build_graph("checkout", _DEMO_EDGES, provider="otel", max_depth=1)
    names = {n.service for n in g.nodes}
    for e in g.edges:
        assert e.source in names and e.target in names


def test_edge_metadata_records_provider_and_protocol():
    g = build_graph("checkout", _DEMO_EDGES, provider="otel", protocol="grpc")
    e = g.edges[0]
    assert e.metadata.provider == "otel"
    assert e.metadata.protocol == "grpc"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("otel", 0.9), ("snow", 0.7), ("cmdb", 0.5), ("mock", 0.3)],
)
def test_confidence_reflects_source_strength(provider, expected):
    """Observed traffic is stronger evidence than a declared relationship, which
    beats a static table. Consumers can weight suspects by this."""
    g = build_graph("checkout", _DEMO_EDGES, provider=provider)
    assert g.edges[0].metadata.confidence == expected


def test_unknown_provider_leaves_confidence_none():
    g = build_graph("checkout", _DEMO_EDGES, provider="something-new")
    assert g.edges[0].metadata.confidence is None


# ─── robustness ──────────────────────────────────────────────────────────────


def test_malformed_edges_are_skipped_not_fatal():
    edges = [("a", "b", 1.0), ("bad",), None, ("", "c", 1.0), ("d", "", 1.0)]
    g = build_graph("a", edges, provider="otel")  # type: ignore[arg-type]
    assert g.downstream == ["b"]


def test_empty_edge_list_yields_root_only_graph():
    g = build_graph("lonely", [], provider="otel")
    assert [n.service for n in g.nodes] == ["lonely"]
    assert g.downstream == [] and g.upstream == []
    assert g.truncated is False


def test_names_are_normalized():
    g = build_graph("  CheckOut ", [("checkout", "PAYMENT", 1.0)], provider="otel")
    assert g.root == "checkout"
    assert g.downstream == ["payment"]


def test_root_absent_from_edges_still_produces_valid_graph():
    g = build_graph("orphan", _DEMO_EDGES, provider="otel")
    assert [n.service for n in g.nodes] == ["orphan"]


# ─── model contract ──────────────────────────────────────────────────────────


def test_graph_is_json_serializable():
    """It must survive a JSON boundary intact — that is the whole reason these
    are Pydantic models rather than dataclasses."""
    g = build_graph("checkout", _DEMO_EDGES, provider="otel", protocol="grpc")
    dumped = g.model_dump(mode="json")

    assert dumped["root"] == "checkout"
    assert isinstance(dumped["nodes"], list)
    assert isinstance(dumped["edges"], list)
    assert dumped["edges"][0]["metadata"]["provider"] == "otel"
    # Round-trips back into the model.
    assert ServiceGraph.model_validate(dumped).root == "checkout"


def test_node_health_defaults_to_unknown_never_healthy():
    """If the health source is unreachable the honest answer is 'unknown'.
    Defaulting to healthy would let a dead dependency look fine."""
    assert NodeHealth().status == "unknown"


def test_service_metadata_fields_default_to_none():
    m = ServiceMetadata()
    assert (m.namespace, m.cluster, m.version, m.environment) == (None, None, None, None)
    assert m.labels == {}


def test_graph_models_reject_unknown_fields():
    with pytest.raises(Exception):
        ServiceGraph(root="x", bogus_field=1)


def test_max_depth_reached_reports_the_deepest_node():
    g = build_graph("frontend", _DEMO_EDGES, provider="otel", max_depth=5)
    assert g.max_depth_reached == 3, "frontend -> checkout -> payment -> currency"


# ─── I/O wrapper ─────────────────────────────────────────────────────────────


def test_build_service_graph_degrades_to_empty_graph_on_fetch_error(monkeypatch):
    """A caller that cannot get a graph should carry on without one, not crash."""
    from aiops.tools.topology.providers import otel

    monkeypatch.setattr(otel, "fetch_edges", lambda: ([], "prometheus unreachable"))
    from aiops.tools.topology.graph_builder import build_service_graph

    g = build_service_graph("checkout")
    assert g.nodes == []
    assert g.truncated is True


def test_build_service_graph_contains_provider_exception(monkeypatch):
    from aiops.tools.topology.providers import otel

    def _boom():
        raise RuntimeError("exploded")

    monkeypatch.setattr(otel, "fetch_edges", _boom)
    from aiops.tools.topology.graph_builder import build_service_graph

    g = build_service_graph("checkout")
    assert g.truncated is True


def test_build_service_graph_builds_from_fetched_edges(monkeypatch):
    from aiops.tools.topology.providers import otel

    monkeypatch.setattr(otel, "fetch_edges", lambda: (_DEMO_EDGES, None))
    from aiops.tools.topology.graph_builder import build_service_graph

    g = build_service_graph("checkout")
    assert set(g.downstream) == {"payment", "cart", "currency", "product-catalog"}
    assert g.provider == "otel"
    assert g.edges[0].metadata.protocol == "grpc"
