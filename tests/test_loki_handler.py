"""Tests for LokiHandler batching, backoff, drop accounting and thread safety.

Two fixtures, and picking the right one matters:

``handler`` keeps the real shipper thread running — use it only for behaviour
that *is* the thread. ``threadless_handler`` stops the thread first, and is the
default choice: anything that swaps ``_queue`` for a test double or calls
``_push`` directly needs no consumer, and a live one would both race the
assertions and refuse to die (``close()`` delivers ``_SHUTDOWN`` *through* the
queue, so a mocked queue never delivers it and the thread leaks into the rest of
the session). ``test_no_shipper_threads_leaked`` is the backstop for that.
"""

import importlib.util
import json
import logging
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The three services carry a byte-identical copy of this module (they are
# separately built container images with no shared package to import from), so
# testing one covers all three — tests/test_loki_handler_parity.py pins that
# they stay identical.
#
# Loaded by file path rather than by adding the service's src/ to sys.path: the
# package there is named `observability`, which would shadow this repo's own
# `aiops/tools/observability/` for every test that runs after this module in the
# same pytest session.
_HANDLER_PATH = (
    Path(__file__).resolve().parents[1]
    / "demo"
    / "ecommerce"
    / "user-service"
    / "src"
    / "observability"
    / "loki_handler.py"
)
_spec = importlib.util.spec_from_file_location("_loki_handler_under_test", _HANDLER_PATH)
assert _spec is not None and _spec.loader is not None
loki_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loki_handler)

LokiHandler = loki_handler.LokiHandler
build_loki_handler = loki_handler.build_loki_handler
_SHUTDOWN = loki_handler._SHUTDOWN
_BACKOFF_SECONDS = loki_handler._BACKOFF_SECONDS
_BATCH_MAX_RECORDS = loki_handler._BATCH_MAX_RECORDS
_QUEUE_MAX_RECORDS = loki_handler._QUEUE_MAX_RECORDS

_URL = "http://localhost:3100"
_LABELS = {"service_name": "test-service", "namespace": "ecommerce"}


def _record(msg: str = "test message", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def _stop_thread(handler) -> None:
    """Shut the shipper thread down through the real ``_SHUTDOWN`` path."""
    handler._queue.put_nowait(_SHUTDOWN)
    handler._thread.join(timeout=5.0)
    assert not handler._thread.is_alive(), "shipper thread did not stop"


@pytest.fixture
def handler():
    """Handler with its shipper thread live. Only for testing the thread itself."""
    h = LokiHandler(_URL, dict(_LABELS))
    yield h
    h.close()
    h._thread.join(timeout=5.0)


@pytest.fixture
def threadless_handler():
    """Handler whose shipper thread has been stopped before the test body runs."""
    h = LokiHandler(_URL, dict(_LABELS))
    _stop_thread(h)
    yield h
    h.close()


# -- producer side ----------------------------------------------------------


def test_emit_adds_to_queue(threadless_handler):
    threadless_handler.emit(_record())
    assert threadless_handler._queue.qsize() == 1


def test_queue_overflow_increments_drop_counter(threadless_handler):
    """A full queue drops the oldest line and counts it."""
    full_queue = MagicMock()
    full_queue.put_nowait.side_effect = queue.Full()
    full_queue.get_nowait.side_effect = queue.Empty()
    threadless_handler._queue = full_queue

    threadless_handler.emit(_record())

    assert threadless_handler._dropped == 1


def test_emit_prefers_newest_line_over_oldest(threadless_handler):
    """On overflow the OLDEST line goes, not the one being emitted.

    During an incident the newest lines describe the failure under
    investigation; discarding them to preserve stale healthy-path chatter would
    lose exactly the evidence RA-007 is asked to correlate.
    """
    tiny = queue.Queue(maxsize=2)
    threadless_handler._queue = tiny

    for i in range(5):
        threadless_handler.emit(_record(f"message {i}"))

    survivors = [tiny.get_nowait()[2] for _ in range(tiny.qsize())]
    assert any("message 4" in line for line in survivors), "newest line was dropped"
    assert not any("message 0" in line for line in survivors), "oldest line survived"
    assert threadless_handler._dropped == 3


def test_drop_counter_is_thread_safe(threadless_handler):
    """Concurrent emit() calls do not lose drop-count increments.

    Nothing is consuming (threadless), so every emit past capacity must be
    counted: three threads pushing OVERSHOOT past a fixed capacity gives an
    exact expected total rather than a "greater than zero" smoke check.
    """
    capacity, threads_count, per_thread = 10, 3, 40
    threadless_handler._queue = queue.Queue(maxsize=capacity)

    def fill():
        for i in range(per_thread):
            threadless_handler.emit(_record(f"message {i}"))

    threads = [threading.Thread(target=fill) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert all(not t.is_alive() for t in threads)

    expected = threads_count * per_thread - capacity
    assert threadless_handler._dropped == expected


# -- push path: drop accounting --------------------------------------------


def test_muted_batch_counts_as_dropped(threadless_handler):
    """Batches discarded during the backoff window are counted, not silent.

    This is the regression the handler exists to survive: during a real Loki
    outage every batch built inside the 5s mute window used to vanish with zero
    record, understating the very incident the logs are being read to explain.
    """
    threadless_handler._muted_until = time.monotonic() + _BACKOFF_SECONDS
    batch = [(1, "error", "line-a"), (2, "error", "line-b")]

    with patch("urllib.request.urlopen") as mock_urlopen:
        threadless_handler._push(batch)

    assert threadless_handler._dropped == len(batch)
    assert mock_urlopen.call_count == 0, "muted push must not touch the network"


def test_failed_push_counts_current_batch_as_dropped(threadless_handler):
    """A batch whose push fails is counted, on top of any pre-existing drops."""
    threadless_handler._dropped = 7
    batch = [(1, "error", "a"), (2, "error", "b"), (3, "error", "c")]

    with patch("urllib.request.urlopen", side_effect=ConnectionError("down")) as mock_urlopen:
        threadless_handler._push(batch)

    assert mock_urlopen.call_count == 1
    # The 7 carried in the (undelivered) notice, plus the 3 that just failed.
    assert threadless_handler._dropped == 7 + len(batch)


def test_successful_push_reports_then_clears_drops(threadless_handler):
    threadless_handler._dropped = 5
    batch = [(1, "error", "boom")]

    with patch("urllib.request.urlopen") as mock_urlopen:
        threadless_handler._push(batch)

    assert mock_urlopen.call_count == 1
    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    notices = [
        v[1]
        for stream in payload["streams"]
        if stream["stream"].get("level") == "warning"
        for v in stream["values"]
    ]
    assert len(notices) == 1, f"expected exactly one drop notice, got {notices}"
    assert "dropped 5 log line(s)" in notices[0]
    assert threadless_handler._dropped == 0


def test_drop_notice_does_not_blame_a_single_cause(threadless_handler):
    """The notice must not attribute drops solely to queue overflow.

    Three paths feed the counter (overflow, muted batch, failed push). Naming
    only overflow would point an incident responder at queue pressure when the
    real cause was Loki being unreachable.
    """
    threadless_handler._dropped = 3
    with patch("urllib.request.urlopen") as mock_urlopen:
        threadless_handler._push([(1, "error", "boom")])

    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    notice = next(
        v[1]
        for stream in payload["streams"]
        if stream["stream"].get("level") == "warning"
        for v in stream["values"]
    )
    assert "unreachable" in notice, "notice omits the Loki-outage cause"


def test_payload_shape_and_labels(threadless_handler):
    """Entries group into one stream per level, ascending, with joined labels."""
    batch = [(3, "error", "c"), (1, "info", "a"), (2, "error", "b")]

    with patch("urllib.request.urlopen") as mock_urlopen:
        threadless_handler._push(batch)

    payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    by_level = {s["stream"]["level"]: s for s in payload["streams"]}
    assert set(by_level) == {"error", "info"}
    # service_name is the join key RA-007 queries on; it must survive onto every
    # stream alongside the per-record level.
    assert by_level["error"]["stream"]["service_name"] == "test-service"
    error_ts = [int(v[0]) for v in by_level["error"]["values"]]
    assert error_ts == sorted(error_ts), "Loki requires ascending timestamps"


def test_empty_batch_is_not_pushed(threadless_handler):
    with patch("urllib.request.urlopen") as mock_urlopen:
        threadless_handler._push([])
    assert mock_urlopen.call_count == 0


# -- push path: backoff ----------------------------------------------------


def test_backoff_mutes_push_after_failure(threadless_handler):
    with patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
        threadless_handler._push([(1, "error", "boom")])

    remaining = threadless_handler._muted_until - time.monotonic()
    assert 0 < remaining <= _BACKOFF_SECONDS + 0.1


# -- timestamps ------------------------------------------------------------


def test_timestamp_collision_avoidance(threadless_handler):
    """Identical messages sharing a coarse clock tick still get unique stamps.

    Loki silently discards an entry whose (timestamp, line) duplicates one
    already in the stream, so a tight error loop on Windows' ~15ms clock would
    collapse into a single stored line and understate the error rate RA-007
    measures.
    """
    captured: list[tuple[int, str, str]] = []
    threadless_handler._queue = MagicMock()
    threadless_handler._queue.put_nowait.side_effect = captured.append

    # Every record shares one `created` value: the worst case, where the clock
    # does not advance at all between emits.
    for _ in range(5):
        record = _record("duplicate message")
        record.created = 1_700_000_000.0
        threadless_handler.emit(record)

    assert len(captured) == 5, "every emit must reach the queue"
    timestamps = [ts for ts, _level, _line in captured]
    assert len(set(timestamps)) == 5, f"timestamps must be unique, got {timestamps}"
    assert timestamps == sorted(timestamps), "timestamps must be ascending"
    # All five lines are byte-identical, so uniqueness came from the nudge alone.
    assert len({line for _ts, _level, line in captured}) == 1


# -- the background thread -------------------------------------------------


def test_unexpected_push_error_does_not_kill_shipper_thread(handler):
    """An exception escaping _push must not end the background thread.

    _push guards its own urlopen call, but anything raising outside that narrow
    try would propagate out of _run and stop the shipper permanently — every
    later line silently discarded for the life of the process, drop counter
    included. That is the failure mode constraint 2 of the module docstring
    exists to prevent, so it degrades to mute-and-continue instead.
    """
    with patch.object(handler, "_push", side_effect=RuntimeError("malformed entry")) as mock_push:
        batch = [(1, "error", "line")]
        handler._safe_push(batch)

        assert mock_push.call_count == 1
        # The unshippable batch is accounted for rather than vanishing...
        assert handler._dropped == len(batch)
        # ...and the failure opens the backoff window, as a network error would.
        assert handler._muted_until > time.monotonic()

    # The thread survived and is still draining — the whole point of the guard.
    assert handler._thread.is_alive()
    handler.emit(_record("after the failure"))
    deadline = time.monotonic() + 5.0
    while handler._queue.qsize() > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert handler._queue.qsize() == 0, "shipper stopped draining the queue"


def test_shipper_flushes_when_batch_size_reached(handler):
    """Reaching _BATCH_MAX_RECORDS triggers a push without waiting for the timer."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        for i in range(_BATCH_MAX_RECORDS):
            handler.emit(_record(f"message {i}"))

        deadline = time.monotonic() + 5.0
        while mock_urlopen.call_count == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert mock_urlopen.call_count >= 1


def test_close_flushes_pending_records(handler):
    """close() delivers _SHUTDOWN so queued lines are not lost on exit.

    The OOMKilled and CrashLoopBackOff scenarios depend on a dying pod still
    leaving its last words somewhere.
    """
    with patch("urllib.request.urlopen") as mock_urlopen:
        handler.emit(_record("last words"))
        handler.close()
        handler._thread.join(timeout=5.0)

        assert not handler._thread.is_alive()
        assert mock_urlopen.call_count >= 1


def test_close_is_idempotent(handler):
    with patch("urllib.request.urlopen"):
        handler.close()
        handler.close()  # must not raise


# -- construction ----------------------------------------------------------


@pytest.mark.parametrize("loki_url", ["", "   "], ids=["empty", "whitespace"])
def test_disabled_by_blank_url(loki_url):
    """build_loki_handler returns None when LOKI_URL is blank.

    Same "blank disables it" contract OTEL_EXPORTER_OTLP_ENDPOINT already uses,
    so a bare `docker compose up` runs with nothing to configure.
    """
    formatter = logging.Formatter("%(message)s")
    with patch.dict("os.environ", {"LOKI_URL": loki_url}):
        assert build_loki_handler(formatter, "test-service") is None


@pytest.mark.parametrize(
    "given",
    [
        "http://localhost:3100",
        "http://localhost:3100/",
        "http://localhost:3100/loki/api/v1/push",
    ],
    ids=["base", "trailing-slash", "full-push-url"],
)
def test_accepts_base_or_full_push_url(given):
    h = LokiHandler(given, dict(_LABELS))
    try:
        assert h._push_url == "http://localhost:3100/loki/api/v1/push"
    finally:
        _stop_thread(h)
        h.close()


# -- leak backstop ---------------------------------------------------------


def test_no_shipper_threads_leaked():
    """No loki-shipper thread may outlive the tests above.

    A leaked shipper is not a tidiness problem. One escaped this module during
    development attached to a mocked queue, where `_safe_push` turned each
    malformed item into a caught exception and the loop spun at full tilt for the
    remainder of the session — enough contention to time out unrelated tests far
    away in the suite. Declared last so it observes the other tests' teardown.
    """
    alive = [t.name for t in threading.enumerate() if t.name == "loki-shipper"]
    assert not alive, f"leaked {len(alive)} loki-shipper thread(s): {alive}"
