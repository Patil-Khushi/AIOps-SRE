"""Order Service — Failure: Network packet loss."""
from .._base import Failure, LoadHint, InjectionLayer
from .._endpoints import ORDER_SERVICE
from . import _infra_backend


def inject() -> None:
    """Inject 5% packet loss on order-service pods."""
    _infra_backend.inject_packet_loss("order-service", loss_percent=5)


def recover() -> None:
    """Remove packet loss."""
    _infra_backend.remove_packet_loss("order-service")


failure = Failure(
    key="order_service.packet_loss",
    service="order-service",
    title="Network packet loss (5%)",
    layer=InjectionLayer.INFRASTRUCTURE,
    inject=inject,
    recover=recover,
    l1="order_failures_total rising; connection errors increasing",
    l2="TCP retransmits increasing; network packet loss observable via tcpdump",
    rca="Network infrastructure packet loss",
    load=LoadHint(f"{ORDER_SERVICE}/orders", "POST",
                  {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0}),
)
