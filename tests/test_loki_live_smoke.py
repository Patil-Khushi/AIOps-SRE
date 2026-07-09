"""Live smoke test for the Loki provider + RA-007 logs leg (#220).

Marked ``integration`` and SKIPPED when Loki isn't reachable, so CI documents
the real path without becoming flaky. Run against a live stack with:

    kubectl -n otel-demo port-forward svc/loki 3100:3100   # or .\\start.ps1
    uv run pytest -m integration tests/test_loki_live_smoke.py

The default suite (``uv run pytest``) skips these — the conftest pins
AIOPS_LOKI_URL at 127.0.0.1:1, which refuses instantly and trips the skip.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

_LOKI_URL = os.environ.get("AIOPS_LOKI_URL", "http://localhost:3100")


def _loki_reachable() -> bool:
    """True only if Loki answers its readiness probe quickly."""
    try:
        r = httpx.get(f"{_LOKI_URL}/ready", timeout=httpx.Timeout(3.0, connect=1.0))
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


requires_loki = pytest.mark.skipif(
    not _loki_reachable(),
    reason=f"Loki not reachable at {_LOKI_URL} (port-forward svc/loki 3100 to run)",
)


@requires_loki
def test_loki_labels_include_service_name():
    """The demo's OTLP logs must land in Loki with a ``service_name`` label —
    that's the label the RA-007 logs provider queries on. A mismatch here is the
    #1 way the live path silently degrades to synthetic."""
    r = httpx.get(f"{_LOKI_URL}/loki/api/v1/labels", timeout=5.0)
    r.raise_for_status()
    labels = r.json().get("data", [])
    assert "service_name" in labels, f"expected service_name label, got {labels}"


@requires_loki
def test_provider_query_returns_live_streams():
    """The registered ``observability.logs.query`` provider returns real Loki
    streams mapped into the agent's shape (``data.streams`` with a ``level``)."""
    from datetime import UTC, datetime, timedelta

    import aiops.tools.observability  # noqa: F401  (registers providers)
    from aiops.tools import get_registry

    end = datetime.now(UTC)
    start = end - timedelta(minutes=30)
    # product-catalog is the highest-traffic demo service, so it always has
    # recent lines; skip rather than fail if this particular cluster is idle.
    res = get_registry().call(
        "observability.logs.query",
        service="product-catalog",
        start=start.isoformat(),
        end=end.isoformat(),
        limit=50,
    )
    assert res.ok, f"provider call failed: {res.error}"
    streams = (res.data or {}).get("streams")
    assert isinstance(streams, list)
    if not streams:
        pytest.skip("Loki reachable but no product-catalog logs in the last 30m")
    # Shape the agent depends on: each stream carries labels + [ts, line] values.
    first = streams[0]
    assert "stream" in first and "values" in first
    assert first["values"], "stream has no log lines"


@requires_loki
def test_correlate_live_path_is_marked_live():
    """End-to-end: RA-007 correlate() over a recent window pulls live Loki logs
    and stamps signal_source='live' (Done-when #1)."""
    from datetime import UTC, datetime, timedelta

    from agents.log_correlation import CorrelationInput, correlate

    end = datetime.now(UTC)
    start = end - timedelta(minutes=30)
    result = correlate(
        CorrelationInput(
            service="product-catalog",
            window={"start": start.isoformat(), "end": end.isoformat()},
        )
    )
    log_lines = [s for s in result.timeline if s.source == "logs"]
    if not log_lines:
        pytest.skip("Loki reachable but no product-catalog logs in the last 30m")
    assert result.audit_metadata.signal_source == "live"
    assert any("from loki" in line for line in result.audit_metadata.decision_trace)
