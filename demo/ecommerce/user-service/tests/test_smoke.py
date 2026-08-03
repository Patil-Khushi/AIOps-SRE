"""Smoke tests that don't require a live MySQL.

Verifies the app imports, exposes /metrics, and that /health degrades (rather
than crashes) when the database is unreachable.
"""
import os

os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def test_metrics_exposed():
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"login_requests_total" in r.content
    assert b"mysql_connection_status" in r.content


def test_health_reports_mysql_state():
    r = client.get("/health")
    assert r.status_code == 200
    assert "mysql" in r.json()


def test_login_requires_body():
    r = client.post("/login", json={})
    assert r.status_code == 422  # pydantic validation