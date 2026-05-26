"""Unit tests for the Jaeger circuit breaker (#113).

The breaker is what unhangs the full pytest suite when no kubectl
port-forward to Jaeger is up. A single failure must short-circuit
subsequent calls for ``_CIRCUIT_OPEN_SECONDS`` so a triage that fans
out to N Jaeger calls doesn't multiply the wall-clock cost by N.
"""

from __future__ import annotations

import httpx
import pytest

from aiops.tools.observability import jaeger


def test_first_failure_trips_circuit_breaker(monkeypatch):
    """A connect error on the first call must store the open-until
    timestamp so the *second* call returns the short-circuit error
    without re-attempting the socket."""

    call_count = {"n": 0}

    def _failing_get(*_args, **_kwargs):
        call_count["n"] += 1
        raise httpx.ConnectError("simulated connect refused")

    monkeypatch.setattr(jaeger.httpx, "get", _failing_get)

    res1 = jaeger._get("/api/services")
    assert res1.ok is False
    assert call_count["n"] == 1, "first call must reach httpx.get"
    assert "simulated connect refused" in (res1.error or "")

    res2 = jaeger._get("/api/services")
    assert res2.ok is False
    assert call_count["n"] == 1, (
        "second call must short-circuit — httpx.get must NOT be invoked again"
    )
    assert "circuit open" in (res2.error or "")


def test_oserror_also_trips_breaker(monkeypatch):
    """Socket-level ``OSError`` (e.g. ``ConnectionRefusedError`` on
    Windows where it isn't always wrapped as ``httpx.HTTPError``) must
    also open the circuit, not bubble up as an unhandled exception."""

    def _raising_oserror(*_args, **_kwargs):
        raise OSError(111, "Connection refused")

    monkeypatch.setattr(jaeger.httpx, "get", _raising_oserror)

    res = jaeger._get("/api/services")
    assert res.ok is False
    assert "Connection refused" in (res.error or "")

    # Circuit should now be open.
    res2 = jaeger._get("/api/services")
    assert "circuit open" in (res2.error or "")


def test_circuit_reopens_after_window_elapses(monkeypatch):
    """Once the configured window passes, the next call must try the
    real socket again rather than staying broken forever."""

    fake_time = {"now": 1000.0}
    monkeypatch.setattr(jaeger.time, "monotonic", lambda: fake_time["now"])

    call_count = {"n": 0}

    def _failing_get(*_args, **_kwargs):
        call_count["n"] += 1
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(jaeger.httpx, "get", _failing_get)

    # First call trips the breaker.
    jaeger._get("/api/services")
    assert call_count["n"] == 1

    # Within the window: short-circuited.
    fake_time["now"] += jaeger._CIRCUIT_OPEN_SECONDS - 1
    res = jaeger._get("/api/services")
    assert "circuit open" in (res.error or "")
    assert call_count["n"] == 1, "still short-circuited within the window"

    # Past the window: real attempt again.
    fake_time["now"] += 2
    res = jaeger._get("/api/services")
    assert call_count["n"] == 2, "must retry the real socket after the window"
    assert "simulated" in (res.error or "")


def test_reset_for_tests_clears_open_state(monkeypatch):
    """The test seam used by ``tests/conftest.py`` must actually clear
    the open-until timestamp, otherwise the autouse fixture is a no-op."""

    def _failing_get(*_args, **_kwargs):
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(jaeger.httpx, "get", _failing_get)

    jaeger._get("/api/services")
    assert jaeger._circuit_open_until > 0.0, "first failure should arm the breaker"

    jaeger._reset_circuit_for_tests()
    assert jaeger._circuit_open_until == 0.0


def test_successful_call_does_not_trip_breaker(monkeypatch):
    """A green call must leave the breaker closed so subsequent calls
    keep going to the real socket."""

    class _OkResponse:
        @staticmethod
        def raise_for_status() -> None:
            pass

        @staticmethod
        def json() -> dict[str, list[str]]:
            return {"data": ["frontend", "checkout"]}

    monkeypatch.setattr(jaeger.httpx, "get", lambda *_a, **_kw: _OkResponse())

    res = jaeger._get("/api/services")
    assert res.ok is True
    assert jaeger._circuit_open_until == 0.0, "successful call must not arm the breaker"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.RemoteProtocolError("malformed http"),
    ],
)
def test_other_httpx_errors_also_trip_breaker(monkeypatch, exc):
    """The breaker should fire on any ``httpx.HTTPError`` subclass, not
    just ``ConnectError`` — read timeouts and protocol errors are equally
    indicative that this Jaeger isn't healthy right now."""

    monkeypatch.setattr(jaeger.httpx, "get", lambda *_a, **_kw: (_ for _ in ()).throw(exc))

    res = jaeger._get("/api/services")
    assert res.ok is False
    assert jaeger._circuit_open_until > 0.0
