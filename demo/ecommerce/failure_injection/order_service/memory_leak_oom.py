"""Order Service — Failure 4: memory leak / OOMKilled.

Enables the per-order leak and caps container memory so sustained order traffic
grows RSS until the kernel OOM-kills the container.
"""
from .. import _docker
from .._base import Failure, LoadHint


def inject() -> None:
    _docker.apply_override(
        "order-service",
        {"environment": {"INJECT_MEMORY_LEAK": "true"}, "mem_limit": "256m"},
    )


def recover() -> None:
    _docker.remove_override("order-service")


failure = Failure(
    key="order_service.memory_leak_oom",
    service="order-service",
    title="Memory leak / OOMKilled",
    inject=inject,
    recover=recover,
    l1="order-service memory usage climbing toward the limit",
    l2="Container restarted with reason OOMKilled",
    rca="Memory leak caused OOMKilled",
    load=LoadHint("http://localhost:8002/orders", "POST",
                  {"items": [{"sku": "widget", "qty": 1, "price": 12.0}], "amount": 12.0}),
)