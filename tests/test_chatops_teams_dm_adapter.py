"""Smoke tests for the Microsoft Teams personal-DM adapter.

Mocks ``httpx.post`` so tests never hit the network. Asserts:

- Constructor validates the webhook URL (same host allow-list as the
  channel adapter); ``__repr__`` leaks nothing
- The filter matrix mirrors SlackBotAdapter: page/notify DM, log skips,
  and no HTTP call at all without a real-looking org email
- The flat payload is what the Power Automate flow parses with
  ``triggerBody()?['...']`` expressions — recipient, urgency framing,
  escaped html, truncation caps
- HTTP status failures re-raise sanitized (the URL embeds ``sig=``)
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aiops.tools.chatops import ChatMessage, Severity
from aiops.tools.chatops.adapters.teams_dm import TeamsDmAdapter
from aiops.tools.chatops.client import ChatOpsClient

WEBHOOK = (
    "https://prod-77.westus.logic.azure.com:443/workflows/f4k3dmfl0w"
    "/triggers/manual/paths/invoke?api-version=2016-06-01&sig=s3cr3ts1g"
)
POWER_PLATFORM_WEBHOOK = (
    "https://a1b2c3.04.environment.api.powerplatform.com/powerautomate"
    "/automations/direct/workflows/f4k3/triggers/manual/paths/invoke"
    "?api-version=1&sig=s3cr3ts1g"
)


def _msg(**overrides: Any) -> ChatMessage:
    defaults: dict[str, Any] = {
        "channel": "incidents",
        "severity": Severity.P1,
        "title": "Sev-1: PaymentErrorRateHigh",
        "body": "Service: payment\nSeverity: Sev-1\nOn-call: Chinmay",
        "incident_id": "INC0010099",
        "service": "payment",
        "mentions": ["@chinmay"],
        "actions": ["page_oncall", "post_to_chat"],
        "response_mode": "page",
        "assignee": "@chinmay",
        "assignee_name": "Chinmay Kotkar",
        "assignee_email": "chinmay.kotkar@zensar.com",
        "timestamp": datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return ChatMessage(**defaults)


def _mock_ok_response() -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    return r


def _sent_payload(msg: ChatMessage) -> dict[str, Any] | None:
    """Payload posted for ``msg``, or None when the adapter skipped."""
    adapter = TeamsDmAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.teams_dm.httpx.post") as post:
        post.return_value = _mock_ok_response()
        adapter.send(msg)
        if not post.called:
            return None
        return post.call_args.kwargs["json"]


# ─── construction ──────────────────────────────────────────────────────


def test_constructor_accepts_teams_webhook_urls() -> None:
    TeamsDmAdapter(WEBHOOK)
    TeamsDmAdapter(POWER_PLATFORM_WEBHOOK)


def test_constructor_rejects_non_teams_and_http_urls() -> None:
    with pytest.raises(ValueError):
        TeamsDmAdapter("https://hooks.slack.com/services/T000/B000/XXX")
    with pytest.raises(ValueError):
        TeamsDmAdapter("http://prod-77.westus.logic.azure.com/workflows/x")
    with pytest.raises(ValueError):
        TeamsDmAdapter("https://prod.logic.azure.com.evil.example/workflows/x")
    with pytest.raises(ValueError):
        TeamsDmAdapter("")


def test_repr_does_not_leak_webhook_url() -> None:
    text = repr(TeamsDmAdapter(WEBHOOK))
    assert "s3cr3ts1g" not in text
    assert "f4k3dmfl0w" not in text
    assert "prod-77" not in text


def test_adapter_name_is_stable() -> None:
    assert TeamsDmAdapter.name == "teams_dm"


# ─── filter matrix (mirrors SlackBotAdapter) ───────────────────────────


def test_page_action_sends_dm() -> None:
    payload = _sent_payload(_msg())
    assert payload is not None
    assert payload["urgency"] == "page"


def test_page_response_mode_without_action_sends_dm() -> None:
    # Back-compat parity with slack_bot: response_mode is honoured even
    # when the page_oncall action is absent.
    payload = _sent_payload(_msg(actions=["post_to_chat"], response_mode="page"))
    assert payload is not None
    assert payload["urgency"] == "page"


def test_notify_mode_sends_dm_to_assignee() -> None:
    payload = _sent_payload(_msg(actions=["post_to_chat"], response_mode="notify"))
    assert payload is not None
    assert payload["urgency"] == "notify"
    assert payload["recipient_email"] == "chinmay.kotkar@zensar.com"


def test_log_mode_skips_dm() -> None:
    # Sev-4: RA-005 nulls assignee fields, but even with them set the
    # log mode must not DM anyone.
    assert _sent_payload(_msg(actions=["post_to_chat"], response_mode="log")) is None


def test_no_assignee_email_skips_without_http_call() -> None:
    assert _sent_payload(_msg(assignee_email=None)) is None


def test_placeholder_email_skips_without_http_call() -> None:
    assert _sent_payload(_msg(assignee_email="chinmay@example.com")) is None


def test_bare_roster_key_skips_without_http_call() -> None:
    # _assignee_from() falls back to the engineer key when the on-call
    # lookup returned no row — "chinmay" is not a UPN.
    assert _sent_payload(_msg(assignee_email="chinmay")) is None


# ─── payload shape (the Power Automate flow contract) ──────────────────


def test_payload_fields_for_flow_expressions() -> None:
    payload = _sent_payload(_msg())
    assert payload is not None
    assert payload["recipient_email"] == "chinmay.kotkar@zensar.com"
    assert payload["severity"] == "p1"
    assert payload["title"] == "[P1] Sev-1: PaymentErrorRateHigh"
    assert payload["incident_id"] == "INC0010099"
    assert payload["service"] == "payment"
    assert payload["channel"] == "incidents"


def test_page_and_notify_framing_differ() -> None:
    paged = _sent_payload(_msg())
    notified = _sent_payload(_msg(actions=["post_to_chat"], response_mode="notify"))
    assert paged is not None and notified is not None
    assert "You're paged" in paged["text"]
    assert "acknowledge now" in paged["text"]
    assert "Assigned to you" in notified["text"]
    assert "review when free" in notified["text"]
    assert paged["text"] != notified["text"]


def test_card_matches_channel_layout_without_the_mention() -> None:
    """The DM renders the same Adaptive Card as the channel post — same
    severity-coloured headline, same routing FactSet — so an engineer reads
    one layout wherever the incident reaches them. It differs only by
    opening with urgency framing and carrying no @-mention or Notify line,
    because a DM is already addressed to exactly one person."""
    payload = _sent_payload(_msg())
    assert payload is not None
    card = payload["card"]
    assert card["type"] == "AdaptiveCard"

    blocks = card["body"]
    headline = blocks[0]
    assert headline["text"].startswith("[P1] ")
    assert headline["color"] == "attention"  # Sev-1 renders red
    assert headline["weight"] == "bolder"

    assert blocks[1]["text"].startswith("You're paged")  # urgency framing
    facts = {f["title"]: f["value"] for b in blocks if b["type"] == "FactSet" for f in b["facts"]}
    assert facts["Application"] == "payment"
    assert facts["Incident"] == "INC0010099"

    # No mention machinery in a DM.
    assert "entities" not in card["msteams"]
    assert not any("Notify:" in b.get("text", "") for b in blocks)


def test_notify_card_uses_softer_framing() -> None:
    payload = _sent_payload(_msg(actions=["post_to_chat"], response_mode="notify"))
    assert payload is not None
    lead = payload["card"]["body"][1]["text"]
    assert lead.startswith("Assigned to you")
    assert "review when free" in lead


def test_runbook_renders_as_an_open_button() -> None:
    from aiops.tools.chatops.runbook_attachment import RunbookAttachment

    rb = RunbookAttachment(
        runbook_id="rb-ad-failure",
        title="Ad service — 5xx errors",
        filename="rb-ad-failure.md",
        markdown="## Resolution steps",
        url="https://example-tenant.sharepoint.com/:t:/p/x/abc123",
    )
    payload = _sent_payload(_msg(runbook=rb))
    assert payload is not None
    actions = payload["card"]["actions"]
    assert len(actions) == 1
    # Action.OpenUrl is a plain hyperlink, not a submit — it needs no
    # callback, which is what makes it usable from a one-way webhook.
    assert actions[0]["type"] == "Action.OpenUrl"
    assert actions[0]["url"] == rb.url
    assert "rb-ad-failure.md" in actions[0]["title"]
    assert payload["runbook_filename"] == "rb-ad-failure.md"
    assert payload["runbook_url"] == rb.url


def test_runbook_without_published_link_renders_no_button() -> None:
    # A runbook exists in the library but was never published, so there is
    # nothing to open. Showing a dead button would be worse than none.
    from aiops.tools.chatops.runbook_attachment import RunbookAttachment

    rb = RunbookAttachment(
        runbook_id="rb-x", title="X", filename="rb-x.md", markdown="body", url=None
    )
    payload = _sent_payload(_msg(runbook=rb))
    assert payload is not None
    assert "actions" not in payload["card"]
    assert payload["runbook_url"] == ""


def test_no_runbook_sends_empty_strings_not_null() -> None:
    payload = _sent_payload(_msg(runbook=None))
    assert payload is not None
    assert payload["runbook_filename"] == ""
    assert payload["runbook_title"] == ""
    assert payload["runbook_url"] == ""
    assert "actions" not in payload["card"]


def test_optional_fields_are_never_null() -> None:
    # The Power Automate trigger validates the body against its declared
    # schema and rejects the entire request with HTTP 400
    # ("TriggerInputSchemaMismatch: Expected String but got Null") when an
    # optional field arrives null. Real alerts do that routinely — no ticket
    # cut yet (incident_id), or a CMDB miss (service).
    payload = _sent_payload(_msg(incident_id=None, service=None, channel=""))
    assert payload is not None
    assert payload["incident_id"] == ""
    assert payload["service"] == ""
    assert payload["channel"] == ""
    assert all(v is not None for v in payload.values())
    scalars = {k: v for k, v in payload.items() if k != "card"}
    assert all(isinstance(v, str) for v in scalars.values())


def test_title_and_text_truncate() -> None:
    payload = _sent_payload(_msg(title="T" * 500, body="B" * 10_000))
    assert payload is not None
    assert len(payload["title"]) == 150
    assert len(payload["text"]) <= 2900


# ─── transport ─────────────────────────────────────────────────────────


def test_send_posts_once_with_timeout() -> None:
    adapter = TeamsDmAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.teams_dm.httpx.post") as post:
        post.return_value = _mock_ok_response()
        adapter.send(_msg())
    post.assert_called_once()
    assert post.call_args.args == (WEBHOOK,)
    # Module constant, not a literal: _TIMEOUT is an import-time env read
    # (#151/#174 hermeticity class).
    from aiops.tools.chatops.adapters import teams_dm as teams_dm_mod

    assert post.call_args.kwargs["timeout"] == teams_dm_mod._TIMEOUT


def test_transport_error_propagates_for_client_to_log() -> None:
    adapter = TeamsDmAdapter(WEBHOOK)
    with patch(
        "aiops.tools.chatops.adapters.teams_dm.httpx.post",
        side_effect=httpx.ConnectError("dns flap"),
    ):
        with pytest.raises(httpx.ConnectError):
            adapter.send(_msg())


def _real_4xx_response(status_code: int = 400) -> httpx.Response:
    """Real Response bound to a real Request so raise_for_status() builds
    the true production message — which embeds the full sig=-bearing URL."""
    req = httpx.Request("POST", WEBHOOK)
    return httpx.Response(status_code, request=req)


def test_http_4xx_raises_sanitized_error(caplog: pytest.LogCaptureFixture) -> None:
    adapter = TeamsDmAdapter(WEBHOOK)
    with patch(
        "aiops.tools.chatops.adapters.teams_dm.httpx.post",
        return_value=_real_4xx_response(400),
    ):
        with caplog.at_level("ERROR"):
            with pytest.raises(httpx.HTTPError) as ei:
                adapter.send(_msg())

    assert "400" in str(ei.value)
    assert "s3cr3ts1g" not in str(ei.value)
    assert "logic.azure.com" not in str(ei.value)
    rendered = "".join(traceback.format_exception(ei.value))
    assert "s3cr3ts1g" not in rendered
    for record in caplog.records:
        assert "s3cr3ts1g" not in record.getMessage()


def test_delivery_result_error_contains_no_secret_via_chatops_client() -> None:
    client = ChatOpsClient()
    client.register(TeamsDmAdapter(WEBHOOK))
    with patch(
        "aiops.tools.chatops.adapters.teams_dm.httpx.post",
        return_value=_real_4xx_response(403),
    ):
        results = client.send(_msg())

    result = results["teams_dm"]
    assert result.ok is False
    assert result.error is not None
    assert "403" in result.error
    assert "s3cr3ts1g" not in result.error
    assert "logic.azure.com" not in result.error
