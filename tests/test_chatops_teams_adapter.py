"""Smoke tests for the Microsoft Teams webhook adapter.

Mocks ``httpx.post`` so tests never hit the network. Asserts:

- Construction validates the webhook URL (Power Automate logic.azure.com
  and legacy webhook.office.com hosts only; https only; no suffix spoofing)
- The payload is the Teams message envelope wrapping one Adaptive Card
- Severity → card color mapping collapses the five-level scale losslessly
  (the title carries the literal severity tag)
- Long titles / bodies truncate instead of failing
- Facts render only when populated
- Mentions and HITL prompts render as informational text (a one-way
  webhook has no interactivity callback)
- HTTP failures surface as exceptions so ``ChatOpsClient`` can log them
- ``__repr__`` does NOT leak the webhook URL (it embeds the auth signature)
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aiops.tools.chatops import ChatMessage, Severity
from aiops.tools.chatops.adapters.teams import TeamsWebhookAdapter
from aiops.tools.chatops.models import InteractivePrompt

WEBHOOK = (
    "https://prod-77.westus.logic.azure.com:443/workflows/f4k3w0rkfl0w"
    "/triggers/manual/paths/invoke?api-version=2016-06-01&sig=s3cr3ts1g"
)
POWER_PLATFORM_WEBHOOK = (
    "https://a1b2c3.04.environment.api.powerplatform.com/powerautomate"
    "/automations/direct/workflows/f4k3/triggers/manual/paths/invoke"
    "?api-version=1&sig=s3cr3ts1g"
)
LEGACY_WEBHOOK = "https://contoso.webhook.office.com/webhookb2/aaa/IncomingWebhook/bbb/ccc"


def _msg(**overrides: Any) -> ChatMessage:
    defaults: dict[str, Any] = {
        "channel": "incidents",
        "severity": Severity.P1,
        "title": "Sev-1: PaymentErrorRateHigh",
        "body": "Service: payment\nSeverity: Sev-1\nOn-call: chinmay",
        "incident_id": "INC0010099",
        "service": "payment",
        "mentions": ["@chinmay"],
        "timestamp": datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return ChatMessage(**defaults)


def _mock_ok_response() -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    return r


def _card(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the Adaptive Card from the Teams message envelope."""
    return payload["attachments"][0]["content"]


def _sent_payload(msg: ChatMessage) -> dict[str, Any]:
    adapter = TeamsWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.teams.httpx.post") as post:
        post.return_value = _mock_ok_response()
        adapter.send(msg)
        return post.call_args.kwargs["json"]


# ─── construction ──────────────────────────────────────────────────────


def test_constructor_accepts_power_automate_url() -> None:
    TeamsWebhookAdapter(WEBHOOK)


def test_constructor_accepts_power_platform_url() -> None:
    # Environments migrated off Logic Apps issue webhook URLs on
    # *.environment.api.powerplatform.com — the same Workflows template,
    # different host. Refusing these silently drops the Teams sink.
    TeamsWebhookAdapter(POWER_PLATFORM_WEBHOOK)


def test_constructor_accepts_legacy_connector_url() -> None:
    TeamsWebhookAdapter(LEGACY_WEBHOOK)


def test_constructor_rejects_non_teams_url() -> None:
    with pytest.raises(ValueError) as ei:
        TeamsWebhookAdapter("https://hooks.slack.com/services/T000/B000/XXX")
    assert "Teams webhook" in str(ei.value)


def test_constructor_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        TeamsWebhookAdapter("")


def test_constructor_rejects_http_scheme() -> None:
    with pytest.raises(ValueError):
        TeamsWebhookAdapter("http://prod-77.westus.logic.azure.com/workflows/x")


def test_constructor_rejects_suffix_spoofing() -> None:
    # "…logic.azure.com" as a *prefix* of a hostile registrable domain.
    with pytest.raises(ValueError):
        TeamsWebhookAdapter("https://prod.logic.azure.com.evil.example/workflows/x")


def test_repr_does_not_leak_webhook_url() -> None:
    text = repr(TeamsWebhookAdapter(WEBHOOK))
    assert "s3cr3ts1g" not in text
    assert "f4k3w0rkfl0w" not in text
    assert "prod-77" not in text


def test_adapter_name_is_stable() -> None:
    # ChatOpsClient keys DeliveryResults by this name; lock it down.
    assert TeamsWebhookAdapter.name == "teams"


# ─── payload shape ─────────────────────────────────────────────────────


def test_payload_is_message_envelope_with_adaptive_card() -> None:
    payload = _sent_payload(_msg())
    assert payload["type"] == "message"
    assert len(payload["attachments"]) == 1
    att = payload["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"], "card body must not be empty"


def test_title_carries_severity_tag_and_color() -> None:
    card = _card(_sent_payload(_msg(severity=Severity.P0)))
    header = card["body"][0]
    assert header["type"] == "TextBlock"
    assert header["text"].startswith("[P0] ")
    assert header["color"] == "attention"


@pytest.mark.parametrize(
    ("severity", "color"),
    [
        (Severity.P0, "attention"),
        (Severity.P1, "attention"),
        (Severity.P2, "warning"),
        (Severity.P3, "warning"),
        (Severity.INFO, "default"),
    ],
)
def test_severity_color_mapping(severity: Severity, color: str) -> None:
    card = _card(_sent_payload(_msg(severity=severity)))
    assert card["body"][0]["color"] == color


def test_facts_render_routing_context() -> None:
    card = _card(_sent_payload(_msg(category_display="Payment Gateway")))
    factsets = [b for b in card["body"] if b["type"] == "FactSet"]
    assert len(factsets) == 1
    facts = {f["title"]: f["value"] for f in factsets[0]["facts"]}
    assert facts == {
        "Application": "payment",
        "Sub-domain": "Payment Gateway",
        "Channel": "incidents",
        "Incident": "INC0010099",
    }


def test_unpopulated_facts_are_omitted() -> None:
    card = _card(
        _sent_payload(_msg(channel="", service=None, incident_id=None, category_display=None))
    )
    assert not any(b["type"] == "FactSet" for b in card["body"])


def test_long_title_and_body_truncate() -> None:
    card = _card(_sent_payload(_msg(title="T" * 500, body="B" * 10_000)))
    header = card["body"][0]
    assert len(header["text"]) == 150
    body_blocks = [
        b for b in card["body"][1:] if b["type"] == "TextBlock" and b["text"].startswith("B")
    ]
    assert len(body_blocks[0]["text"]) == 2900


def test_mentions_render_as_notify_line() -> None:
    card = _card(_sent_payload(_msg(mentions=["@chinmay", "@riya"])))
    notify = [b for b in card["body"] if b["type"] == "TextBlock" and "Notify:" in b["text"]]
    assert len(notify) == 1
    assert "@chinmay" in notify[0]["text"]
    assert "@riya" in notify[0]["text"]


def test_interactive_prompt_renders_as_text_not_buttons() -> None:
    prompt = InteractivePrompt(
        approval_id="apr-123",
        action="automation.runbook.execute",
        expires_at=datetime(2026, 8, 6, 11, 0, tzinfo=UTC),
    )
    card = _card(_sent_payload(_msg(interactive=prompt)))
    texts = [b["text"] for b in card["body"] if b["type"] == "TextBlock"]
    assert any("apr-123" in t for t in texts)
    # One-way webhook: no ActionSet / Action.Submit may sneak in.
    assert not any(b["type"] == "ActionSet" for b in card["body"])
    assert "actions" not in card


# ─── native assignee mention (per-person notification) ────────────────


def _assigned_msg(**overrides: Any) -> ChatMessage:
    defaults: dict[str, Any] = {
        "assignee": "@chinmay",
        "assignee_name": "Chinmay Kotkar",
        "assignee_email": "chinmay.kotkar@zensar.com",
        "mentions": ["@chinmay"],
        "response_mode": "page",
        "actions": ["page_oncall", "post_to_chat"],
    }
    defaults.update(overrides)
    return _msg(**defaults)


def test_assignee_mention_emits_entity_with_matching_text() -> None:
    card = _card(_sent_payload(_assigned_msg()))
    entities = card["msteams"]["entities"]
    assert len(entities) == 1
    entity = entities[0]
    assert entity["type"] == "mention"
    assert entity["text"] == "<at>Chinmay Kotkar</at>"
    assert entity["mentioned"] == {"id": "chinmay.kotkar@zensar.com", "name": "Chinmay Kotkar"}
    # The <at> text must appear verbatim in a TextBlock or Teams renders
    # the mention as literal text.
    notify = [b for b in card["body"] if b["type"] == "TextBlock" and "Notify:" in b["text"]]
    assert len(notify) == 1
    assert "<at>Chinmay Kotkar</at>" in notify[0]["text"]
    assert card["msteams"]["width"] == "Full"  # width survives the merge


def test_no_assignee_keeps_plain_notify_line_and_no_entities() -> None:
    card = _card(_sent_payload(_msg()))  # default _msg: mentions only
    assert "entities" not in card["msteams"]
    notify = [b for b in card["body"] if b["type"] == "TextBlock" and "Notify:" in b["text"]]
    assert notify[0]["text"] == "Notify: @chinmay"


def test_log_mode_no_mention_no_notify_line() -> None:
    # Sev-4: RA-005 nulls the assignee fields and empties mentions.
    card = _card(
        _sent_payload(
            _msg(
                response_mode="log",
                mentions=[],
                assignee=None,
                assignee_name=None,
                assignee_email=None,
            )
        )
    )
    assert "entities" not in card["msteams"]
    assert not any("Notify:" in b.get("text", "") for b in card["body"])


def test_placeholder_email_skips_native_mention() -> None:
    card = _card(_sent_payload(_assigned_msg(assignee_email="chinmay@example.com")))
    assert "entities" not in card["msteams"]
    notify = [b for b in card["body"] if "Notify:" in b.get("text", "")]
    assert notify[0]["text"] == "Notify: @chinmay"  # plain fallback


def test_bare_roster_key_assignee_email_skips_native_mention() -> None:
    # _assignee_from() falls back to verdict.assigned_engineer (a roster
    # key, not an email) when the on-call lookup returned no row.
    card = _card(_sent_payload(_assigned_msg(assignee_email="chinmay")))
    assert "entities" not in card["msteams"]


def test_mention_display_name_falls_back_to_email() -> None:
    card = _card(_sent_payload(_assigned_msg(assignee_name=None)))
    entity = card["msteams"]["entities"][0]
    assert entity["text"] == "<at>chinmay.kotkar@zensar.com</at>"
    assert entity["mentioned"]["name"] == "chinmay.kotkar@zensar.com"


def test_assignee_deduped_from_plain_notify_remainder() -> None:
    card = _card(_sent_payload(_assigned_msg(mentions=["@chinmay", "@riya"])))
    notify = next(b for b in card["body"] if "Notify:" in b.get("text", ""))["text"]
    assert notify == "Notify: <at>Chinmay Kotkar</at> @riya"
    assert "@chinmay " not in notify + " "  # handle not repeated as plain text


def test_mention_display_name_sanitized_and_still_matches_entity() -> None:
    card = _card(_sent_payload(_assigned_msg(assignee_name="Chinmay <b>K</b>")))
    entity = card["msteams"]["entities"][0]
    # The load-bearing invariant: entity text == the <at> substring in the
    # card body, and the display name carries no angle brackets that could
    # break the <at> delimiters.
    notify = next(b for b in card["body"] if "Notify:" in b.get("text", ""))["text"]
    assert entity["text"] in notify
    assert entity["text"].startswith("<at>") and entity["text"].endswith("</at>")
    inner = entity["text"][len("<at>") : -len("</at>")]
    assert "<" not in inner and ">" not in inner
    assert entity["mentioned"]["name"] == inner


# ─── runbook button (shared with the DM card) ─────────────────────────


def test_channel_card_renders_runbook_open_button() -> None:
    """The channel card links the published runbook, not the CMDB's
    placeholder URL. Same button the DM shows, pointing at the same file —
    runbooks are published once, so both sinks share one link."""
    from aiops.tools.chatops.runbook_attachment import RunbookAttachment

    rb = RunbookAttachment(
        runbook_id="rb-ad-failure",
        title="Ad service — 5xx errors",
        filename="rb-ad-failure.md",
        markdown="## Resolution steps",
        url="https://example-tenant.sharepoint.com/:t:/p/x/abc123",
    )
    card = _card(_sent_payload(_msg(runbook=rb)))
    actions = card["actions"]
    assert len(actions) == 1
    assert actions[0]["type"] == "Action.OpenUrl"
    assert actions[0]["url"] == rb.url
    assert "rb-ad-failure.md" in actions[0]["title"]


def test_channel_card_without_runbook_has_no_actions() -> None:
    assert "actions" not in _card(_sent_payload(_msg()))


def test_unpublished_runbook_renders_no_button() -> None:
    from aiops.tools.chatops.runbook_attachment import RunbookAttachment

    rb = RunbookAttachment(
        runbook_id="rb-x", title="X", filename="rb-x.md", markdown="body", url=None
    )
    assert "actions" not in _card(_sent_payload(_msg(runbook=rb)))


# ─── transport ─────────────────────────────────────────────────────────


def test_send_posts_once_with_timeout() -> None:
    adapter = TeamsWebhookAdapter(WEBHOOK)
    with patch("aiops.tools.chatops.adapters.teams.httpx.post") as post:
        post.return_value = _mock_ok_response()
        adapter.send(_msg())
    post.assert_called_once()
    assert post.call_args.args == (WEBHOOK,)
    # Compare against the module constant, not a literal 5.0: _TIMEOUT is an
    # import-time env read, and a developer's .env AIOPS_TEAMS_TIMEOUT leaks
    # into os.environ before this module imports in full-suite runs (the
    # load_dotenv-at-import class from #151/#174). The literal would be
    # green in isolation, red in the suite.
    from aiops.tools.chatops.adapters import teams as teams_mod

    assert post.call_args.kwargs["timeout"] == teams_mod._TIMEOUT


def test_transport_error_propagates_for_client_to_log() -> None:
    adapter = TeamsWebhookAdapter(WEBHOOK)
    with patch(
        "aiops.tools.chatops.adapters.teams.httpx.post",
        side_effect=httpx.ConnectError("dns flap"),
    ):
        with pytest.raises(httpx.ConnectError):
            adapter.send(_msg())


def _real_4xx_response(status_code: int = 400) -> httpx.Response:
    """A real Response bound to a real Request, so raise_for_status()
    produces the true production message — which embeds the full URL,
    sig= credential included. MagicMocks here would hide the leak."""
    req = httpx.Request("POST", WEBHOOK)
    return httpx.Response(status_code, request=req)


def test_http_4xx_raises_sanitized_error(caplog: pytest.LogCaptureFixture) -> None:
    adapter = TeamsWebhookAdapter(WEBHOOK)
    with patch(
        "aiops.tools.chatops.adapters.teams.httpx.post",
        return_value=_real_4xx_response(400),
    ):
        with caplog.at_level("ERROR"):
            with pytest.raises(httpx.HTTPError) as ei:
                adapter.send(_msg())

    # Still an httpx.HTTPError (the ChatOpsClient contract) with the status
    # code preserved for diagnosis...
    assert "400" in str(ei.value)
    # ...but the secret-bearing URL must not survive into the exception
    # (ChatOpsClient serializes str(exc) into DeliveryResult.error, which
    # /api/triage returns to the dashboard) nor into this adapter's log.
    assert "s3cr3ts1g" not in str(ei.value)
    assert "logic.azure.com" not in str(ei.value)
    # `from None` can't clear __context__, but it must suppress it so the
    # traceback ChatOpsClient's logger.exception renders omits the
    # URL-bearing original. Assert on the rendered form — the leak channel.
    rendered = "".join(traceback.format_exception(ei.value))
    assert "s3cr3ts1g" not in rendered
    for record in caplog.records:
        assert "s3cr3ts1g" not in record.getMessage()


def test_delivery_result_error_contains_no_secret_via_chatops_client() -> None:
    # End-to-end through ChatOpsClient.send: the DeliveryResult.error string
    # is what ReactiveFlowResult.to_api_dict() exposes on /api/triage.
    from aiops.tools.chatops.client import ChatOpsClient

    client = ChatOpsClient()
    client.register(TeamsWebhookAdapter(WEBHOOK))
    with patch(
        "aiops.tools.chatops.adapters.teams.httpx.post",
        return_value=_real_4xx_response(403),
    ):
        results = client.send(_msg())

    result = results["teams"]
    assert result.ok is False
    assert result.error is not None
    assert "403" in result.error
    assert "s3cr3ts1g" not in result.error
    assert "logic.azure.com" not in result.error
