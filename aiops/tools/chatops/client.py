"""ChatOps client — the sorting tray.

Fans every ChatMessage out to every registered adapter. Agents call
``get_client().send(msg)`` without knowing or caring which sinks are
plugged in. D2 plugs in the WebSocket adapter (live UI panel). D3 plugs
in the JSON audit-log adapter. Later tasks may plug in Slack, Teams,
PagerDuty — agent code never changes.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .models import ChatMessage

logger = logging.getLogger(__name__)


class ChatOpsAdapter(Protocol):
    """Anything that can deliver a ChatMessage. Implement ``send``."""

    def send(self, msg: ChatMessage) -> None: ...


class ChatOpsClient:
    """Holds adapters; fans each send() out to all of them.

    One failing adapter must not block the others — failures are logged
    and the loop continues.
    """

    def __init__(self) -> None:
        self._adapters: list[ChatOpsAdapter] = []

    def register(self, adapter: ChatOpsAdapter) -> None:
        self._adapters.append(adapter)

    def adapter_count(self) -> int:
        return len(self._adapters)

    @property
    def adapters(self) -> tuple[ChatOpsAdapter, ...]:
        """Read-only snapshot of registered adapters.

        Tests previously reached into ``_adapters`` directly, which coupled
        them to the list-storage implementation. Returning a tuple keeps
        the snapshot immutable so callers can't accidentally mutate the
        client's internal state.
        """
        return tuple(self._adapters)

    def send(self, msg: ChatMessage) -> None:
        if not self._adapters:
            logger.debug("chatops: no adapters registered; dropping %r", msg.title)
            return
        for a in self._adapters:
            try:
                a.send(msg)
            except Exception:
                logger.exception("chatops: adapter %r failed sending %r", a, msg.title)


_CLIENT = ChatOpsClient()


def get_client() -> ChatOpsClient:
    return _CLIENT
