"""User Service — Failure 2: high API latency on /login."""
from .. import _backend
from .._base import Failure, InjectionLayer, LoadHint
from .._endpoints import USER_SERVICE


def inject() -> None:
    _backend.apply_override("user-service", {"environment": {"INJECT_LATENCY_SECONDS": "10"}})


def recover() -> None:
    _backend.remove_override("user-service")


def inject_infra() -> None:
    """Infrastructure-layer: 500ms network delay via tc qdisc."""
    # Imported lazily: the infra layer shells out to kubectl, and importing it at
    # module scope would make `list` fail on a machine with no cluster.
    from ..infrastructure_layer import network_latency
    network_latency.inject()


def recover_infra() -> None:
    """Infrastructure-layer: remove the network delay."""
    from ..infrastructure_layer import network_latency
    network_latency.recover()


failure = Failure(
    key="user_service.high_latency",
    service="user-service",
    title="High API latency (/login)",
    layer=InjectionLayer.HYBRID,
    inject=inject,
    recover=recover,
    inject_infra=inject_infra,
    recover_infra=recover_infra,
    l1="login_latency_seconds p95/p99 breaches threshold (~10s)",
    l2="Latency isolated to the login handler; DB and CPU normal; OR network latency observed",
    rca="Application processing delay OR network infrastructure latency",
    load=LoadHint(f"{USER_SERVICE}/login", "POST",
                  {"email": "load@test.dev", "password": "x"}),
)