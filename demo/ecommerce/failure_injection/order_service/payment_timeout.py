"""Order Service — Failure 2: payment service timeout.

Driven by slowing the mock gateway past order-service's PAYMENT_TIMEOUT_SECONDS
(default 5s). Observed at the order layer as a 504 on /orders.
"""

from .. import _backend
from .._base import Failure, InjectionLayer, LoadHint
from .._endpoints import ORDER_SERVICE


def inject() -> None:
    _backend.apply_override("mock-payment-gateway", {"environment": {"INJECT_DELAY_SECONDS": "30"}})


def recover() -> None:
    _backend.remove_override("mock-payment-gateway")


def inject_infra() -> None:
    """Infrastructure-layer: 30s network delay on payment-service."""
    from ..infrastructure_layer import service_timeout

    service_timeout.inject()


def recover_infra() -> None:
    """Infrastructure-layer: remove the network delay."""
    from ..infrastructure_layer import service_timeout

    service_timeout.recover()


failure = Failure(
    key="order_service.payment_timeout",
    service="order-service",
    title="Payment service timeout",
    layer=InjectionLayer.HYBRID,
    inject=inject,
    recover=recover,
    inject_infra=inject_infra,
    recover_infra=recover_infra,
    l1="order_latency_seconds high; payment_timeout_total rising; 504 on /orders",
    l2="Trace shows order->payment span stalling; OR network latency observed (tc qdisc)",
    rca="Payment dependency timeout OR network infrastructure latency",
    load=LoadHint(
        f"{ORDER_SERVICE}/orders",
        "POST",
        {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0},
    ),
)
