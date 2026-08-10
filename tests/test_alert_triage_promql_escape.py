"""Tests for PromQL label-value escaping in Stage 4 query construction.

A service name with special characters used to be interpolated raw into
``service_name="..."`` and could break out of the matcher (quote) or
inject extra PromQL (closing brace + operator). These tests pin the
escape contract and verify the queries built by ``_build_promql_queries``
remain syntactically intact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

# ─── pure helper tests ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # No special characters: passes through unchanged.
        ("payment", "payment"),
        ("payment-service", "payment-service"),
        # The three escape transforms.
        ('foo"bar', 'foo\\"bar'),
        ("foo\\bar", "foo\\\\bar"),
        ("foo\nbar", "foo\\nbar"),
        # Order matters: backslash must be doubled BEFORE quotes are escaped,
        # otherwise '\"' produced by the quote-pass would itself become '\\"'.
        ('foo"\\bar', 'foo\\"\\\\bar'),
        # Empty string (defensive — Stage 1 validation prevents this, but
        # the helper must still behave).
        ("", ""),
    ],
)
def test_escape_promql_label_value(raw: str, expected: str) -> None:
    from agents.alert_triage.agent import _escape_promql_label_value

    assert _escape_promql_label_value(raw) == expected


# ─── query-construction integration ────────────────────────────────────────


def _alert_with_service(service: str) -> Any:
    from agents.alert_triage import Alert

    return Alert(
        alert_id="ALT-Q",
        service=service,
        metric="cpu",
        value=90.0,
        timestamp=datetime.now(UTC),
        source="Prometheus",
        labels={},
        annotations={},
    )


# Queries come in two families with different matchers, so a blanket
# "every query contains service_name=" assertion no longer holds:
#
#   - OTLP HTTP metrics are labelled by service and use an exact matcher,
#     ``service_name="<svc>"``.
#   - cAdvisor container metrics are labelled by pod, not service, and use a
#     regex matcher, ``pod=~"<workload>-.*"``. They also drop the ``ecommerce-``
#     telemetry prefix, because the Deployment carries no prefix.
#
# Both must survive a hostile service name; the assertion just has to know
# which family it is looking at.
_POD_MATCHER_QUERIES = {"cpu_seconds_rate", "memory_bytes"}


def _assert_quotes_balanced(name: str, q: str) -> None:
    """No unescaped quote may break out of a label-matcher value."""
    unescaped_quotes = sum(1 for i, c in enumerate(q) if c == '"' and (i == 0 or q[i - 1] != "\\"))
    assert unescaped_quotes % 2 == 0, (
        f"{name}: odd number of unescaped quotes — matcher broken: {q}"
    )


def test_service_with_quote_is_escaped_in_promql(monkeypatch) -> None:
    """A service name containing ``"`` must NOT terminate the label-matcher
    string — in either matcher family."""
    from agents.alert_triage.agent import _build_promql_queries

    queries = _build_promql_queries(_alert_with_service('foo"bar'))
    assert _POD_MATCHER_QUERIES & set(queries), "expected a container-metric query for metric=cpu"
    for name, q in queries.items():
        if name in _POD_MATCHER_QUERIES:
            assert 'pod=~"foo\\"bar-.*"' in q, f"{name}: {q}"
        else:
            assert 'service_name="foo\\"bar"' in q, f"{name}: {q}"
        _assert_quotes_balanced(name, q)


def test_service_with_backslash_is_escaped(monkeypatch) -> None:
    from agents.alert_triage.agent import _build_promql_queries

    queries = _build_promql_queries(_alert_with_service("foo\\bar"))
    for name, q in queries.items():
        if name in _POD_MATCHER_QUERIES:
            # Two escapes compose here: the backslash is escaped once for RE2,
            # then both are escaped again for the PromQL string literal.
            assert 'pod=~"foo\\\\\\\\bar-.*"' in q, f"{name}: {q}"
        else:
            assert 'service_name="foo\\\\bar"' in q, f"{name}: {q}"
        _assert_quotes_balanced(name, q)


def test_pod_matcher_neutralizes_regex_metacharacters() -> None:
    """The pod matcher is a regex, so a service name of ``.*`` must not widen
    the match to every pod in the namespace and enrich the alert with a
    neighbouring service's CPU."""
    from agents.alert_triage.agent import _build_promql_queries

    q = _build_promql_queries(_alert_with_service(".*"))["cpu_seconds_rate"]
    assert 'pod=~"\\\\.\\\\*-.*"' in q, q
    # The bare, executable form must not survive anywhere in the matcher value.
    matcher = q.split('pod=~"', 1)[1].split('",', 1)[0]
    assert matcher == "\\\\.\\\\*-.*", matcher


def test_pod_matcher_strips_the_ecommerce_telemetry_prefix() -> None:
    """``OTEL_SERVICE_NAME`` is ``ecommerce-user-service``; the Deployment — and
    therefore the pod — is plain ``user-service``. Without the strip, the pod
    matcher never matches and CPU/memory enrichment silently returns nothing."""
    from agents.alert_triage.agent import _build_promql_queries

    q = _build_promql_queries(_alert_with_service("ecommerce-user-service"))["cpu_seconds_rate"]
    assert 'pod=~"user-service-.*"' in q, q


def test_container_queries_replaced_collector_self_metrics() -> None:
    """The CPU/memory enrichment used to read ``otelcol_process_*`` — the
    OpenTelemetry Collector's own process metrics. Those described the
    collector's resource use rather than the alerting service's, and the
    collector no longer exists (services export OTLP straight to Jaeger), so the
    series would never resolve."""
    from agents.alert_triage.agent import _build_promql_queries

    cpu = _build_promql_queries(_alert_with_service("order-service"))["cpu_seconds_rate"]
    assert "otelcol_" not in cpu, cpu
    assert "container_cpu_usage_seconds_total" in cpu, cpu

    mem_alert = _alert_with_service("order-service")
    mem_alert.metric = "memory"
    mem = _build_promql_queries(mem_alert)["memory_bytes"]
    assert "otelcol_" not in mem, mem
    assert "container_memory_working_set_bytes" in mem, mem


def test_query_injection_attempt_is_neutralized(monkeypatch) -> None:
    """Adversarial service name attempts to break out of the label matcher
    and append an extra query clause. After escaping, the entire payload
    sits inside the ``service_name="..."`` value — Prometheus will return
    no data for an obviously-impossible service name but the query stays
    syntactically valid (no injection)."""
    from agents.alert_triage.agent import _build_promql_queries

    evil = 'evil"} or vector(1) #'
    queries = _build_promql_queries(_alert_with_service(evil))

    # The injection text appears, but inside an escaped label value.
    request_rate_q = queries["request_rate"]
    assert 'service_name="evil\\"} or vector(1) #"' in request_rate_q
    # The unescaped sequence `"} or vector(1)` (the injection payload outside
    # the value) must NOT appear — that would be the broken-out, executable form.
    assert '"} or vector(1)' not in request_rate_q.replace('\\"', "")
