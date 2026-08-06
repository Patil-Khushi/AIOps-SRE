"""Payment Service — Failure 4: HTTP 500 errors."""

from .. import _backend
from .._base import Failure, LoadHint
from .._endpoints import PAYMENT_SERVICE


def inject() -> None:
    _backend.apply_override("payment-service", {"environment": {"INJECT_HTTP_500": "true"}})


def recover() -> None:
    _backend.remove_override("payment-service")


failure = Failure(
    key="payment_service.http_500",
    service="payment-service",
    title="HTTP 500 errors",
    inject=inject,
    recover=recover,
    l1="5xx rate on /payments increases; payment_failures_total{reason=injected_500} rising",
    l2="Payment logs show the injected failure",
    rca="Payment application failure",
    load=LoadHint(f"{PAYMENT_SERVICE}/payments", "POST", {"order_id": 1, "amount": 12.0}),
)
