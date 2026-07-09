"""Loki provider for the ``observability.logs.query`` capability.

Mirrors ``jaeger.py``: an env-configured base URL, a short connect timeout, and
a process-local circuit breaker so an unreachable Loki degrades fast instead of
adding connect-timeout latency to every RA-007 correlate call (the agent fans
out logs/traces/metrics in a ThreadPoolExecutor).

Wire contract with the Log Correlation agent (RA-007): ``_fetch_logs`` expects
``ToolResult.data["streams"]`` to be a list of ``{"stream": {labels...},
"values": [[ts_ns, line], ...]}`` and reads the per-stream severity from
``labels["level"]`` (or ``labels["severity"]``). Loki's ``query_range`` returns
``data.result`` in exactly that stream/values shape, so the only transform is
``result -> streams`` plus promoting Loki's auto-detected ``detected_level`` to
``level`` (see ``_map_streams``).
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import httpx

from aiops.tools.registry import ToolResult, tool

_URL = os.environ.get("AIOPS_LOKI_URL", "http://localhost:3100")
_TIMEOUT = float(os.environ.get("AIOPS_LOKI_TIMEOUT", "10"))
# Connect-phase cap (mirrors #113 for Jaeger). When the port-forward to Loki is
# down — common during tests and CI — a long default connect timeout cascades
# across the fan-out and can stall the correlate call. Keep it short.
_CONNECT_TIMEOUT = float(os.environ.get("AIOPS_LOKI_CONNECT_TIMEOUT", "2"))

# Process-local circuit breaker (mirrors #113 for Jaeger). The connect timeout
# above is not enforced reliably on every platform (notably Windows, where a
# refused localhost connect can stall longer than ``connect=...`` suggests).
# After one failure, short-circuit subsequent calls for ``_CIRCUIT_OPEN_SECONDS``
# so a single correlate that fans out doesn't multiply the wall-clock cost. The
# agent already handles ``ToolResult.ok=False`` gracefully (falls back to the
# synthetic path), so callers see a fast "logs: loki error" rather than a hang.
_CIRCUIT_OPEN_SECONDS = float(os.environ.get("AIOPS_LOKI_CIRCUIT_OPEN_SECONDS", "30"))
_circuit_open_until: float = 0.0


def _reset_circuit_for_tests() -> None:
    """Reset the circuit breaker. Test seam only (mirrors ``jaeger``).

    The breaker is process-local module state, so it survives across pytest
    test boundaries — a test that trips it (e.g. on a mocked socket failure)
    would cause the next 30s of tests to short-circuit even when their own
    httpx mocks are set to succeed.  ``tests/conftest.py`` calls this in an
    autouse fixture so the breaker is fresh per-test.
    """
    global _circuit_open_until
    _circuit_open_until = 0.0


def _to_nanos(value: Any) -> str:
    """Coerce an ISO-8601 string / datetime / epoch number to unix-nanoseconds.

    ``query_range`` accepts RFC3339 or unix-nanoseconds; nanoseconds are the
    unambiguous form across Loki versions. The RA-007 agent passes
    ``window.start.isoformat()`` (a tz-aware ISO string), but accept datetimes
    and numeric epochs too so the provider is robust to any caller.

    Assumes timezone-aware inputs: ``datetime.timestamp()`` interprets a
    tz-naive datetime (and a naive ISO string with no offset) as *local* wall
    time, so a naive value silently shifts the window by the host's UTC offset.
    The agent always passes tz-aware UTC, so this is a documented caller
    contract rather than a guard — a raise would break the "robust to any
    caller" promise for the common tz-aware path."""
    if isinstance(value, datetime):
        return str(int(value.timestamp() * 1e9))
    if isinstance(value, (int, float)):
        # Heuristic: treat large values as already-nanoseconds, else epoch secs.
        return str(int(value)) if value > 1e12 else str(int(value * 1e9))
    s = str(value).strip()
    if s.isdigit():
        return s
    normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
    return str(int(datetime.fromisoformat(normalized).timestamp() * 1e9))


def _get(path: str, params: dict[str, str] | None = None) -> ToolResult:
    global _circuit_open_until
    now = time.monotonic()
    if now < _circuit_open_until:
        return ToolResult(ok=False, error="HTTPError: circuit open (Loki unreachable)")
    try:
        r = httpx.get(
            f"{_URL}{path}",
            params=params,
            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )
        r.raise_for_status()
        body = r.json()
    # Catch OSError as well as httpx errors so socket-level failures
    # (ConnectionRefusedError, OSError on Windows when no listener is bound)
    # also trip the breaker. Same aggressive-for-the-demo posture as Jaeger:
    # a transient hiccup blocks Loki for 30s, cheaper than stalling every
    # correlate on a real outage. Tune ``AIOPS_LOKI_CIRCUIT_OPEN_SECONDS``.
    except (httpx.HTTPError, OSError) as exc:
        _circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
        return ToolResult(ok=False, error=f"HTTPError: {exc}")
    # A 200 with a non-JSON body (nginx/proxy error page, Loki startup splash,
    # a TLS-inspecting proxy intercept) makes ``r.json()`` raise
    # ``json.JSONDecodeError`` — a ``ValueError`` subclass caught by neither
    # ``httpx.HTTPError`` nor ``OSError`` above. Without this branch it would
    # escape ``_get`` and bypass the graceful ToolResult(ok=False) path the
    # agent's synthetic fallback relies on. Loki WAS reachable (we got a 200),
    # so do NOT trip the breaker — just report the bad body.
    except ValueError as exc:
        return ToolResult(ok=False, error=f"loki response parse failed: {exc}")
    if body.get("status") != "success":
        return ToolResult(ok=False, error=body.get("error") or "non-success response")
    return ToolResult(
        ok=True, data=body.get("data") or {}, metadata={"provider": "loki", "url": _URL}
    )


def _map_streams(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map Loki ``query_range`` ``data.result`` to the agent's ``streams`` shape.

    Each Loki stream is ``{"stream": {labels}, "values": [[ts_ns, line, meta?]]}``.
    RA-007's ``_fetch_logs`` reads the severity from the stream label
    ``level`` (or ``severity``); Loki records its auto-detected level as
    ``detected_level``. When level-detection lands it as a **stream label** we
    promote it directly; when it lands as per-entry **structured metadata**
    (Loki 3.x, the 3rd value element) we lift the first entry's value up to the
    stream label. Either way the agent finds a ``level``; the untouched 3rd
    element is ignored by the agent's ``[*entry, "", ""][:2]`` slice."""
    streams: list[dict[str, Any]] = []
    for s in data.get("result", []) or []:
        labels = dict(s.get("stream", {}) or {})
        values = s.get("values", []) or []
        if "level" not in labels:
            promoted = labels.get("detected_level")
            if not promoted:
                # Look for detected_level in the first entry's structured metadata.
                for entry in values:
                    if len(entry) >= 3 and isinstance(entry[2], dict):
                        promoted = entry[2].get("detected_level") or entry[2].get("level")
                        if promoted:
                            break
            if promoted:
                labels["level"] = promoted
                # Drop the source key once promoted so the stream dict carries a
                # single, unambiguous severity label for downstream consumers.
                labels.pop("detected_level", None)
        streams.append({"stream": labels, "values": values})
    return streams


@tool(
    name="loki.observability.logs.query",
    capability="observability.logs.query",
    provider="loki",
    description="Query Loki for a service's log lines in a time window. Returns log streams.",
)
def query(service: str, start: Any, end: Any, limit: int = 200) -> ToolResult:
    """Range-query Loki for ``{service_name="<service>"}`` between ``start`` and
    ``end``. ``start``/``end`` accept ISO-8601 strings (what RA-007 passes),
    datetimes, or epoch numbers; ``limit`` caps returned lines."""
    # Escape the label value so a service name with a quote/backslash can't break
    # out of the LogQL stream selector (mirrors prometheus.py's promql escaping).
    svc = str(service).replace("\\", "\\\\").replace('"', '\\"')
    res = _get(
        "/loki/api/v1/query_range",
        params={
            "query": f'{{service_name="{svc}"}}',
            "start": _to_nanos(start),
            "end": _to_nanos(end),
            "limit": str(limit),
            "direction": "backward",
        },
    )
    if not res.ok:
        return res
    return ToolResult(
        ok=True,
        data={"streams": _map_streams(res.data or {})},
        metadata=res.metadata,
    )
