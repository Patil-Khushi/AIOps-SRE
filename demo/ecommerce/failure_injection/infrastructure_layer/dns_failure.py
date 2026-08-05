"""Payment Service — Failure: DNS resolution broken."""
from .._base import Failure, InjectionLayer
from . import _infra_backend


def inject() -> None:
    """Break DNS resolution on payment-service by poisoning /etc/resolv.conf."""
    _infra_backend.break_dns("payment-service")


def recover() -> None:
    """Restore DNS by restarting the pod."""
    _infra_backend.restore_dns("payment-service")


failure = Failure(
    key="payment_service.dns_failure",
    service="payment-service",
    title="DNS resolution broken",
    layer=InjectionLayer.INFRASTRUCTURE,
    inject=inject,
    recover=recover,
    l1="payment_service DNS lookup failures; connection errors increasing",
    l2="Payment service unable to resolve domain names (getaddrinfo failures in logs)",
    rca="DNS infrastructure broken",
)
