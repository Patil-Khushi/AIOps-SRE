"""Order Service — memory exhaustion driven from outside the application.

Distinct from ``order_service.memory_leak_oom``, which sets INJECT_MEMORY_LEAK
and lets the app grow its own heap. Here the app is an innocent bystander: an
external process holds pages resident until the cgroup limit forces an OOMKill,
so the app's own memory profile stays clean and the RCA has to come from pod
state rather than application logs.
"""

from .._base import Failure, InjectionLayer
from . import _infra_backend

# order-service's container limit is 256Mi (k8s/20-app.yaml), and the app itself
# sits near its 128Mi request, so ~200MB of extra resident pages reliably crosses
# the limit. Larger is not better: the allocation would abort before the pages
# were touched, producing no memory pressure at all.
#
# Caveat worth knowing before reading the L1 signal: the cgroup OOM killer picks
# by oom_score, and the hog has the largest RSS, so it is usually the hog that
# dies rather than the container's main process. That yields real memory pressure
# and a real kernel OOM event, but *not* necessarily a pod-level
# lastState.terminated.reason=OOMKilled. For a guaranteed container-level
# OOMKilled, use the app-layer order_service.memory_leak_oom instead — there the
# app's own heap is the largest allocation, so the app is what gets killed.
MEMORY_MB = 200
DURATION_SEC = 600


def inject() -> None:
    """Hold MEMORY_MB resident in order-service until the cgroup OOMKills it."""
    _infra_backend.stress_memory("order-service", memory_mb=MEMORY_MB, duration_sec=DURATION_SEC)


def recover() -> None:
    """Release the held memory.

    Kills only the hog process. If the cgroup already OOMKilled the container
    this is a no-op, which is correct — the kernel got there first.
    """
    _infra_backend.stop_stress("order-service")


failure = Failure(
    key="order_service.memory_exhaust",
    service="order-service",
    title="Memory exhaustion (OOMKilled)",
    layer=InjectionLayer.INFRASTRUCTURE,
    inject=inject,
    recover=recover,
    l1="container_memory_working_set_bytes for order-service pinned at its 256Mi limit",
    l2="Kernel OOM event in the container's cgroup; application heap metrics normal, "
    "so the pressure originates outside the application",
    rca="External memory pressure exhausted the container's cgroup limit",
)
