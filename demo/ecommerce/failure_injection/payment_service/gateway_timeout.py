"""Payment Service — Failure 2: external gateway timeout.

Slows the mock gateway past payment-service's GATEWAY_TIMEOUT_SECONDS (default
5s). Observed at the payment layer as a 504 on /payments.
"""
from .. import _backend
from .._base import Failure, LoadHint
from .._endpoints import PAYMENT_SERVICE


def inject() -> None:
    _backend.apply_override("mock-payment-gateway", {"environment": {"INJECT_DELAY_SECONDS": "30"}})


def recover() -> None:
    _backend.remove_override("mock-payment-gateway")


failure = Failure(
    key="payment_service.gateway_timeout",
    service="payment-service",
    title="External gateway timeout",
    inject=inject,
    recover=recover,
    l1="payment_latency_seconds high; payment_failures_total{reason=gateway_timeout} rising; 504 on /payments",
    l2="Trace shows the payment->gateway span stalling; external dependency slow",
    rca="Payment gateway unavailable / slow",
    load=LoadHint(f"{PAYMENT_SERVICE}/payments", "POST",
                  {"order_id": 1, "amount": 12.0}),
)