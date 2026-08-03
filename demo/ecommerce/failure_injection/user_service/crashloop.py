"""User Service — Failure 4: CrashLoopBackOff (bad config at startup)."""
from .. import _docker
from .._base import Failure


def inject() -> None:
    # Point the service at a non-existent DB host and make it restart on failure
    # so it visibly loops (startup raises -> container exits -> restarts).
    _docker.apply_override(
        "user-service",
        {"environment": {"MYSQL_HOST": "nonexistent-db-host"}, "restart": "on-failure"},
    )


def recover() -> None:
    _docker.remove_override("user-service")


failure = Failure(
    key="user_service.crashloop",
    service="user-service",
    title="CrashLoopBackOff (startup config failure)",
    inject=inject,
    recover=recover,
    l1="user-service restart count rising; never becomes healthy",
    l2="Startup logs show DB resolution/connection failure; container exits then restarts",
    rca="Startup configuration failure (bad MYSQL_HOST)",
)