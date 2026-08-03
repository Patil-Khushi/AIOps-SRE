"""Order Service — Failure 2: payment service timeout.

Driven by slowing the mock gateway past order-service's PAYMENT_TIMEOUT_SECONDS
(default 5s). Observed at the order layer as a 504 on /orders.
"""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override("mock-payment-gateway", {"environment": {"INJECT_DELAY_SECONDS": "30"}})


def recover() -> None:
    _docker.remove_override("mock-payment-gateway")


failure = Failure(
    key="order_service.payment_timeout",
    service="order-service",
    title="Payment service timeout",
    inject=inject,
    recover=recover,
    l1="order_latency_seconds high; payment_timeout_total rising; 504 on /orders",
    l2="Trace shows the order->payment span stalling on the downstream gateway",
    rca="Payment dependency timeout (slow external gateway)",
    load=LoadHint("http://localhost:8002/orders", "POST",
                  {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0}),
)