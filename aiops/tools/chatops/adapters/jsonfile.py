"""File-based JSON audit-log adapter for the chatops seam (D3).

Why this exists:
    Solution Design §2 (vendor-neutral by default) requires every integration
    point to have at least two implementations. The WebSocket adapter (D2)
    proves the seam can stream live; this adapter proves the same
    ``ChatMessage`` can also land in a flat file with zero code coupling to
    WebSocket-specific shapes. If a future adapter (Slack, Teams) needs the
    same input and produces a different output, the seam is genuinely
    abstract — not accidentally tied to one transport.

Format:
    One JSON object per line (JSONL). Easy to ``grep``, ``jq``, tail, ingest.

Failure handling:
    Adapter raises only on programmer errors (e.g. unwritable directory at
    construction). Per-message write failures log and re-raise; the
    ``ChatOpsClient`` catches them so one broken sink can never block the
    others.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..models import ChatMessage, to_record

logger = logging.getLogger(__name__)


class JsonFileChatOpsAdapter:
    """Append every ``ChatMessage`` as one JSON line to a target file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, msg: ChatMessage) -> None:
        line = json.dumps(to_record(msg), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
