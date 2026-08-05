"""Shared types for failure-injection modules."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class InjectionLayer(Enum):
    """Which layer(s) a failure injection targets."""
    APPLICATION = "application"       # Environment variables, ConfigMaps
    INFRASTRUCTURE = "infrastructure" # tc, python stress, kubectl, dd, DNS
    HYBRID = "hybrid"                 # Both layers


class ChaosUnavailable(RuntimeError):
    """The environment cannot support this injection — not a failed injection.

    Raised when a required in-container tool or kernel capability is absent (no
    `tc`, no CAP_NET_ADMIN). Distinct from an ordinary exception because the
    orchestrator treats it as "this layer is unavailable here" rather than "this
    layer broke": a hybrid failure whose application half landed is still
    injected, and reporting it as a failure would send an operator chasing a
    fault that is in fact active.

    Lives in _base rather than the infrastructure layer so the orchestrator can
    recognise it without importing that layer.
    """


@dataclass(frozen=True)
class LoadHint:
    """Traffic to drive so a fault becomes observable (CPU/latency/OOM need it)."""
    url: str
    method: str = "GET"
    body: Optional[dict] = None


@dataclass(frozen=True)
class Failure:
    key: str                    # unique id, e.g. "user_service.mysql_down"
    service: str                # logical service the fault belongs to
    title: str
    inject: Callable[[], None]  # application-layer injection
    recover: Callable[[], None] # application-layer recovery

    # Injection layer configuration
    layer: InjectionLayer = InjectionLayer.APPLICATION

    # Optional infrastructure-layer implementations (for HYBRID failures)
    inject_infra: Optional[Callable[[], None]] = None
    recover_infra: Optional[Callable[[], None]] = None

    # Reference signals (also mirrored into truth_files/ later).
    l1: str = ""                # alert-level signal
    l2: str = ""                # investigation-level signal
    rca: str = ""               # root cause
    load: Optional[LoadHint] = None