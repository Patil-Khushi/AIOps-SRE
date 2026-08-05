"""User Service — Failure 1: MySQL database down."""
from .. import _backend
from .._base import Failure


def inject() -> None:
    _backend.stop("mysql")


def recover() -> None:
    _backend.start("mysql")


failure = Failure(
    key="user_service.mysql_down",
    service="user-service",
    title="MySQL database down",
    inject=inject,
    recover=recover,
    l1="login_failure_total rising; HTTP 500 on /login; mysql_connection_status=0",
    l2="Logs: 'database connection failed' / connection refused to mysql:3306",
    rca="MySQL database unavailable",
)