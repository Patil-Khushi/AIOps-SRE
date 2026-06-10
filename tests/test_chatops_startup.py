"""Tests for environment-driven chatops adapter registration."""

from __future__ import annotations

from aiops.tools.chatops import (
    JsonFileChatOpsAdapter,
    PagerDutyAdapter,
    SlackWebhookAdapter,
    register_env_adapters,
)
from aiops.tools.chatops.client import ChatOpsClient

_FAKE_PD_KEY = "y" * 32


def _fresh_chatops_client(monkeypatch):
    from aiops.tools.chatops import client as _client_mod

    fresh = ChatOpsClient()
    monkeypatch.setattr(_client_mod, "_CLIENT", fresh)
    # Isolate from any developer-local .env that may set the bot token:
    # the env reader would otherwise auto-register a 4th adapter.
    monkeypatch.delenv("AIOPS_SLACK_BOT_TOKEN", raising=False)
    return fresh


def test_register_env_adapters_registers_json_slack_and_pagerduty(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)

    audit_path = tmp_path / "chatops.jsonl"
    register_env_adapters(
        audit_path=audit_path,
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXXXXXX",
        pagerduty_integration_key=_FAKE_PD_KEY,
        slack_bot_token="",
    )

    assert any(isinstance(adapter, JsonFileChatOpsAdapter) for adapter in client.adapters)
    assert any(isinstance(adapter, SlackWebhookAdapter) for adapter in client.adapters)
    assert any(isinstance(adapter, PagerDutyAdapter) for adapter in client.adapters)
    assert len(client.adapters) == 3


def test_register_env_adapters_is_idempotent(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)

    audit_path = tmp_path / "chatops.jsonl"
    register_env_adapters(
        audit_path=audit_path,
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXXXXXX",
        pagerduty_integration_key=_FAKE_PD_KEY,
        slack_bot_token="",
    )
    register_env_adapters(
        audit_path=audit_path,
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXXXXXX",
        pagerduty_integration_key=_FAKE_PD_KEY,
        slack_bot_token="",
    )

    assert len(client.adapters) == 3


def test_register_env_adapters_skips_invalid_adapters(monkeypatch, tmp_path, caplog):
    client = _fresh_chatops_client(monkeypatch)

    audit_path = tmp_path / "chatops.jsonl"
    with caplog.at_level("WARNING"):
        register_env_adapters(
            audit_path=audit_path,
            slack_webhook_url="https://not-a-slack-url",
            pagerduty_integration_key="NOT_A_REAL_KEY",
        )

    assert any("AIOPS_SLACK_WEBHOOK_URL" in rec.message for rec in caplog.records)
    assert any("AIOPS_PAGERDUTY_INTEGRATION_KEY" in rec.message for rec in caplog.records)
    assert any(isinstance(adapter, JsonFileChatOpsAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, SlackWebhookAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, PagerDutyAdapter) for adapter in client.adapters)
