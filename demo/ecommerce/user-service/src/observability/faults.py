"""Failure-injection helpers, driven by env vars.

Kept in one place so routes stay readable. All default to inert.
    INJECT_LATENCY_SECONDS  -> Failure 2 (high API latency)
    INJECT_CPU_LOAD         -> Failure 3 (high CPU usage)
"""

import os
import time


def maybe_inject_latency() -> None:
    """Sleep for INJECT_LATENCY_SECONDS if set (>0)."""
    try:
        seconds = float(os.getenv("INJECT_LATENCY_SECONDS", "0"))
    except ValueError:
        seconds = 0
    if seconds > 0:
        time.sleep(seconds)


def maybe_burn_cpu() -> None:
    """Spin the CPU for a short burst if INJECT_CPU_LOAD is true.

    Bounded (~2s of tight looping) so a single request doesn't hang forever;
    sustained load comes from repeated requests, which is what the scenario
    driver does.
    """
    if os.getenv("INJECT_CPU_LOAD", "false").lower() != "true":
        return
    deadline = time.time() + 2.0
    x = 0
    while time.time() < deadline:
        x = (x * x + 1) % 2_147_483_647
