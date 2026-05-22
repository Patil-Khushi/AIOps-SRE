"""Tests for the PagerDuty Events API v2 adapter (CHAT-5, issue #85).

Covers the Done-when checks from #85 plus the review-feedback items
from PR #96 (severity defense, regex key validation, registration
boundary, fire-and-forget thread behaviour).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from aiops.tools.chatops import ChatMessage, ChatOpsClient, Severity
from aiops.tools.chatops.adapters.pagerduty import (
    PAGE_ACTIONS,
    PAGE_WORTHY_SEVERITIES,
    PagerDutyAdapter,
)

_FAKE_KEY = "y" * 32  # 32 alphanumeric chars — passes the regex
_FIXED_TIME = datetime(2026, 5, 22, 14, 0, tzinfo=UTC)


def _msg(
    *,
    severity: Severity = Severity.P1,
    actions: list[str] | None = None,
    incident_id: str | None = None,
    service: str | None = "payment",
    title: str = "Payment service down",
) -> ChatMessage:
    return ChatMessage(
        channel="incidents",
        severity=severity,
        title=title,
        body="100% 5xx for the last 2 minutes",
        incident_id=incident_id,
        service=service,
        mentions=["@oncall@payments.example.com"],
        actions=actions if actions is not None else ["page_oncall", "post_to_chat"],
        timestamp=_FIXED_TIME,
    )


def _wait_for_threads(timeout: float = 1.0) -> None:
    """Wait for daemon HTTP-post threads spawned by the adapter to finish.

    The adapter fires HTTP off the calling thread; tests asserting on the
    mocked ``httpx.post`` need to wait until the daemon thread has actually
    invoked the mock. Joins any thread whose name starts with ``pagerduty-``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [t for t in threading.enumerate() if t.name.startswith("pagerduty-")]
        if not pending:
            return
        for t in pending:
            t.join(timeout=max(0.0, deadline - time.monotonic()))


# ─── construction / key validation ─────────────────────────────────────────


def test_empty_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("")


def test_placeholder_text_rejected():
    """Literal 'API_KEY' (a common copy-paste mistake) must fail at
    construction so misconfiguration doesn't silently degrade to no-op."""
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("API_KEY")


def test_too_short_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("y" * 16)


def test_too_long_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("y" * 33)


def test_non_alphanumeric_integration_key_rejected():
    with pytest.raises(ValueError, match="integration key"):
        PagerDutyAdapter("a" * 31 + "-")


def test_whitespace_is_trimmed_then_validated():
    """A correctly-shaped key with leading/trailing whitespace is accepted
    (auto-trimmed), so users can paste from the PD dashboard without
    grooming."""
    adapter = PagerDutyAdapter(f"   {_FAKE_KEY}\n")
    assert adapter._integration_key == _FAKE_KEY


def test_repr_does_not_leak_integration_key():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    representation = repr(adapter)
    assert _FAKE_KEY not in representation
    assert "***" in representation


# ─── send() filter behaviour ───────────────────────────────────────────────


def test_send_skips_when_actions_lacks_page_oncall():
    """Chat-only message (Sev-3 daytime) must not create a PD incident."""
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        adapter.send(_msg(severity=Severity.P3, actions=["post_to_chat"]))
        _wait_for_threads()
        mock_post.assert_not_called()


def test_send_skips_when_actions_empty():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        adapter.send(_msg(actions=[]))
        _wait_for_threads()
        mock_post.assert_not_called()


def test_send_fires_when_actions_contains_page_oncall():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg())
        _wait_for_threads()
        assert mock_post.call_count == 1


# ─── defence-in-depth severity check (PR #96 CR #3) ────────────────────────


@pytest.mark.parametrize("below_p2_severity", [Severity.P3, Severity.INFO])
def test_send_refuses_to_page_below_p2_even_with_page_oncall(below_p2_severity, caplog):
    """RA-005 should never attach page_oncall to a Sev-3/4 routing
    decision; if it does (bug), this adapter refuses + logs a warning
    rather than waking someone on a contradiction."""
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        adapter.send(_msg(severity=below_p2_severity, actions=["page_oncall"]))
        _wait_for_threads()
        mock_post.assert_not_called()
    assert any("refusing to page" in rec.message for rec in caplog.records)


def test_page_worthy_severities_is_strictly_p0_p1_p2():
    assert PAGE_WORTHY_SEVERITIES == {Severity.P0, Severity.P1, Severity.P2}


# ─── payload shape ─────────────────────────────────────────────────────────


def test_payload_includes_required_pd_fields():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg())
        _wait_for_threads()

        kwargs = mock_post.call_args.kwargs
        payload = kwargs["json"]

        assert payload["routing_key"] == _FAKE_KEY
        assert payload["event_action"] == "trigger"
        assert "dedup_key" in payload
        body = payload["payload"]
        assert body["summary"] == "Payment service down"
        assert body["source"] == "payment"
        assert body["severity"] == "critical"  # P1 → critical
        assert body["component"] == "payment"
        assert body["group"] == "incidents"
        assert body["custom_details"]["chat_severity"] == "p1"
        assert "page_oncall" in body["custom_details"]["actions"]


def test_severity_maps_correctly_to_pd_levels():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    # Only test severities that survive the page-worthy gate; the
    # below-P2 cases are covered by test_send_refuses_to_page_below_p2.
    cases = [
        (Severity.P0, "critical"),
        (Severity.P1, "critical"),
        (Severity.P2, "error"),
    ]
    for chat_sev, expected_pd in cases:
        with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            adapter.send(_msg(severity=chat_sev, actions=["page_oncall"]))
            _wait_for_threads()
            sent = mock_post.call_args.kwargs["json"]["payload"]["severity"]
            assert sent == expected_pd, f"{chat_sev} should map to {expected_pd}, got {sent}"


def test_falls_back_to_source_string_when_service_missing():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        adapter.send(_msg(service=None))
        _wait_for_threads()
        body = mock_post.call_args.kwargs["json"]["payload"]
        assert body["source"] == "adaptive-aiops/RA-005"
        assert body["component"] == "unknown"


# ─── dedup ─────────────────────────────────────────────────────────────────


def test_same_incident_id_produces_same_dedup_key():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    keys = []
    for _ in range(2):
        with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            adapter.send(_msg(incident_id="INC-1234"))
            _wait_for_threads()
            keys.append(mock_post.call_args.kwargs["json"]["dedup_key"])

    assert keys[0] == keys[1] == "aiops:incident:INC-1234"


def test_no_incident_id_falls_back_to_service_title_hash():
    adapter = PagerDutyAdapter(_FAKE_KEY)
    captured: list[str] = []
    for title in ("A", "A", "B"):
        with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            adapter.send(_msg(incident_id=None, service="payment", title=title))
            _wait_for_threads()
            captured.append(mock_post.call_args.kwargs["json"]["dedup_key"])

    key_a, key_a_again, key_b = captured
    assert key_a == key_a_again, "same service+title must dedup"
    assert key_a != key_b, "different titles must not collide"
    assert key_a.startswith("aiops:hash:")


# ─── error path (non-blocking — failure stays inside the thread) ───────────


def test_http_error_is_swallowed_at_caller_logged_inside_thread(caplog):
    """The adapter fires on a daemon thread; an HTTP failure must be logged
    inside the thread but never raise to the caller. This is the correct
    semantic for paging: a slow / unreachable PD must not back-pressure
    the chatops seam or block the API request that triggered the alert.
    """
    import httpx

    adapter = PagerDutyAdapter(_FAKE_KEY)
    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("dns is down")
        adapter.send(_msg())  # must not raise
        _wait_for_threads()

    assert any("enqueue failed" in rec.message for rec in caplog.records)


def test_send_returns_immediately_without_waiting_for_http():
    """``send()`` should be fire-and-forget. Even with a 10-second
    httpx.post, ``send`` should return in milliseconds."""
    adapter = PagerDutyAdapter(_FAKE_KEY)

    def slow_post(*_args, **_kwargs):
        time.sleep(2.0)
        m = MagicMock()
        m.raise_for_status.return_value = None
        return m

    with patch(
        "aiops.tools.chatops.adapters.pagerduty.httpx.post",
        side_effect=slow_post,
    ):
        t0 = time.monotonic()
        adapter.send(_msg())
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"send() should return immediately but blocked for {elapsed:.2f}s"
        _wait_for_threads(timeout=3.0)


# ─── seam integration ──────────────────────────────────────────────────────


def test_plays_nicely_alongside_other_adapters():
    """When registered alongside a non-page sink, both receive the message
    but only the PagerDuty adapter actually POSTs (other sinks are not
    bound by PAGE_ACTIONS — they log everything)."""

    class _Recorder:
        def __init__(self) -> None:
            self.received: list[ChatMessage] = []

        def send(self, m: ChatMessage) -> None:
            self.received.append(m)

    recorder = _Recorder()
    pd = PagerDutyAdapter(_FAKE_KEY)
    client = ChatOpsClient()
    client.register(recorder)
    client.register(pd)

    with patch("aiops.tools.chatops.adapters.pagerduty.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        # Sev-1 page-worthy message
        client.send(_msg())
        # Sev-3 chat-only
        client.send(_msg(severity=Severity.P3, actions=["post_to_chat"]))
        _wait_for_threads()

    assert len(recorder.received) == 2, "recorder sees every message"
    assert mock_post.call_count == 1, "PD only fires on the page-worthy one"


def test_page_actions_set_is_extensible():
    """v2 escalation actions (e.g. 'page_backup') should be addable
    without touching call-site code — they just go in PAGE_ACTIONS."""
    assert "page_oncall" in PAGE_ACTIONS
    assert isinstance(PAGE_ACTIONS, frozenset)  # immutable on purpose


# ─── registration boundary (PR #96 CR #5) ──────────────────────────────────
#
# The wire-up between ``AIOPS_PAGERDUTY_INTEGRATION_KEY`` and the chatops
# client lives in ``demo.ui.server._register_chatops_adapters``. A silent
# regression there (typo in env var name, swapped condition, wrong adapter
# class) would mean every deploy ships a non-paging build that looks fine
# in CI. These tests exercise the actual function with a swapped-in
# ChatOpsClient so the integration boundary is covered.


@pytest.fixture
def isolated_chatops_client(monkeypatch):
    """Replace the process-wide chatops singleton with a fresh client so
    each registration test starts from zero adapters and doesn't leak
    into the next test.

    Also imports ``demo.ui.server`` eagerly: that module calls
    ``load_dotenv`` at import time, which would otherwise re-populate
    ``AIOPS_PAGERDUTY_INTEGRATION_KEY`` from the dev's ``.env`` AFTER the
    test's ``monkeypatch.delenv`` ran, defeating the test. By forcing the
    import up here, dotenv runs first and per-test monkeypatching wins.
    """
    import demo.ui.server  # noqa: F401  — force load_dotenv before delenv
    from aiops.tools.chatops import client as _chatops_client_mod

    fresh = ChatOpsClient()
    monkeypatch.setattr(_chatops_client_mod, "_CLIENT", fresh)
    return fresh


def _call_register_chatops_adapters() -> None:
    from demo.ui.server import _register_chatops_adapters

    _register_chatops_adapters()


def test_register_skips_pagerduty_when_env_var_unset(monkeypatch, isolated_chatops_client):
    """No PD env var → the adapter must not be registered. JSON-file
    audit sink still registers (it's mandatory)."""
    monkeypatch.delenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", raising=False)
    _call_register_chatops_adapters()

    pd_adapters = [a for a in isolated_chatops_client.adapters if isinstance(a, PagerDutyAdapter)]
    assert pd_adapters == [], (
        "PagerDutyAdapter must not register when AIOPS_PAGERDUTY_INTEGRATION_KEY is unset"
    )


def test_register_skips_pagerduty_when_env_var_blank(monkeypatch, isolated_chatops_client):
    """Whitespace-only env var (common .env mistake — `KEY= `) must be
    treated the same as unset."""
    monkeypatch.setenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", "   ")
    _call_register_chatops_adapters()

    pd_adapters = [a for a in isolated_chatops_client.adapters if isinstance(a, PagerDutyAdapter)]
    assert pd_adapters == []


def test_register_attaches_pagerduty_when_env_var_valid(monkeypatch, isolated_chatops_client):
    """Well-formed 32-char key → exactly one PagerDutyAdapter on the
    client."""
    monkeypatch.setenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", _FAKE_KEY)
    _call_register_chatops_adapters()

    pd_adapters = [a for a in isolated_chatops_client.adapters if isinstance(a, PagerDutyAdapter)]
    assert len(pd_adapters) == 1, f"expected exactly one PagerDutyAdapter, got {len(pd_adapters)}"


def test_register_skips_pagerduty_when_env_var_invalid(
    monkeypatch, isolated_chatops_client, caplog
):
    """Misshapen key (placeholder text, truncated paste, etc.) must not
    register — the adapter's ValueError is caught and logged as a
    warning so server startup keeps going."""
    monkeypatch.setenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", "NOT_A_REAL_KEY")
    with caplog.at_level("WARNING"):
        _call_register_chatops_adapters()

    pd_adapters = [a for a in isolated_chatops_client.adapters if isinstance(a, PagerDutyAdapter)]
    assert pd_adapters == [], "invalid key must not register a half-broken adapter"
    assert any(
        "AIOPS_PAGERDUTY_INTEGRATION_KEY" in rec.message and "invalid" in rec.message
        for rec in caplog.records
    ), "invalid key must surface a warning so operators see the misconfiguration"


def test_register_is_idempotent_no_duplicate_adapters(monkeypatch, isolated_chatops_client):
    """Calling _register_chatops_adapters twice must not double-register any
    adapter kind. FastAPI today only fires startup hooks once, but a future
    hot-reload path or a test that exercises startup twice would silently
    duplicate every audit log line without this guard."""
    monkeypatch.setenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", _FAKE_KEY)
    monkeypatch.delenv("AIOPS_SLACK_WEBHOOK_URL", raising=False)

    _call_register_chatops_adapters()
    first_kinds = [type(a).__name__ for a in isolated_chatops_client.adapters]
    _call_register_chatops_adapters()
    second_kinds = [type(a).__name__ for a in isolated_chatops_client.adapters]

    assert first_kinds == second_kinds, (
        f"second call registered duplicates: {set(second_kinds) - set(first_kinds)}"
    )
    # Specifically: at most one PagerDutyAdapter regardless of call count.
    assert sum(1 for a in isolated_chatops_client.adapters if isinstance(a, PagerDutyAdapter)) == 1


# ─── transient retry (PR #96 follow-up review note #1) ─────────────────────


def test_post_retries_once_on_transient_http_error(caplog):
    """One transient HTTP failure followed by a success must result in a
    successfully-delivered page — no warning, one retry. Page-worthy
    alerts are expensive to drop on a network blip."""
    import httpx

    adapter = PagerDutyAdapter(_FAKE_KEY)

    ok_response = MagicMock()
    ok_response.raise_for_status.return_value = None
    side_effects = [httpx.ConnectError("blip"), ok_response]

    with patch(
        "aiops.tools.chatops.adapters.pagerduty.httpx.post",
        side_effect=side_effects,
    ) as mock_post:
        adapter.send(_msg())
        _wait_for_threads(timeout=3.0)

    assert mock_post.call_count == 2, "must retry once after a transient failure"
    assert not any("enqueue failed" in rec.message for rec in caplog.records), (
        "successful retry must not log the final-failure error"
    )


def test_post_gives_up_after_one_retry_and_logs(caplog):
    """Two consecutive failures exhaust the single retry budget and the
    final error is logged with the attempt count, so operators can tell a
    one-off blip apart from a sustained PD outage."""
    import httpx

    adapter = PagerDutyAdapter(_FAKE_KEY)

    with patch(
        "aiops.tools.chatops.adapters.pagerduty.httpx.post",
        side_effect=httpx.ConnectError("dns dead"),
    ) as mock_post:
        adapter.send(_msg())
        _wait_for_threads(timeout=3.0)

    assert mock_post.call_count == 2, "must attempt original + one retry, then give up"
    assert any(
        "enqueue failed" in rec.message and "2 attempt" in rec.message for rec in caplog.records
    ), "must log a final failure that names the attempt count"
