"""Base URLs the load generator drives traffic at.

The ports differ per deployment, so hardcoding them into each LoadHint made the
faults silently unobservable after the k8s migration: the generator hammered
dead Compose ports, got connection-refused for every request, and the scenario
produced no signal — with no error to explain why.

    k8s     NodePorts   30081 / 30082 / 30083   (demo/ecommerce/k8s/20-app.yaml)
    docker  published    8001 /  8002 /  8003   (demo/ecommerce/docker-compose.yml)

Override any of them explicitly with FI_USER_URL / FI_ORDER_URL / FI_PAYMENT_URL
— useful when port-forwarding from another machine, or if a NodePort clashes.
"""
from __future__ import annotations

import os

from ._backend import BACKEND_NAME

_DEFAULTS = {
    "k8s": {"user": 30081, "order": 30082, "payment": 30083},
    "docker": {"user": 8001, "order": 8002, "payment": 8003},
}

_ports = _DEFAULTS[BACKEND_NAME]

USER_SERVICE = os.getenv("FI_USER_URL", f"http://localhost:{_ports['user']}")
ORDER_SERVICE = os.getenv("FI_ORDER_URL", f"http://localhost:{_ports['order']}")
PAYMENT_SERVICE = os.getenv("FI_PAYMENT_URL", f"http://localhost:{_ports['payment']}")

__all__ = ["ORDER_SERVICE", "PAYMENT_SERVICE", "USER_SERVICE"]
