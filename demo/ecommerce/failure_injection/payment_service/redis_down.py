"""Payment Service — Failure 1: Redis down."""
from .. import _backend
from .._base import Failure


def inject() -> None:
    _backend.stop("redis")


def recover() -> None:
    _backend.start("redis")


failure = Failure(
    key="payment_service.redis_down",
    service="payment-service",
    title="Redis down",
    inject=inject,
    recover=recover,
    l1="payment_failures_total{reason=redis_error} rising; 500 on /payments; redis_connection_status=0",
    l2="Logs: 'redis connection error' / connection refused to redis:6379",
    rca="Redis unavailable",
)