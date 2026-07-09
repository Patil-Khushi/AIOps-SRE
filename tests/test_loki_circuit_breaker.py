"""Unit tests for the Loki provider (RA-007 logs backend).

Two concerns:

- The process-local **circuit breaker** (mirrors the Jaeger breaker, #113):
  a single failure must short-circuit subsequent calls for
  ``_CIRCUIT_OPEN_SECONDS`` so RA-007's logs/traces/metrics fan-out doesn't
  multiply the wall-clock cost when Loki is unreachable.
- The **wire mapping**: Loki ``query_range`` returns ``data.result``; the agent
  reads ``data.streams`` with a per-stream ``level`` label, so the provider must
  rename ``result -> streams`` and promote Loki's ``detected_level``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aiops.tools.observability import loki

_QR = "/loki/api/v1/query_range"


class _OkResponse:
    """Minimal stand-in for an httpx.Response with a canned JSON body."""

    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


# ─── circuit breaker (mirrors test_jaeger_circuit_breaker.py) ────────────────


def test_first_failure_trips_circuit_breaker(monkeypatch):
    call_count = {"n": 0}

    def _failing_get(*_args, **_kwargs):
        call_count["n"] += 1
        raise httpx.ConnectError("simulated connect refused")

    monkeypatch.setattr(loki.httpx, "get", _failing_get)

    res1 = loki._get(_QR)
    assert res1.ok is False
    assert call_count["n"] == 1, "first call must reach httpx.get"
    assert "simulated connect refused" in (res1.error or "")

    res2 = loki._get(_QR)
    assert res2.ok is False
    assert call_count["n"] == 1, (
        "second call must short-circuit — httpx.get must NOT be invoked again"
    )
    assert "circuit open" in (res2.error or "")


def test_oserror_also_trips_breaker(monkeypatch):
    def _raising_oserror(*_args, **_kwargs):
        raise OSError(111, "Connection refused")

    monkeypatch.setattr(loki.httpx, "get", _raising_oserror)

    res = loki._get(_QR)
    assert res.ok is False
    assert "Connection refused" in (res.error or "")

    res2 = loki._get(_QR)
    assert "circuit open" in (res2.error or "")


def test_circuit_reopens_after_window_elapses(monkeypatch):
    fake_time = {"now": 1000.0}
    monkeypatch.setattr(loki.time, "monotonic", lambda: fake_time["now"])

    call_count = {"n": 0}

    def _failing_get(*_args, **_kwargs):
        call_count["n"] += 1
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(loki.httpx, "get", _failing_get)

    loki._get(_QR)
    assert call_count["n"] == 1

    fake_time["now"] += loki._CIRCUIT_OPEN_SECONDS - 1
    res = loki._get(_QR)
    assert "circuit open" in (res.error or "")
    assert call_count["n"] == 1, "still short-circuited within the window"

    fake_time["now"] += 2
    res = loki._get(_QR)
    assert call_count["n"] == 2, "must retry the real socket after the window"
    assert "simulated" in (res.error or "")


def test_reset_for_tests_clears_open_state(monkeypatch):
    def _failing_get(*_args, **_kwargs):
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(loki.httpx, "get", _failing_get)

    loki._get(_QR)
    assert loki._circuit_open_until > 0.0, "first failure should arm the breaker"

    loki._reset_circuit_for_tests()
    assert loki._circuit_open_until == 0.0


def test_successful_call_does_not_trip_breaker(monkeypatch):
    body = {"status": "success", "data": {"resultType": "streams", "result": []}}
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _OkResponse(body))

    res = loki._get(_QR)
    assert res.ok is True
    assert loki._circuit_open_until == 0.0, "successful call must not arm the breaker"


def test_non_success_body_is_not_ok(monkeypatch):
    """A 200 with ``status != success`` (Loki error payload) must surface as
    ``ok=False`` without tripping the breaker (it reached Loki fine)."""
    body = {"status": "error", "error": "parse error"}
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _OkResponse(body))

    res = loki._get(_QR)
    assert res.ok is False
    assert "parse error" in (res.error or "")
    assert loki._circuit_open_until == 0.0


def test_non_json_200_does_not_trip_breaker(monkeypatch):
    """A 200 with a non-JSON body (proxy error page / startup splash) must
    surface as ``ok=False`` WITHOUT tripping the breaker — Loki was reachable,
    only the body was unparseable. Regression guard for the ValueError branch."""

    class _BadJson:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            raise json.JSONDecodeError("expecting value", "", 0)

    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _BadJson())

    res = loki._get(_QR)
    assert res.ok is False
    assert "parse failed" in (res.error or "")
    assert loki._circuit_open_until == 0.0, "bad body must NOT arm the breaker"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.RemoteProtocolError("malformed http"),
    ],
)
def test_other_httpx_errors_also_trip_breaker(monkeypatch, exc):
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: (_ for _ in ()).throw(exc))

    res = loki._get(_QR)
    assert res.ok is False
    assert loki._circuit_open_until > 0.0


# ─── wire mapping (result -> streams, detected_level -> level) ───────────────


def test_query_maps_result_to_streams_and_promotes_label(monkeypatch):
    """``data.result`` becomes ``data.streams``; a stream-label ``detected_level``
    is promoted to ``level`` (what the agent's _fetch_logs reads)."""
    body = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "product-catalog", "detected_level": "error"},
                    "values": [["1690000000000000000", "GetProduct slow"]],
                }
            ],
        },
    }
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _OkResponse(body))

    res = loki.query(
        service="product-catalog",
        start="2026-07-08T10:00:00+00:00",
        end="2026-07-08T10:15:00+00:00",
    )
    assert res.ok is True
    streams = res.data["streams"]
    assert len(streams) == 1
    assert streams[0]["stream"]["level"] == "error"
    assert streams[0]["values"][0][1] == "GetProduct slow"


def test_query_promotes_structured_metadata_level(monkeypatch):
    """When Loki emits ``detected_level`` as per-entry structured metadata (the
    3rd value element, Loki 3.x) rather than a stream label, the provider lifts
    it to the stream ``level`` so the agent still sees a severity."""
    body = {
        "status": "success",
        "data": {
            "result": [
                {
                    "stream": {"service_name": "payment"},
                    "values": [
                        ["1690000000000000000", "charge failed", {"detected_level": "critical"}],
                    ],
                }
            ],
        },
    }
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _OkResponse(body))

    res = loki.query(service="payment", start="2026-07-08T10:00:00Z", end="2026-07-08T10:15:00Z")
    assert res.ok is True
    assert res.data["streams"][0]["stream"]["level"] == "critical"


def test_query_leaves_existing_level_untouched(monkeypatch):
    """An explicit ``level`` label wins over ``detected_level``."""
    body = {
        "status": "success",
        "data": {
            "result": [
                {
                    "stream": {"service_name": "cart", "level": "warn", "detected_level": "error"},
                    "values": [["1690000000000000000", "cart rpc failed"]],
                }
            ],
        },
    }
    monkeypatch.setattr(loki.httpx, "get", lambda *_a, **_kw: _OkResponse(body))

    res = loki.query(service="cart", start="2026-07-08T10:00:00Z", end="2026-07-08T10:15:00Z")
    assert res.data["streams"][0]["stream"]["level"] == "warn"


# ─── timestamp + selector helpers ────────────────────────────────────────────


def test_to_nanos_accepts_iso_and_epoch():
    from datetime import UTC, datetime

    dt = datetime(2026, 7, 8, 10, 0, 0, tzinfo=UTC)
    expected = str(int(dt.timestamp() * 1e9))
    assert loki._to_nanos("2026-07-08T10:00:00+00:00") == expected
    assert loki._to_nanos("2026-07-08T10:00:00Z") == expected
    assert loki._to_nanos(dt) == expected
    # Already-nanoseconds passes through; epoch-seconds is scaled up.
    assert loki._to_nanos("1690000000000000000") == "1690000000000000000"
    assert loki._to_nanos(1690000000) == str(int(1690000000 * 1e9))


def test_query_escapes_service_in_selector(monkeypatch):
    """A service name with a quote can't break out of the LogQL selector."""
    captured = {}

    def _capture(url, params=None, timeout=None):
        captured["params"] = params
        return _OkResponse({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(loki.httpx, "get", _capture)
    loki.query(service='pay"; drop', start="2026-07-08T10:00:00Z", end="2026-07-08T10:15:00Z")
    assert captured["params"]["query"] == '{service_name="pay\\"; drop"}'


def test_query_escapes_backslash_in_selector(monkeypatch):
    """A backslash is escaped first (before the quote), so a service name with
    a trailing backslash can't escape the LogQL selector."""
    captured = {}

    def _capture(url, params=None, timeout=None):
        captured["params"] = params
        return _OkResponse({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(loki.httpx, "get", _capture)
    loki.query(service="pay\\", start="2026-07-08T10:00:00Z", end="2026-07-08T10:15:00Z")
    # 'pay\' -> backslash doubled -> 'pay\\', wrapped in the selector.
    assert captured["params"]["query"] == '{service_name="pay\\\\"}'
