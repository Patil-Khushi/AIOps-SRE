"""Prometheus metrics for the User Service.

Metric names follow the project spec so dashboards/alerts and the AIOps agents
can rely on stable names.
"""
from prometheus_client import Counter, Gauge, Histogram

login_requests_total = Counter(
    "login_requests_total",
    "Total number of login attempts received.",
)

login_failure_total = Counter(
    "login_failure_total",
    "Total number of failed login attempts.",
    ["reason"],  # invalid_credentials | db_error | unknown
)

login_latency_seconds = Histogram(
    "login_latency_seconds",
    "Latency of the /login endpoint in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30),
)

# 1 = MySQL reachable, 0 = not. Scrape-time value is refreshed on DB pings.
mysql_connection_status = Gauge(
    "mysql_connection_status",
    "Whether the User Service can reach MySQL (1=up, 0=down).",
)