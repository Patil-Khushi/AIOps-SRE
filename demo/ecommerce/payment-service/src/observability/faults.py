"""Failure-injection helpers for the Payment Service.

    INJECT_HTTP_500   -> Failure 4 (unhandled 5xx)
    INJECT_CPU_LOAD   -> Failure 3 (high CPU)
"""
import os
import time


def http_500_enabled() -> bool:
    return os.getenv("INJECT_HTTP_500", "false").lower() == "true"


def maybe_burn_cpu() -> None:
    """Spin the CPU ~2s per request when enabled. Sustained load comes from
    repeated requests (what the scenario driver produces)."""
    if os.getenv("INJECT_CPU_LOAD", "false").lower() != "true":
        return
    deadline = time.time() + 2.0
    x = 0
    while time.time() < deadline:
        x = (x * x + 1) % 2_147_483_647