"""Tests for stage 6 of the Context Engineering Layer — security / redaction.

Three of these are load-bearing rather than illustrative, and are the reason this
file exists at all:

``test_raw_payload_is_left_byte_for_byte_alone``
    An agent adapter reproduces legacy prompt strings from ``ContextSection.raw``
    exactly. If redaction ever reaches into ``raw``, RCA's prompt and RA-007's
    log truncation change silently and no other test in the repo notices.

``test_shared_signature_survives_redaction``
    ``signature`` is an identity field. Scrubbing it must not split a group that
    was one before, or cross-source agreement quietly stops working.

``test_counts_do_not_inflate_when_redacting_twice``
    ``scrub``'s ``assigned_secret`` rule re-matches its own placeholder, so a
    naive wrapper reports phantom findings on already-clean text. A false alarm
    from a security control is how people learn to ignore it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiops.context.models import Observation, SectionStatus, make_observation_id
from aiops.context.pack import ContextSection, SourceProvenance
from aiops.context.redactor import redact, redact_text

WHEN = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

# A Luhn-valid test PAN, and two long digit runs that are not cards: epoch
# nanoseconds as Loki renders them, and OpenTelemetry's all-zero "invalid span id".
CARD = "4111111111111111"
EPOCH_NANOS = "1754812800123456789"
INVALID_SPAN_ID = "0000000000000000"


# --- builders ------------------------------------------------------------


def _observation(**overrides: Any) -> Observation:
    signature = overrides.pop("signature", "db timeout")
    base: dict[str, Any] = {
        "observation_id": make_observation_id("corr1", "logs", "error_log", signature),
        "correlation_id": "corr1",
        "source": "logs",
        "timestamp": WHEN,
        "service": "payment-service",
        "severity": "error",
        "category": "error_log",
        "signature": signature,
        "evidence": "connection to mysql timed out after 5s",
        "confidence": 0.8,
    }
    return Observation(**{**base, **overrides})


def _section(
    *,
    status: SectionStatus = SectionStatus.COLLECTED,
    observations: tuple[Observation, ...] = (),
    raw: dict[str, object] | None = None,
    error: str | None = None,
) -> ContextSection:
    return ContextSection(
        status=status,
        observations=observations,
        provenance=SourceProvenance(provider="loki", status=status, error=error),
        raw=raw,
    )


# --- the patterns added on top of _secrets.scrub -------------------------


def test_redacts_email_address():
    out, counts = redact_text("Paged oncall@example.com about the outage")
    assert "oncall@example.com" not in out
    assert "[REDACTED_EMAIL]" in out
    assert counts == {"email": 1}


def test_redacts_ipv4_address():
    out, counts = redact_text("pod ip 10.42.0.7 refused the connection")
    assert "10.42.0.7" not in out
    assert counts == {"ipv4": 1}


def test_redacts_card_number_bare_and_grouped():
    bare, bare_counts = redact_text(f"charged card {CARD} for order 9")
    grouped, grouped_counts = redact_text("charged card 4111-1111-1111-1111 for order 9")
    assert CARD not in bare
    assert "4111-1111-1111-1111" not in grouped
    assert bare_counts == grouped_counts == {"card_number": 1}


def test_leaves_epoch_and_all_zero_span_id_intact():
    """The card pattern must not eat the long digit runs telemetry is made of.

    Redacting a log timestamp or a parentless span id would be a visible
    over-redaction in exactly the sections this layer collects most of.
    """
    out, counts = redact_text(f"ts={EPOCH_NANOS} span_id={INVALID_SPAN_ID} level=error")
    assert out == f"ts={EPOCH_NANOS} span_id={INVALID_SPAN_ID} level=error"
    assert counts == {}


def test_leaves_clean_incident_text_unchanged():
    clean = "checkout p99 latency 5.2s, error rate 0.99, flag productCatalogFailure on"
    out, counts = redact_text(clean)
    assert out == clean
    assert counts == {}


# --- scrub's patterns still fire through the wrapper ---------------------


def test_scrub_credential_patterns_still_fire():
    """The credential patterns are ``_secrets.scrub``'s, not ours — prove the reuse."""
    samples = {
        "github_token": "token ghp_ABCDEFGHIJKLMNOPQRSTUV leaked",
        "aws_access_key": "AKIAIOSFODNN7EXAMPLE appeared in the logs",
        "slack_token": "xoxb-1234567890-abcdefghijkl posted the alert",
        "llm_api_key": "AIOPS_LLM used sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "url_credentials": "postgres://svc:s3cr3t@db.internal/orders unreachable",
        "assigned_secret": "DB_PASSWORD=hunter2 in the pod env",
    }
    for label, text in samples.items():
        out, counts = redact_text(text)
        assert label in counts, f"{label} did not fire: {counts}"
        assert "[REDACTED_SECRET]" in out


def test_credentials_are_claimed_before_the_email_pattern_can_mislabel_them():
    """Ordering regression: ``scrub`` must run before the PII patterns.

    ``s3cr3t@db.example.com`` matches the email pattern. If that ran first the
    password would be filed as an email, ``url_credentials`` would count zero, and
    the username would stay in the text — so an auditor asking "were credentials
    exposed?" gets told no.
    """
    out, counts = redact_text("postgres://svc:s3cr3t@db.example.com/orders unreachable")
    assert "s3cr3t" not in out
    assert "svc" not in out
    assert counts == {"url_credentials": 1}


def test_email_pattern_runs_before_the_ipv4_pattern():
    """``1.2.3.4@example.com`` is an address whose local part is IPv4-shaped.

    Redact the IP first and the local part becomes a placeholder the email pattern
    can no longer match, leaving the domain exposed.
    """
    out, counts = redact_text("1.2.3.4@example.com was paged")
    assert out == "[REDACTED_EMAIL] was paged"
    assert counts == {"email": 1}


# --- idempotency ---------------------------------------------------------


def test_counts_do_not_inflate_when_redacting_twice():
    text = "DB_PASSWORD=hunter2, contact oncall@example.com, pod 10.42.0.7"
    once, first_counts = redact_text(text)
    twice, second_counts = redact_text(once)
    assert twice == once, "already-redacted text was substituted a second time"
    assert second_counts == {}, "already-redacted text reported phantom findings"
    assert first_counts == {"assigned_secret": 1, "email": 1, "ipv4": 1}


def test_redacting_a_context_twice_reports_nothing_the_second_time():
    sections = {"logs": _section(observations=(_observation(evidence="mail a@b.com"),))}
    first, first_meta = redact(sections)
    second, second_meta = redact(first)
    assert first_meta.redaction_applied is True
    assert second_meta.redaction_applied is False
    assert second_meta.redaction_counts == {}
    assert second["logs"].observations == first["logs"].observations


# --- section-level behaviour --------------------------------------------


def test_raw_payload_is_left_byte_for_byte_alone():
    """``raw`` feeds byte-identical legacy prompt reproduction — see the module docstring."""
    raw: dict[str, object] = {
        "recent_errors": {
            "streams": [{"values": [["1", "login failed for user@example.com"]]}],
            "connection": "postgres://svc:s3cr3t@db.internal/orders",
        }
    }
    section = _section(
        observations=(_observation(evidence="login failed for user@example.com"),), raw=raw
    )
    out, meta = redact({"logs": section})

    assert out["logs"].raw is section.raw
    assert out["logs"].raw == raw
    # The observation *was* scrubbed, so this is not a case of redaction not running.
    assert "user@example.com" not in out["logs"].observations[0].evidence
    assert meta.redaction_counts["email"] == 1


def test_provenance_travels_untouched():
    section = _section(status=SectionStatus.FAILED, error="dial tcp 10.0.0.5:3100: refused")
    out, meta = redact({"logs": section})
    assert out["logs"] is section
    assert out["logs"].provenance.error == "dial tcp 10.0.0.5:3100: refused"
    assert meta.redaction_applied is False


def test_unusable_sections_are_still_scrubbed():
    """Redaction is status-blind on purpose.

    A FAILED section normally holds nothing, but gating the scrub on
    ``status.usable`` would mean "we could not trust this payload, so we did not
    clean it" — backwards for a security control.
    """
    section = _section(
        status=SectionStatus.FAILED,
        observations=(_observation(evidence="leaked AKIAIOSFODNN7EXAMPLE"),),
    )
    out, meta = redact({"logs": section})
    assert "AKIAIOSFODNN7EXAMPLE" not in out["logs"].observations[0].evidence
    assert meta.redaction_counts == {"aws_access_key": 1}


def test_section_keys_and_order_are_preserved():
    names = ["metrics", "logs", "traces", "oncall"]
    sections = {name: _section(status=SectionStatus.NOT_REQUESTED) for name in names}
    out, _ = redact(sections)
    assert list(out) == names


def test_clean_sections_are_returned_as_the_same_objects():
    sections = {"logs": _section(observations=(_observation(),))}
    out, meta = redact(sections)
    assert out["logs"] is sections["logs"]
    assert out["logs"].observations[0] is sections["logs"].observations[0]
    assert meta.redaction_applied is False
    assert meta.redaction_counts == {}


def test_input_sections_are_never_mutated():
    original = _observation(evidence="paged oncall@example.com", signature="paged a@b.com")
    sections = {"logs": _section(observations=(original,))}
    out, _ = redact(sections)

    assert original.evidence == "paged oncall@example.com"
    assert original.signature == "paged a@b.com"
    assert sections["logs"].observations[0] is original
    assert out["logs"] is not sections["logs"]


# --- identity ------------------------------------------------------------


def test_shared_signature_survives_redaction():
    """Two observations that shared a signature must still share one after scrubbing."""
    shared = "login failed for user@example.com"
    first = _observation(signature=shared, service="checkout")
    second = _observation(signature=shared, service="payment")
    assert first.signature == second.signature

    out, _ = redact({"logs": _section(observations=(first, second))})
    left, right = out["logs"].observations
    assert left.signature == right.signature
    assert "user@example.com" not in left.signature


def test_observation_id_is_not_recomputed():
    """Stage 4 already emitted rankings keyed on the pre-redaction id."""
    observation = _observation(signature="login failed for user@example.com")
    out, _ = redact({"logs": _section(observations=(observation,))})
    assert out["logs"].observations[0].observation_id == observation.observation_id


def test_fields_other_than_evidence_and_signature_are_carried_over():
    observation = _observation(
        evidence="paged oncall@example.com",
        metadata={"oncall_email": "oncall@example.com"},
    )
    out, _ = redact({"logs": _section(observations=(observation,))})
    scrubbed = out["logs"].observations[0]

    assert scrubbed.service == observation.service
    assert scrubbed.confidence == observation.confidence
    assert scrubbed.timestamp == observation.timestamp
    # metadata is the routing channel the notification assembler reads, so it is
    # deliberately not scrubbed — the page must still reach a human.
    assert scrubbed.metadata == {"oncall_email": "oncall@example.com"}


# --- metadata contract --------------------------------------------------


def test_counts_are_counts_and_never_the_matched_values():
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUV"
    email = "oncall@example.com"
    sections = {
        "logs": _section(observations=(_observation(evidence=f"{secret} used by {email}"),)),
    }
    _, meta = redact(sections)

    assert meta.redaction_counts == {"email": 1, "github_token": 1}
    for label, hits in meta.redaction_counts.items():
        assert isinstance(hits, int)
        assert secret not in label
        assert email not in label


def test_counts_are_summed_across_sections_and_sorted():
    sections = {
        "traces": _section(observations=(_observation(evidence="pod 10.0.0.9"),)),
        "logs": _section(
            observations=(
                _observation(evidence="mail a@b.com"),
                _observation(evidence="mail c@d.com and pod 10.0.0.8"),
            )
        ),
    }
    _, meta = redact(sections)
    assert meta.redaction_counts == {"email": 2, "ipv4": 2}
    assert list(meta.redaction_counts) == sorted(meta.redaction_counts)


def test_denied_capabilities_is_left_for_the_builder_to_fill():
    _, meta = redact({"logs": _section()})
    assert meta.denied_capabilities == ()


# --- purity / determinism ------------------------------------------------


def test_same_input_gives_byte_identical_output():
    sections = {
        "logs": _section(
            observations=(
                _observation(evidence=f"card {CARD} for oncall@example.com from 10.0.0.4"),
            )
        )
    }
    first_sections, first_meta = redact(sections)
    second_sections, second_meta = redact(sections)
    assert first_meta.model_dump_json() == second_meta.model_dump_json()
    assert first_sections["logs"].model_dump_json() == second_sections["logs"].model_dump_json()


def test_does_not_raise_on_hostile_text():
    """Never raise on the incident path — a malformed line costs evidence, not a verdict."""
    hostile = [
        "",
        "\x00",
        "[REDACTED_SECRET]",
        "PASSWORD=[REDACTED_SECRET]",
        "\U0001f525 " * 200,
        "-" * 5000,
        "9" * 5000,
        "a@b." + "c" * 500,
    ]
    for text in hostile:
        out, counts = redact_text(text)
        assert isinstance(out, str)
        assert all(isinstance(v, int) for v in counts.values())
