"""ChatOps client — the sorting tray.

Fans every ChatMessage out to every registered adapter. Agents call
``get_client().send(msg)`` without knowing or caring which sinks are
plugged in. D2 plugs in the WebSocket adapter (live UI panel). D3 plugs
in the JSON audit-log adapter. Later tasks may plug in Slack, Teams,
PagerDuty — agent code never changes.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Protocol

from .models import ChatMessage, DeliveryResult

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

    def send(self, msg: ChatMessage) -> dict[str, DeliveryResult]:
        results: dict[str, DeliveryResult] = {}
        if not self._adapters:
            logger.debug("chatops: no adapters registered; dropping %r", msg.title)
            return results

        name_counts: dict[str, int] = {}
        for adapter in self._adapters:
            base_name = getattr(adapter, "name", None) or adapter.__class__.__name__
            count = name_counts.get(base_name, 0) + 1
            name_counts[base_name] = count
            adapter_name = base_name if count == 1 else f"{base_name}#{count}"

            start = perf_counter()
            ok = True
            error: str | None = None
            try:
                adapter.send(msg)
            except Exception as exc:
                logger.exception(
                    "chatops: adapter %r failed sending %r",
                    adapter_name,
                    msg.title,
                )
                ok = False
                error = f"{type(exc).__name__}: {exc}"
            latency_ms = int((perf_counter() - start) * 1000)
            results[adapter_name] = DeliveryResult.model_construct(
                _fields_set=set(),
                adapter=adapter_name,
                ok=ok,
                error=error,
                latency_ms=latency_ms,
            )

        return results


_CLIENT = ChatOpsClient()


def get_client() -> ChatOpsClient:
    return _CLIENT
