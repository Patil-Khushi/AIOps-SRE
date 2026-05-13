"""Tests for the JSON-file chatops adapter (D3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.tools.chatops import ChatMessage, ChatOpsClient, Severity
from aiops.tools.chatops.adapters.jsonfile import JsonFileChatOpsAdapter


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "chatops.jsonl"


def _msg(**over) -> ChatMessage:
    base = dict(
        channel="ops",
        severity=Severity.P2,
        title="db cpu spike",
        body="payment-svc primary db at 95% CPU for 5m",
        incident_id="INC-1234",
        service="payment-svc",
        mentions=["@oncall"],
        timestamp=datetime(2026, 5, 13, 11, 30, tzinfo=UTC),
    )
    base.update(over)
    return ChatMessage(**base)


def test_first_write_creates_parent_dirs(audit_path: Path):
    assert not audit_path.parent.exists()
    JsonFileChatOpsAdapter(audit_path).send(_msg())
    assert audit_path.exists()


def test_each_send_appends_one_line(audit_path: Path):
    adapter = JsonFileChatOpsAdapter(audit_path)
    adapter.send(_msg(title="first"))
    adapter.send(_msg(title="second"))
    adapter.send(_msg(title="third"))

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["title"] for ln in lines] == ["first", "second", "third"]


def test_record_is_json_friendly(audit_path: Path):
    JsonFileChatOpsAdapter(audit_path).send(_msg())

    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["severity"] == "p2"  # StrEnum flattened
    assert rec["timestamp"] == "2026-05-13T11:30:00+00:00"
    assert rec["channel"] == "ops"
    assert rec["incident_id"] == "INC-1234"
    assert rec["mentions"] == ["@oncall"]


def test_handles_unicode_bodies(audit_path: Path):
    JsonFileChatOpsAdapter(audit_path).send(_msg(body="café — naïve résumé ✓"))

    line = audit_path.read_text(encoding="utf-8").splitlines()[0]
    assert "café" in line
    assert json.loads(line)["body"] == "café — naïve résumé ✓"


def test_seam_fans_out_to_jsonfile_alongside_other_adapters(audit_path: Path):
    """The vendor-neutrality contract: same ChatMessage, multiple sinks."""

    class _Memory:
        def __init__(self) -> None:
            self.seen: list[ChatMessage] = []

        def send(self, m: ChatMessage) -> None:
            self.seen.append(m)

    client = ChatOpsClient()
    file_sink = JsonFileChatOpsAdapter(audit_path)
    memory_sink = _Memory()
    client.register(file_sink)
    client.register(memory_sink)

    m = _msg(title="cross-sink")
    client.send(m)

    # File saw it
    assert json.loads(audit_path.read_text("utf-8").splitlines()[0])["title"] == "cross-sink"
    # Memory adapter saw the same object
    assert memory_sink.seen == [m]
