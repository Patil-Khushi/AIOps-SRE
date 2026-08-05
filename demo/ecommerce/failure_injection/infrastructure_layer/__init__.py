"""Infrastructure-layer failure injection - real chaos engineering.

This layer injects failures at the infrastructure level using:
- tc (traffic control) for network chaos (latency, packet loss)
- kubectl for pod operations (kill pods)
- stress-ng for resource stress (CPU, memory)
- iptables for network rules
- cgroups for resource limits
- dd for disk filling
- DNS poisoning for DNS failures

Each failure module provides inject() and recover() callables
that implement real infrastructure-level chaos.
"""

from . import (
    cpu_spike,
    dependency_failure,
    disk_full,
    dns_failure,
    memory_exhaust,
    network_latency,
    packet_loss,
    pool_exhaustion,
    service_timeout,
)

__all__ = [
    "cpu_spike",
    "dependency_failure",
    "disk_full",
    "dns_failure",
    "memory_exhaust",
    "network_latency",
    "packet_loss",
    "pool_exhaustion",
    "service_timeout",
]
