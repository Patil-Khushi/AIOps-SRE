"""User Service — Failure 3: high CPU usage."""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override("user-service", {"environment": {"INJECT_CPU_LOAD": "true"}})


def recover() -> None:
    _docker.remove_override("user-service")


failure = Failure(
    key="user_service.high_cpu",
    service="user-service",
    title="High CPU usage",
    inject=inject,
    recover=recover,
    l1="user-service container CPU > 90%",
    l2="A single process pegged on CPU; latency climbs under load",
    rca="Application CPU saturation",
    load=LoadHint("http://localhost:8001/login", "POST",
                  {"email": "load@test.dev", "password": "x"}),
)