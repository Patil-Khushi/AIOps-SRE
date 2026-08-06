"""User Service — Failure 3: high CPU usage."""

from .. import _backend
from .._base import Failure, LoadHint
from .._endpoints import USER_SERVICE


def inject() -> None:
    _backend.apply_override("user-service", {"environment": {"INJECT_CPU_LOAD": "true"}})


def recover() -> None:
    _backend.remove_override("user-service")


failure = Failure(
    key="user_service.high_cpu",
    service="user-service",
    title="High CPU usage",
    inject=inject,
    recover=recover,
    l1="user-service container CPU > 90%",
    l2="A single process pegged on CPU; latency climbs under load",
    rca="Application CPU saturation",
    load=LoadHint(f"{USER_SERVICE}/login", "POST", {"email": "load@test.dev", "password": "x"}),
)
