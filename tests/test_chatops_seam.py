"""Tests for the chatops seam (D1).

The seam itself has no sinks until D2/D3 plug them in. These tests use
fake adapters to verify the fan-out and isolation contracts.
"""

from __future__ import annotations

from aiops.tools.chatops import (
    ChatMessage,
    ChatOpsClient,
    DeliveryResult,
    Severity,
    get_client,
    to_record,
)


class _FakeAdapter:
    def __init__(self) -> None:
        self.received: list[ChatMessage] = []

    def send(self, msg: ChatMessage) -> None:
        self.received.append(msg)


def _make_msg(title: str = "test") -> ChatMessage:
    return ChatMessage(
        channel="ops",
        severity=Severity.P2,
        title=title,
        body="hello",
    )


def test_send_with_no_adapters_is_a_noop():
    client = ChatOpsClient()
    client.send(_make_msg())  # must not raise


def test_send_fans_out_to_every_adapter():
    client = ChatOpsClient()
    a, b, c = _FakeAdapter(), _FakeAdapter(), _FakeAdapter()
    client.register(a)
    client.register(b)
    client.register(c)
    msg = _make_msg("hello")

    results = client.send(msg)

    assert a.received == [msg]
    assert b.received == [msg]
    assert c.received == [msg]
    assert set(results) == {"_FakeAdapter", "_FakeAdapter#2", "_FakeAdapter#3"}
    assert all(result.ok for result in results.values())


def test_send_returns_delivery_results_for_each_adapter():
    client = ChatOpsClient()
    named = _FakeAdapter()
    client.register(named)
    result = client.send(_make_msg("hello"))

    assert list(result) == ["_FakeAdapter"]
    delivery = result["_FakeAdapter"]
    assert isinstance(delivery, DeliveryResult)
    assert delivery.ok is True
    assert delivery.error is None
    assert isinstance(delivery.latency_ms, int)


def test_send_uses_adapter_name_when_provided():
    class _NamedAdapter:
        name = "custom-adapter"

        def __init__(self) -> None:
            self.received: list[ChatMessage] = []

        def send(self, msg: ChatMessage) -> None:
            self.received.append(msg)

    client = ChatOpsClient()
    adapter = _NamedAdapter()
    client.register(adapter)

    result = client.send(_make_msg("hello"))

    assert list(result) == ["custom-adapter"]
    assert result["custom-adapter"].ok is True


def test_failing_adapter_does_not_block_others():
    client = ChatOpsClient()

    class _Broken:
        def send(self, msg: ChatMessage) -> None:
            raise RuntimeError("boom")

    good = _FakeAdapter()
    client.register(_Broken())
    client.register(good)

    client.send(_make_msg())

    assert len(good.received) == 1


def test_get_client_returns_process_wide_singleton():
    assert get_client() is get_client()


def test_chatmessage_defaults_are_sensible():
    msg = ChatMessage(channel="x", severity=Severity.INFO, title="t")
    assert msg.body == ""
    assert msg.mentions == []
    assert msg.timestamp is not None
    assert msg.incident_id is None
    assert msg.service is None


def test_severity_serializes_as_string():
    # str-Enum lets dataclass.asdict produce JSON-friendly values without a
    # custom encoder — D3 (JSON adapter) relies on this.
    assert Severity.P2.value == "p2"
    assert Severity("p2") is Severity.P2


def test_to_record_includes_actions_field():
    """``actions`` was added to ChatMessage in CHAT-5 (#85) so adapters can
    filter on routing intent (e.g. PagerDuty only fires on ``page_oncall``).
    Locking the serialized contract here keeps the audit log, the WebSocket
    feed, and the dashboard JSON from silently dropping the field on a
    future model change."""
    msg = ChatMessage(
        channel="incidents",
        severity=Severity.P1,
        title="payment down",
        actions=["page_oncall", "post_to_chat"],
        mentions=["@chinmay"],
    )
    record = to_record(msg)

    assert record["actions"] == ["page_oncall", "post_to_chat"]
    assert record["mentions"] == ["@chinmay"]
    # `to_record` should hand back a fresh list (defensive copy), not the
    # adapter-observable underlying list — mutating one must not bleed.
    record["actions"].append("mutate")
    assert msg.actions == ["page_oncall", "post_to_chat"]


def test_to_record_full_key_contract():
    """The exact set of keys emitted by to_record is the wire contract for
    every chatops sink. Pin it so additions are reviewed deliberately."""
    msg = ChatMessage(channel="ops", severity=Severity.INFO, title="x")
    assert set(to_record(msg).keys()) == {
        "timestamp",
        "channel",
        "severity",
        "title",
        "body",
        "incident_id",
        "service",
        "mentions",
        "actions",
        "interactive",
    }
