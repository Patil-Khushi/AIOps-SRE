"""Smoke tests that don't require a live Redis or gateway."""
import os

os.environ.setdefault("REDIS_HOST", "localhost")

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def test_metrics_exposed():
    r = client.get("/metrics")
    assert r.status_code == 200
    for name in [
        b"payment_requests_total",
        b"payment_failures_total",
        b"payment_latency_seconds",
        b"redis_connection_status",
    ]:
        assert name in r.content


def test_health_reports_redis_state():
    r = client.get("/health")
    assert r.status_code == 200
    assert "redis" in r.json()


def test_missing_payment_is_404_or_500():
    # No live Redis in CI: either not-found (fakeredis) or storage error.
    r = client.get("/payments/does-not-exist")
    assert r.status_code in (404, 500)