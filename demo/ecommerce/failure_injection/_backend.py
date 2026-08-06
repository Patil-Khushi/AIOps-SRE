"""Backend selector for failure injection.

The 12 failure modules call four verbs — stop / start / apply_override /
remove_override — and never care how they are implemented. This module picks
the implementation so the modules stay deployment-agnostic.

    FI_BACKEND=k8s      (default) inject into Rancher Desktop k3s
    FI_BACKEND=docker             inject into the Docker Compose stack

k8s is the default because that is where the SUT runs for AIOps work: only
there do OOMKilled and CrashLoopBackOff appear as real pod states, which is
what the truth files assert. The Compose backend is kept because the app is
still perfectly runnable under Compose for plain development.
"""

from __future__ import annotations

import os

_BACKEND = os.getenv("FI_BACKEND", "k8s").lower()

if _BACKEND == "docker":
    from . import _docker as _impl
elif _BACKEND in ("k8s", "kubernetes", "kubectl"):
    from . import _k8s as _impl
else:
    raise ValueError(f"Unknown FI_BACKEND={_BACKEND!r}. Expected 'k8s' or 'docker'.")

BACKEND_NAME = "docker" if _BACKEND == "docker" else "k8s"

stop = _impl.stop
start = _impl.start
apply_override = _impl.apply_override
remove_override = _impl.remove_override

__all__ = ["BACKEND_NAME", "apply_override", "remove_override", "start", "stop"]
