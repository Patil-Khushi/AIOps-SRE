"""Payment Service — Failure 3: high CPU usage."""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override("payment-service", {"environment": {"INJECT_CPU_LOAD": "true"}})


def recover() -> None:
    _docker.remove_override("payment-service")


failure = Failure(
    key="payment_service.high_cpu",
    service="payment-service",
    title="High CPU usage",
    inject=inject,
    recover=recover,
    l1="payment-service container CPU > 90%",
    l2="Resource consumption analysis shows a process pegged on CPU",
    rca="Application resource exhaustion",
    load=LoadHint("http://localhost:8003/payments", "POST",
                  {"order_id": 1, "amount": 12.0}),
)