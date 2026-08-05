"""Prometheus metrics for the Payment Service."""

from prometheus_client import Counter, Gauge, Histogram

payment_requests_total = Counter(
    "payment_requests_total",
    "Total number of payment requests received.",
)

payment_failures_total = Counter(
    "payment_failures_total",
    "Total number of failed payments.",
    ["reason"],  # redis_error | gateway_timeout | gateway_error | injected_500
)

payment_latency_seconds = Histogram(
    "payment_latency_seconds",
    "Latency of the POST /payments endpoint in seconds.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)

redis_connection_status = Gauge(
    "redis_connection_status",
    "Whether the Payment Service can reach Redis (1=up, 0=down).",
)
