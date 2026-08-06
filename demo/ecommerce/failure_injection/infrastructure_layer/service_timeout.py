"""Infrastructure half of ``order_service.payment_timeout`` — tc delay on payment.

inject()/recover() only; the registered Failure lives in
``order_service/payment_timeout.py``.

Delays payment-service rather than the mock gateway: the app-layer version slows
the gateway via env var, so putting the tc qdisc one hop closer (on payment
itself) makes the two halves stack instead of masking each other.
"""

from . import _infra_backend

DELAY_MS = 30_000


def inject() -> None:
    """Inject 30s network delay on payment-service."""
    _infra_backend.inject_network_delay("payment-service", delay_ms=DELAY_MS)


def recover() -> None:
    """Remove network delay from payment-service."""
    _infra_backend.remove_network_delay("payment-service")
