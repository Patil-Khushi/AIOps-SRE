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


def test_service_with_quote_is_escaped_in_promql(monkeypatch) -> None:
    """A service name containing ``"`` must NOT terminate the label-matcher
    string. Result: ``service_name="foo\\"bar"`` — exactly one matcher,
    with the inner quote escaped."""
    from agents.alert_triage.agent import _build_promql_queries

    queries = _build_promql_queries(_alert_with_service('foo"bar'))
    for name, q in queries.items():
        # The literal escaped form appears in every query.
        assert 'service_name="foo\\"bar"' in q, f"{name}: {q}"
        # Sanity: there are exactly two unescaped `"` characters bracketing
        # the value — no third one from a broken-out matcher.
        # Count quotes that are NOT preceded by a backslash.
        unescaped_quotes = sum(
            1 for i, c in enumerate(q) if c == '"' and (i == 0 or q[i - 1] != "\\")
        )
        # Each query has multiple service_name="..." matchers (and possibly
        # http_status_code=~"..."), so just verify it's even.
        assert unescaped_quotes % 2 == 0, (
            f"{name}: odd number of unescaped quotes — matcher broken: {q}"
        )


def test_service_with_backslash_is_escaped(monkeypatch) -> None:
    from agents.alert_triage.agent import _build_promql_queries

    queries = _build_promql_queries(_alert_with_service("foo\\bar"))
    for name, q in queries.items():
        assert 'service_name="foo\\\\bar"' in q, f"{name}: {q}"


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
