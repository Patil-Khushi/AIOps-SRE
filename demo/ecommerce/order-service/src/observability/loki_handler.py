"""Direct-to-Loki log shipping.

Pushes log lines straight to Loki's native HTTP API (``POST
/loki/api/v1/push``) from inside the process. This is the same posture the
other two signals already have — Prometheus scrapes ``/metrics`` off the app,
traces go OTLP straight to Jaeger — so logs stop being the one signal that
needs a sidecar (Promtail) tailing files to get anywhere.

Design constraints, in priority order:

1. **Never block a request.** ``emit`` only puts a tuple on a bounded queue;
   every byte of I/O happens on a daemon thread. A wedged Loki costs zero
   request latency, which matters because half the demo's failure scenarios are
   *about* latency and a logging stall would forge the signal being measured.
2. **Never take the app down with the log pipeline.** The queue drops oldest on
   overflow and every network error is swallowed behind a backoff. Logging is
   telemetry, not a dependency.
3. **Never replace stdout.** This handler is strictly *additive*. ``kubectl
   logs`` / ``docker logs`` keep working, which the OOMKilled and
   CrashLoopBackOff scenarios depend on: a pod that dies before the next flush
   must still leave its last words somewhere.

Labels are deliberately few — ``service_name``, ``level``, ``namespace``.
``service_name`` is the join key RA-007's log_correlation agent queries on
(``{service_name="..."}``) and is sourced from ``OTEL_SERVICE_NAME`` so logs,
metrics and traces all carry the identical value. The log *message* is never a
label: that is unbounded cardinality and the one reliable way to hurt a Loki.

No third-party HTTP client on purpose. user-service carries neither httpx nor
requests, and adding one just to ship logs would make this file diverge per
service; ``urllib.request`` is in the stdlib and entirely adequate for a
batched background POST.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

# Flush when either bound is hit, whichever comes first.
_BATCH_MAX_RECORDS = int(os.getenv("LOKI_BATCH_MAX_RECORDS", "100"))
_BATCH_INTERVAL_SECONDS = float(os.getenv("LOKI_BATCH_INTERVAL_SECONDS", "1.0"))
# Bounded so a log storm (INJECT_CPU_LOAD, a crash loop) cannot grow the heap
# unbounded — order-service runs under a 256Mi limit precisely so OOM is
# reachable on demand, and an unbounded log queue would make it reachable by
# accident.
_QUEUE_MAX_RECORDS = int(os.getenv("LOKI_QUEUE_MAX_RECORDS", "10000"))
_PUSH_TIMEOUT_SECONDS = float(os.getenv("LOKI_PUSH_TIMEOUT_SECONDS", "3.0"))
# After a failed push, stop trying for this long. Mirrors the circuit breaker in
# aiops/tools/observability/loki.py: when Loki is down the useful behaviour is
# to fail fast and cheaply, not to retry every batch into a black hole.
_BACKOFF_SECONDS = float(os.getenv("LOKI_BACKOFF_SECONDS", "5.0"))

_SHUTDOWN = object()


class LokiHandler(logging.Handler):
    """Ship formatted log lines to Loki on a background thread."""

    def __init__(self, url: str, labels: dict[str, str]) -> None:
        super().__init__()
        # Accept either a base URL or a full push URL, so setting LOKI_URL to
        # whatever someone has in front of them does the right thing.
        base = url.rstrip("/")
        self._push_url = base if base.endswith("/push") else f"{base}/loki/api/v1/push"
        self._labels = dict(labels)
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX_RECORDS)
        self._dropped = 0
        self._muted_until = 0.0
        # Loki drops an entry whose (timestamp, line) exactly matches one already
        # in the stream. ``record.created`` resolves to ~15ms on Windows, so a
        # tight loop emitting the same message — precisely what INJECT_HTTP_500
        # and the crash-loop scenarios produce — would collapse into one stored
        # line and under-report the error rate the agent is measuring. Nudging
        # each collision forward a nanosecond keeps timestamps unique and
        # ascending at a resolution no one will ever read.
        self._ts_lock = threading.Lock()
        self._last_ts = 0
        self._thread = threading.Thread(
            target=self._run, name="loki-shipper", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    # -- producer side (runs on the caller's thread; must stay cheap) --------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._ts_lock:
                ts = max(int(record.created * 1_000_000_000), self._last_ts + 1)
                self._last_ts = ts
            item = (ts, record.levelname.lower(), line)
        except Exception:  # pragma: no cover - formatting must never raise here
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Drop the OLDEST line, not this one. During an incident the newest
            # lines are the ones describing the failure under investigation;
            # discarding them to preserve stale healthy-path chatter would lose
            # exactly the evidence RA-007 is being asked to correlate.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
            self._dropped += 1

    # -- consumer side (background thread) ----------------------------------

    def _run(self) -> None:
        batch: list[tuple[int, str, str]] = []
        deadline = time.monotonic() + _BATCH_INTERVAL_SECONDS
        while True:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is _SHUTDOWN:
                self._push(batch)
                return
            if item is not None:
                batch.append(item)  # type: ignore[arg-type]

            if len(batch) >= _BATCH_MAX_RECORDS or time.monotonic() >= deadline:
                self._push(batch)
                batch = []
                deadline = time.monotonic() + _BATCH_INTERVAL_SECONDS

    def _push(self, batch: list[tuple[int, str, str]]) -> None:
        if not batch:
            return
        now = time.monotonic()
        if now < self._muted_until:
            return

        # One Loki stream per distinct label set. Only `level` varies per
        # record, so this is at most a handful of streams per batch.
        streams: dict[str, list[list[str]]] = {}
        for ts_ns, level, line in batch:
            streams.setdefault(level, []).append([str(ts_ns), line])

        # Declare any lines the queue had to discard. A gap in the log stream
        # that nothing accounts for is worse than the gap itself: RA-007 counts
        # error-severity lines to decide whether there was a spike, and a
        # silently truncated stream would understate an incident at exactly the
        # moment the service was loudest. Reported once, then the counter clears.
        dropped, self._dropped = self._dropped, 0
        if dropped:
            streams.setdefault("warning", []).append(
                [
                    str(batch[-1][0] + 1),
                    json.dumps(
                        {
                            "level": "WARNING",
                            "service": self._labels.get("service_name", "unknown"),
                            "message": (
                                f"loki shipper dropped {dropped} log line(s): "
                                f"queue full at {_QUEUE_MAX_RECORDS} records"
                            ),
                        }
                    ),
                ]
            )

        payload = {
            "streams": [
                {
                    "stream": {**self._labels, "level": level},
                    # Loki wants entries in ascending time order within a stream.
                    "values": sorted(values, key=lambda v: int(v[0])),
                }
                for level, values in streams.items()
            ]
        }

        req = urllib.request.Request(
            self._push_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_PUSH_TIMEOUT_SECONDS):
                pass
        except (urllib.error.URLError, OSError, ValueError):
            # Deliberately silent: routing this to the logger would recurse
            # straight back into this handler. The mute window is the only
            # signal, and stdout still has every line regardless.
            self._muted_until = now + _BACKOFF_SECONDS
            # Put the drop count back — this push carried the notice and did not
            # land, so zeroing it here would lose the only record that anything
            # was discarded.
            self._dropped += dropped

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SHUTDOWN)
            self._thread.join(timeout=_PUSH_TIMEOUT_SECONDS + 1.0)
        except Exception:
            pass
        super().close()


def build_loki_handler(
    formatter: logging.Formatter, service_name: str
) -> LokiHandler | None:
    """Construct the handler from the environment, or ``None`` if disabled.

    Returns ``None`` when ``LOKI_URL`` is unset — the same "blank disables it"
    contract ``OTEL_EXPORTER_OTLP_ENDPOINT`` already uses, so a bare ``docker
    compose up`` or a local ``uvicorn`` runs with no observability stack at all
    and nothing to configure.
    """
    url = os.getenv("LOKI_URL", "").strip()
    if not url:
        return None

    labels = {
        # Same value as OTEL_SERVICE_NAME so one label joins logs, metrics and
        # traces. Falls back to the service's own name when unset.
        "service_name": os.getenv("OTEL_SERVICE_NAME", service_name),
        "namespace": os.getenv("LOKI_NAMESPACE", "ecommerce"),
    }
    handler = LokiHandler(url, labels)
    # Same formatter as stdout, so the line Loki stores is byte-identical to the
    # line in `kubectl logs` — the json/level parsing downstream is unchanged
    # from what Promtail's pipeline_stages used to produce.
    handler.setFormatter(formatter)
    return handler
