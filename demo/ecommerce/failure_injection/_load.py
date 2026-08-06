"""Tiny stdlib-only load generator.

Several faults (high CPU, high latency, memory leak) only become visible under
traffic. This fires concurrent requests for a fixed duration and reports the
status-code distribution. No third-party dependencies so it runs anywhere.
"""

from __future__ import annotations

import json as _json
import threading
import time
import urllib.error
import urllib.request


def _one(url: str, method: str, body: dict | None) -> int:
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def generate(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    duration: float = 20.0,
    concurrency: int = 4,
) -> dict[int, int]:
    """Hammer `url` for `duration` seconds with `concurrency` workers."""
    deadline = time.time() + duration
    counts: dict[int, int] = {}
    lock = threading.Lock()

    def worker() -> None:
        while time.time() < deadline:
            status = _one(url, method, body)
            with lock:
                counts[status] = counts.get(status, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return counts
