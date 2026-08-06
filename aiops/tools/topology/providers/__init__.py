"""Topology provider implementations.

Each module here supplies one tier of the resolution chain. Providers are plain
classes satisfying ``aiops.tools.topology.base.TopologyProvider`` — no base class
to inherit, no registration side effect on import. The resolver owns which tiers
exist and in what order, so importing a provider module never changes behaviour
on its own.
"""
