"""User Service — Failure 2: high API latency on /login."""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override("user-service", {"environment": {"INJECT_LATENCY_SECONDS": "10"}})


def recover() -> None:
    _docker.remove_override("user-service")


failure = Failure(
    key="user_service.high_latency",
    service="user-service",
    title="High API latency (/login)",
    inject=inject,
    recover=recover,
    l1="login_latency_seconds p95/p99 breaches threshold (~10s)",
    l2="Latency isolated to the login handler; DB and CPU normal",
    rca="Application processing delay on the login endpoint",
    load=LoadHint("http://localhost:8001/login", "POST",
                  {"email": "load@test.dev", "password": "x"}),
)