"""Pluggable service-topology discovery.

Answers "which services does X depend on?" from the best source available,
falling back down a priority chain rather than depending on any single backend
being reachable (CLAUDE.md principle #1 — every external dependency behind a
thin internal interface, with documented alternatives).

Usage::

    from aiops.tools.topology import resolve

    resolution = resolve("checkout")
    resolution.dependencies      # ['cart', 'currency', 'payment', ...]
    resolution.winning_provider  # 'cmdb'
    resolution.attempts          # per-tier provenance for the decision trace

Default chain is ``cmdb,mock`` — behaviour-identical to what RA-007 did before
this package existed. Override with ``AIOPS_TOPOLOGY_PROVIDERS``.

This package does **not** register anything with the tool registry, and does not
touch the ``itsm.cmdb.dependencies`` capability that ``alert_triage``,
``notification_assembler`` and ``log_correlation`` all consume. Importing it is
side-effect free.
"""

from aiops.tools.topology.base import (
    HealthStatus,
    ProviderStatus,
    TopologyProvider,
    TopologyResult,
)
from aiops.tools.topology.graph import (
    EdgeMetadata,
    GraphEdge,
    GraphNode,
    NodeHealth,
    ServiceGraph,
    ServiceMetadata,
)
from aiops.tools.topology.graph_builder import build_graph, build_service_graph
from aiops.tools.topology.paths import (
    DependencyPath,
    DependencyPaths,
    analyze_paths,
    detect_cycles,
    find_all_paths,
    find_path,
    shortest_paths,
)
from aiops.tools.topology.resolver import (
    TopologyResolution,
    register_provider,
    reset_for_tests,
    resolve,
)

__all__ = [
    "DependencyPath",
    "DependencyPaths",
    "EdgeMetadata",
    "GraphEdge",
    "GraphNode",
    "HealthStatus",
    "NodeHealth",
    "ProviderStatus",
    "ServiceGraph",
    "ServiceMetadata",
    "TopologyProvider",
    "TopologyResolution",
    "TopologyResult",
    "analyze_paths",
    "build_graph",
    "build_service_graph",
    "detect_cycles",
    "find_all_paths",
    "find_path",
    "register_provider",
    "reset_for_tests",
    "resolve",
    "shortest_paths",
]
