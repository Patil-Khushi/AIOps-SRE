"""Infrastructure half of ``order_service.http_500`` — kill the payment dependency.

inject()/recover() only; the registered Failure lives in
``order_service/http_500.py``.

Note the asymmetry: inject() deletes the pod, but recover() only *waits* for the
Deployment to bring a replacement up. Kubernetes owns the restart, so there is
nothing to undo — recovery is "confirm it came back", not "put it back".
"""
from . import _infra_backend


def inject() -> None:
    """Kill payment-service pod so order-service throws real dependency errors."""
    _infra_backend.kill_pod("payment-service")


def recover() -> None:
    """Wait for the replacement payment-service pod to become ready."""
    _infra_backend.wait_for_pod_ready("payment-service")
