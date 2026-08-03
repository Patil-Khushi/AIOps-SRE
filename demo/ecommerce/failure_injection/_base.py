"""Shared types for failure-injection modules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


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
    inject: Callable[[], None]
    recover: Callable[[], None]
    # Reference signals (also mirrored into truth_files/ later).
    l1: str = ""                # alert-level signal
    l2: str = ""                # investigation-level signal
    rca: str = ""               # root cause
    load: Optional[LoadHint] = None