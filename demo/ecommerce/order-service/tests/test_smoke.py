"""Smoke tests that don't require live Postgres or downstream services."""

import os

os.environ.setdefault("POSTGRES_HOST", "localhost")

from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_metrics_exposed():
    r = client.get("/metrics")
    assert r.status_code == 200
    for name in [
        b"orders_created_total",
        b"orders_failed_total",
        b"payment_timeout_total",
        b"order_latency_seconds",
    ]:
        assert name in r.content


def test_health_reports_postgres_state():
    r = client.get("/health")
    assert r.status_code == 200
    assert "postgres" in r.json()


def test_orders_requires_auth():
    # No Authorization header -> user validation fails -> 401.
    r = client.get("/orders")
    assert r.status_code == 401
