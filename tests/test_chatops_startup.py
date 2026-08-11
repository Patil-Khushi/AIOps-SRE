"""Tests for environment-driven chatops adapter registration."""

from __future__ import annotations

from aiops.tools.chatops import (
    JsonFileChatOpsAdapter,
    PagerDutyAdapter,
    SlackWebhookAdapter,
    TeamsDmAdapter,
    TeamsWebhookAdapter,
    register_env_adapters,
)
from aiops.tools.chatops.client import ChatOpsClient

_FAKE_PD_KEY = "y" * 32
_FAKE_TEAMS_URL = (
    "https://prod-77.westus.logic.azure.com:443/workflows/f4k3"
    "/triggers/manual/paths/invoke?api-version=2016-06-01&sig=xxx"
)


def _fresh_chatops_client(monkeypatch):
    from aiops.tools.chatops import client as _client_mod

    fresh = ChatOpsClient()
    monkeypatch.setattr(_client_mod, "_CLIENT", fresh)
    # Isolate from any developer-local .env that may set the bot token or
    # Teams webhooks: the env reader would otherwise auto-register extra
    # adapters and break the adapter-count assertions below.
    monkeypatch.delenv("AIOPS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AIOPS_TEAMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AIOPS_TEAMS_DM_WEBHOOK_URL", raising=False)
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
            teams_webhook_url="https://not-a-teams-url.example.com/hook",
            teams_dm_webhook_url="https://also-not-teams.example.com/hook",
        )

    assert any("AIOPS_SLACK_WEBHOOK_URL" in rec.message for rec in caplog.records)
    assert any("AIOPS_PAGERDUTY_INTEGRATION_KEY" in rec.message for rec in caplog.records)
    assert any("AIOPS_TEAMS_WEBHOOK_URL" in rec.message for rec in caplog.records)
    assert any("AIOPS_TEAMS_DM_WEBHOOK_URL" in rec.message for rec in caplog.records)
    assert any(isinstance(adapter, JsonFileChatOpsAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, SlackWebhookAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, PagerDutyAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, TeamsWebhookAdapter) for adapter in client.adapters)
    assert not any(isinstance(adapter, TeamsDmAdapter) for adapter in client.adapters)


def test_register_env_adapters_registers_teams_from_env(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)
    monkeypatch.setenv("AIOPS_TEAMS_WEBHOOK_URL", _FAKE_TEAMS_URL)

    register_env_adapters(
        audit_path=tmp_path / "chatops.jsonl",
        slack_webhook_url="",
        pagerduty_integration_key="",
        slack_bot_token="",
    )

    assert any(isinstance(adapter, TeamsWebhookAdapter) for adapter in client.adapters)
    assert len(client.adapters) == 2  # jsonfile + teams


def test_register_env_adapters_teams_and_slack_coexist(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)

    register_env_adapters(
        audit_path=tmp_path / "chatops.jsonl",
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXXXXXX",
        pagerduty_integration_key="",
        slack_bot_token="",
        teams_webhook_url=_FAKE_TEAMS_URL,
    )

    assert any(isinstance(adapter, SlackWebhookAdapter) for adapter in client.adapters)
    assert any(isinstance(adapter, TeamsWebhookAdapter) for adapter in client.adapters)
    assert len(client.adapters) == 3  # jsonfile + slack + teams


def test_register_env_adapters_registers_teams_dm_from_env(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)
    monkeypatch.setenv("AIOPS_TEAMS_DM_WEBHOOK_URL", _FAKE_TEAMS_URL)

    register_env_adapters(
        audit_path=tmp_path / "chatops.jsonl",
        slack_webhook_url="",
        pagerduty_integration_key="",
        slack_bot_token="",
        teams_webhook_url="",
    )

    assert any(isinstance(adapter, TeamsDmAdapter) for adapter in client.adapters)
    assert len(client.adapters) == 2  # jsonfile + teams_dm


def test_register_env_adapters_teams_channel_and_dm_coexist(monkeypatch, tmp_path):
    client = _fresh_chatops_client(monkeypatch)

    register_env_adapters(
        audit_path=tmp_path / "chatops.jsonl",
        slack_webhook_url="",
        pagerduty_integration_key="",
        slack_bot_token="",
        teams_webhook_url=_FAKE_TEAMS_URL,
        teams_dm_webhook_url=_FAKE_TEAMS_URL,
    )

    assert any(isinstance(adapter, TeamsWebhookAdapter) for adapter in client.adapters)
    assert any(isinstance(adapter, TeamsDmAdapter) for adapter in client.adapters)
    assert len(client.adapters) == 3  # jsonfile + teams + teams_dm
