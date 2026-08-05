"""Order Service — Failure 3: HTTP 500 errors."""
from .. import _backend
from .._base import Failure, InjectionLayer, LoadHint
from .._endpoints import ORDER_SERVICE


def inject() -> None:
    _backend.apply_override("order-service", {"environment": {"INJECT_HTTP_500": "true"}})


def recover() -> None:
    _backend.remove_override("order-service")


def inject_infra() -> None:
    """Infrastructure-layer: kill the payment dependency for real 5xx."""
    from ..infrastructure_layer import dependency_failure
    dependency_failure.inject()


def recover_infra() -> None:
    """Infrastructure-layer: wait for the payment pod to come back."""
    from ..infrastructure_layer import dependency_failure
    dependency_failure.recover()


failure = Failure(
    key="order_service.http_500",
    service="order-service",
    title="HTTP 500 errors",
    layer=InjectionLayer.HYBRID,
    inject=inject,
    recover=recover,
    inject_infra=inject_infra,
    recover_infra=recover_infra,
    l1="5xx rate on /orders increases; orders_failed_total rising",
    l2="Application logs show error OR payment service pod is down (connection refused)",
    rca="Code failure (unhandled exception) OR dependency unavailable",
    load=LoadHint(f"{ORDER_SERVICE}/orders", "POST",
                  {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0}),
)