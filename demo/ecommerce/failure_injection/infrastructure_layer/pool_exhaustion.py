"""User Service — MySQL connection exhaustion driven from outside the app.

Saturates the *server's* max_connections (151 on this deployment) rather than
user-service's own SQLAlchemy pool. An external process holds the sessions, so
the application is a bystander: its pool is healthy, but every attempt to open a
new connection is refused by MySQL. That is the shape this takes in production —
some other client exhausts the server and an innocent service starts failing.
"""

from .._base import Failure, InjectionLayer
from . import _infra_backend

# MySQL's max_connections is 151; overshooting slightly guarantees the ceiling is
# reached. The holder stops early once the server starts refusing, so the extra
# attempts cost nothing.
CONNECTIONS = 155
DURATION_SEC = 600


def inject() -> None:
    """Hold MySQL sessions open until the server refuses new ones."""
    _infra_backend.start_connection_holder(
        "user-service", connection_count=CONNECTIONS, duration_sec=DURATION_SEC
    )


def recover() -> None:
    """Release the held sessions without restarting user-service."""
    _infra_backend.stop_connection_holder("user-service")


failure = Failure(
    key="user_service.pool_exhaustion",
    service="user-service",
    title="MySQL connection exhaustion",
    layer=InjectionLayer.INFRASTRUCTURE,
    inject=inject,
    recover=recover,
    l1="login_failure_total rising; 500s on /login; mysql_connection_status flapping",
    l2="MySQL Threads_connected pinned at max_connections; user-service logs "
    "'Too many connections' on connect, while its own pool metrics look healthy",
    rca="MySQL server connection limit exhausted by an external client",
)
