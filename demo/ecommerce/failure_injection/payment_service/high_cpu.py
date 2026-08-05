"""Payment Service — Failure 3: high CPU usage."""

from .. import _backend
from .._base import Failure, InjectionLayer, LoadHint
from .._endpoints import PAYMENT_SERVICE


def inject() -> None:
    _backend.apply_override("payment-service", {"environment": {"INJECT_CPU_LOAD": "true"}})


def recover() -> None:
    _backend.remove_override("payment-service")


def inject_infra() -> None:
    """Infrastructure-layer: burn CPU cores with stress-ng."""
    from ..infrastructure_layer import cpu_spike

    cpu_spike.inject()


def recover_infra() -> None:
    """Infrastructure-layer: kill the pod to stop the stress process."""
    from ..infrastructure_layer import cpu_spike

    cpu_spike.recover()


failure = Failure(
    key="payment_service.high_cpu",
    service="payment-service",
    title="High CPU usage",
    layer=InjectionLayer.HYBRID,
    inject=inject,
    recover=recover,
    inject_infra=inject_infra,
    recover_infra=recover_infra,
    l1="payment-service container CPU > 90%",
    l2="Resource consumption shows process pegged on CPU; OR stress-ng visible",
    rca="Application resource exhaustion OR external CPU stress (infra-layer)",
    load=LoadHint(f"{PAYMENT_SERVICE}/payments", "POST", {"order_id": 1, "amount": 12.0}),
)
