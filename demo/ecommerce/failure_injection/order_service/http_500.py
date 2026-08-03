"""Order Service — Failure 3: HTTP 500 errors."""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override("order-service", {"environment": {"INJECT_HTTP_500": "true"}})


def recover() -> None:
    _docker.remove_override("order-service")


failure = Failure(
    key="order_service.http_500",
    service="order-service",
    title="HTTP 500 errors",
    inject=inject,
    recover=recover,
    l1="5xx rate on /orders increases; orders_failed_total{reason=injected_500} rising",
    l2="Application logs show the injected error on order creation",
    rca="Code failure (unhandled exception path)",
    load=LoadHint("http://localhost:8002/orders", "POST",
                  {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0}),
)