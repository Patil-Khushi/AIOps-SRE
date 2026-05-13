"""Tests for the chatops seam (D1).

The seam itself has no sinks until D2/D3 plug them in. These tests use
fake adapters to verify the fan-out and isolation contracts.
"""

from __future__ import annotations

from aiops.tools.chatops import (
    ChatMessage,
    ChatOpsClient,
    Severity,
    get_client,
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

    client.send(msg)

    assert a.received == [msg]
    assert b.received == [msg]
    assert c.received == [msg]


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
