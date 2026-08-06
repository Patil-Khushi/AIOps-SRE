"""Prometheus metrics for the Order Service (names per project spec)."""

from prometheus_client import Counter, Gauge, Histogram

orders_created_total = Counter(
    "orders_created_total",
    "Total number of orders successfully created.",
)

orders_failed_total = Counter(
    "orders_failed_total",
    "Total number of orders that failed.",
    ["reason"],  # db_error | user_invalid | payment_failed | payment_timeout | injected_500
)

payment_timeout_total = Counter(
    "payment_timeout_total",
    "Total number of payment calls that timed out.",
)

order_latency_seconds = Histogram(
    "order_latency_seconds",
    "Latency of the POST /orders endpoint in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)

postgres_connection_status = Gauge(
    "postgres_connection_status",
    "Whether the Order Service can reach PostgreSQL (1=up, 0=down).",
)
