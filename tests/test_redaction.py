"""Tests for the Knowledge Synthesizer PII/secret redactor.

Two things matter: it must catch the secret/PII shapes (no leaks into a
published KB article), and it must NOT over-redact legitimate incident content
(service names, flag names, latency numbers, dates) — over-redaction would
make the knowledge useless.
"""

from __future__ import annotations

import pytest

from agents.knowledge_synthesizer.redaction import redact, scrub

# ─── catches secrets / PII ───────────────────────────────────────────────────


def test_redacts_email():
    r = redact("Paged oncall@example.com about the outage.")
    assert "oncall@example.com" not in r.text
    assert "[REDACTED_EMAIL]" in r.text
    assert r.report.findings["email"] == 1


def test_redacts_ipv4():
    r = redact("Pod IP was 10.42.0.7 during the incident.")
    assert "10.42.0.7" not in r.text
    assert "[REDACTED_IP]" in r.text


def test_redacts_bearer_token():
    r = redact("curl -H 'Authorization: Bearer abc123DEF.token-value'")
    assert "abc123DEF.token-value" not in r.text
    assert "Bearer [REDACTED_TOKEN]" in r.text


def test_redacts_key_value_secret_preserving_key():
    r = redact("Config had password=hunter2 and api_key: sk-9f8a7b6c.")
    assert "hunter2" not in r.text
    assert "sk-9f8a7b6c" not in r.text
    # The key name is preserved so the redaction is auditable.
    assert "password=[REDACTED_SECRET]" in r.text
    assert "api_key: [REDACTED_SECRET]" in r.text


def test_redacts_aws_access_key():
    r = redact("Leaked key AKIAIOSFODNN7EXAMPLE in the logs.")
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert "[REDACTED_AWS_KEY]" in r.text


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4"
    r = redact(f"Session token {jwt} appeared in trace.")
    assert jwt not in r.text
    assert "[REDACTED_JWT]" in r.text


def test_redacts_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890\nabcdEFGH\n"
        "-----END RSA PRIVATE KEY-----"
    )
    r = redact(f"Someone pasted:\n{pem}\ninto the channel.")
    assert "MIIEowIBAAKCAQEA" not in r.text
    assert "[REDACTED_PRIVATE_KEY]" in r.text


def test_redacts_multiple_categories_and_reports_counts():
    text = "Mailed a@b.com and c@d.org from 192.168.1.1; password=secretpw"
    r = redact(text)
    assert r.report.findings["email"] == 2
    assert r.report.findings["ipv4"] == 1
    assert r.report.findings["secret_assignment"] == 1
    assert r.report.total == 4
    assert r.report.redacted is True


# ─── does NOT over-redact legitimate incident content ────────────────────────


@pytest.mark.parametrize(
    "clean",
    [
        "The flagd flag productCatalogFailure was on.",
        "p95 latency crossed 5.2s (threshold 1.0).",
        "Rolled back to version 2 of the runbook.",
        "last_updated 2026-06-11 by the poc-team.",
        "helm rollback otel-demo to the prior revision.",
        "GetProduct spans showed STATUS_CODE_ERROR.",
    ],
)
def test_does_not_redact_legitimate_content(clean):
    r = redact(clean)
    assert r.text == clean
    assert r.report.redacted is False


def test_empty_text_is_noop():
    r = redact("")
    assert r.text == ""
    assert r.report.total == 0


def test_no_findings_summary():
    assert redact("all clean here").report.summary() == "no PII/secrets detected"


def test_summary_lists_categories():
    s = redact("a@b.com and c@d.com from 10.0.0.1").report.summary()
    assert "email=2" in s
    assert "ipv4=1" in s


# ─── idempotency ─────────────────────────────────────────────────────────────


def test_redaction_is_idempotent():
    once = scrub("oncall@example.com hit 10.0.0.1 with password=p")
    twice = scrub(once)
    assert once == twice
    # Second pass finds nothing — placeholders match no rule.
    assert redact(once).report.redacted is False
