"""Infrastructure half of ``payment_service.high_cpu`` — stress-ng CPU burn.

inject()/recover() only; the registered Failure lives in
``payment_service/high_cpu.py``.

The burn loop is plain Python, not stress-ng: the service images do not ship
stress-ng. It is bounded by DURATION_SEC, so a missed recover() self-heals.
"""
from . import _infra_backend

# payment-service has `limits.cpu: 1`, so one worker is the whole quota —
# asking for 2 only deepens cgroup throttling without raising the CPU series.
CORES = 1

# Held below 1.0 deliberately. The liveness probe allows 1s per check and fails
# the container after 3 misses; a flat spin starves /health and the kubelet
# restarts the pod about a minute in, turning a CPU-saturation scenario into a
# crashloop. See stress_cpu() for the full reasoning.
UTILIZATION = 0.85

DURATION_SEC = 600


def inject() -> None:
    """Drive payment-service CPU to UTILIZATION for DURATION_SEC."""
    _infra_backend.stress_cpu(
        "payment-service",
        cores=CORES,
        duration_sec=DURATION_SEC,
        utilization=UTILIZATION,
    )


def recover() -> None:
    """Kill the burn processes, leaving the pod (and its metrics history) alive."""
    _infra_backend.stop_stress("payment-service")
