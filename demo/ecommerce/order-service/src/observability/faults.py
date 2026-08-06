"""Failure-injection helpers for the Order Service.

INJECT_HTTP_500     -> Failure 3 (unhandled 5xx)
INJECT_MEMORY_LEAK  -> Failure 4 (unbounded allocation -> OOMKilled)
"""

import os

# Module-global sink that is never freed while the leak toggle is on. Each
# order appends a chunk; sustained traffic grows RSS until the container hits
# its memory limit and is OOMKilled.
_LEAK: list[bytes] = []
_CHUNK = b"x" * (5 * 1024 * 1024)  # 5 MB per leaked order


def http_500_enabled() -> bool:
    return os.getenv("INJECT_HTTP_500", "false").lower() == "true"


def maybe_leak_memory() -> None:
    if os.getenv("INJECT_MEMORY_LEAK", "false").lower() == "true":
        _LEAK.append(bytes(_CHUNK))
