"""Order Service — Failure 1: PostgreSQL database down."""
from .. import _backend
from .._base import Failure


def inject() -> None:
    _backend.stop("postgres")


def recover() -> None:
    _backend.start("postgres")


failure = Failure(
    key="order_service.postgres_down",
    service="order-service",
    title="PostgreSQL database down",
    inject=inject,
    recover=recover,
    l1="orders_failed_total{reason=db_error} rising; HTTP 500 on /orders; postgres_connection_status=0",
    l2="Logs: 'database connection failed' / connection refused to postgres:5432",
    rca="PostgreSQL database unavailable",
)