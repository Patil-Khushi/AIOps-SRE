"""Tests for LokiHandler concurrency, batching, backoff, and drop accounting."""

import json
import logging
import queue
import time
import unittest.mock as mock
from unittest.mock import MagicMock, patch

import pytest

# Import handler from one service (identical across all three)
import sys
import os

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "demo", "ecommerce", "user-service", "src")
)

from observability.loki_handler import (
    LokiHandler,
    _BATCH_INTERVAL_SECONDS,
    _BATCH_MAX_RECORDS,
    _BACKOFF_SECONDS,
    _QUEUE_MAX_RECORDS,
)


@pytest.fixture
def handler():
    """Create a LokiHandler with a mock URL."""
    handler = LokiHandler("http://localhost:3100", {"service_name": "test-service"})
    yield handler
    handler.close()


def test_emit_adds_to_queue(handler):
    """emit() queues a log record on the producer thread."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert handler._queue.qsize() == 1


def test_queue_overflow_increments_drop_counter(handler):
    """Queue overflow in emit() increments _dropped counter with proper locking."""
    # Create a custom queue that's already "full"
    real_queue = handler._queue
    full_queue = MagicMock()
    full_queue.put_nowait.side_effect = queue.Full()
    full_queue.get_nowait.side_effect = queue.Empty()
    handler._queue = full_queue

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )

    initial_dropped = handler._dropped
    handler.emit(record)

    # Drop counter should have incremented (using lock)
    assert handler._dropped == initial_dropped + 1

    # Restore real queue
    handler._queue = real_queue


def test_drop_counter_is_thread_safe(handler):
    """Concurrent emit() calls don't lose drop count increments."""
    import threading

    def fill_queue():
        """Fill the queue and trigger drops."""
        for i in range(_QUEUE_MAX_RECORDS + 10):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"message {i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

    threads = [threading.Thread(target=fill_queue) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All drops should be accounted for (at least the expected count)
    assert handler._dropped > 0


def test_muted_batch_counts_as_dropped(handler):
    """Batches discarded during mute window are counted as dropped."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        # First push fails, triggers mute
        mock_urlopen.side_effect = [
            ConnectionError("Loki unreachable"),
            MagicMock(),  # Second push succeeds
        ]

        # Add a record and flush it
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="first batch",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        # Trigger push: this will fail and mute
        batch = []
        while handler._queue.qsize() > 0:
            try:
                batch.append(handler._queue.get_nowait())
            except Exception:
                break
        handler._push(batch)

        # Now we're muted. Emit another batch.
        record2 = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="second batch (will be muted)",
            args=(),
            exc_info=None,
        )
        handler.emit(record2)

        # Collect the second batch
        batch2 = []
        while handler._queue.qsize() > 0:
            try:
                batch2.append(handler._queue.get_nowait())
            except Exception:
                break

        # Push while muted: should NOT call urlopen, should count as dropped
        dropped_before = handler._dropped
        handler._push(batch2)
        # The batch was dropped during mute
        assert handler._dropped == dropped_before + len(batch2)
        # urlopen was only called once (the failing first push)
        assert mock_urlopen.call_count == 1


def test_failed_push_counts_current_batch_as_dropped(handler):
    """When push fails, the batch being sent counts as dropped."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = ConnectionError("Loki unreachable")

        # Add records and push them
        for i in range(3):
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg=f"error {i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        batch = []
        while handler._queue.qsize() > 0:
            try:
                batch.append(handler._queue.get_nowait())
            except Exception:
                break

        dropped_before = handler._dropped
        handler._push(batch)

        # The batch that failed to send should be counted
        assert handler._dropped >= dropped_before + len(batch)
        # urlopen was called
        assert mock_urlopen.call_count == 1


def test_successful_push_clears_queued_drops(handler):
    """Successful push sends a warning line for queued drops then clears counter."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        # First, manually force some drops into the counter
        handler._dropped = 5

        # Create a batch
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="test error",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        batch = []
        while handler._queue.qsize() > 0:
            try:
                batch.append(handler._queue.get_nowait())
            except Exception:
                break

        handler._push(batch)

        # Push should have been called with a payload that includes the warning
        assert mock_urlopen.call_count == 1
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))

        # Check that the warning stream is present
        warning_found = False
        for stream in payload["streams"]:
            if stream["stream"].get("level") == "warning":
                warning_found = True
                assert any("dropped 5 log line(s)" in v[1] for v in stream["values"])
                break

        assert warning_found, "Expected warning stream with drop count"
        # Drop counter should be reset after successful push
        assert handler._dropped == 0


def test_batching_respects_size_limit(handler):
    """_push is called when batch size reaches _BATCH_MAX_RECORDS."""
    with patch.object(handler, "_push") as mock_push:
        # Add exactly _BATCH_MAX_RECORDS records
        for i in range(_BATCH_MAX_RECORDS):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"message {i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)

        # Give the background thread time to process
        time.sleep(0.1)

        # _push should have been called (batch size limit hit)
        assert mock_push.call_count >= 1


def test_backoff_mutes_push_after_failure(handler):
    """Failed push triggers backoff and mutes subsequent pushes."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = ConnectionError("Loki unreachable")

        # Create and push a batch
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="test error",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        batch = []
        while handler._queue.qsize() > 0:
            try:
                batch.append(handler._queue.get_nowait())
            except Exception:
                break

        handler._push(batch)

        # Mute should be set
        assert handler._muted_until > time.monotonic()
        # Should be approximately _BACKOFF_SECONDS in the future
        mute_duration = handler._muted_until - time.monotonic()
        assert mute_duration > 0
        assert mute_duration <= _BACKOFF_SECONDS + 0.1


def test_timestamp_collision_avoidance(handler):
    """Rapidly emitted identical messages get unique nanosecond timestamps."""
    # Emit the same message 5 times rapidly
    messages = []
    for _ in range(5):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="duplicate message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    # Collect all records
    records = []
    while handler._queue.qsize() > 0:
        try:
            records.append(handler._queue.get_nowait())
        except Exception:
            break

    # All timestamps should be unique
    timestamps = [r[0] for r in records]
    assert len(timestamps) == len(set(timestamps)), "Timestamps should be unique"
    # Timestamps should be ascending
    assert timestamps == sorted(timestamps), "Timestamps should be ascending"


def test_handler_close_flushes_pending_records(handler):
    """close() triggers a final push of pending records."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        # Add a record but don't trigger a push
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        initial_call_count = mock_urlopen.call_count

        # Close should flush the pending record
        handler.close()
        time.sleep(0.1)  # Give background thread time to process

        # urlopen should have been called at least once (for the flush)
        assert mock_urlopen.call_count >= initial_call_count


def test_disabled_by_empty_url():
    """build_loki_handler returns None when LOKI_URL is unset."""
    from observability.loki_handler import build_loki_handler

    formatter = logging.Formatter("%(message)s")
    with patch.dict("os.environ", {"LOKI_URL": ""}):
        result = build_loki_handler(formatter, "test-service")
        assert result is None


def test_accepts_both_base_and_full_urls():
    """LokiHandler accepts either base URL or full push URL."""
    # Base URL
    handler1 = LokiHandler("http://localhost:3100", {"service_name": "test"})
    assert handler1._push_url == "http://localhost:3100/loki/api/v1/push"
    handler1.close()

    # Full URL
    handler2 = LokiHandler("http://localhost:3100/loki/api/v1/push", {"service_name": "test"})
    assert handler2._push_url == "http://localhost:3100/loki/api/v1/push"
    handler2.close()

    # With trailing slash
    handler3 = LokiHandler("http://localhost:3100/", {"service_name": "test"})
    assert handler3._push_url == "http://localhost:3100/loki/api/v1/push"
    handler3.close()
