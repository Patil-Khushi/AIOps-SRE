"""Top-level entry points for LLM calls.

Agent code calls ``complete()`` / ``acomplete()`` and never touches a vendor SDK directly.
"""

from __future__ import annotations

import os

from .base import LLMRequest, LLMResponse, Message, get_provider


def _enforce_caps(req: LLMRequest) -> LLMRequest:
    """Apply env-driven safety caps before any call leaves the process."""
    cap = int(os.environ.get("AIOPS_LLM_MAX_TOKENS_PER_CALL", "4096"))
    if req.max_tokens > cap:
        req.max_tokens = cap
    return req


def complete(
    messages: list[Message] | list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    provider: str | None = None,
    metadata: dict[str, str] | None = None,
) -> LLMResponse:
    """Synchronous LLM call. Use for scripts and Phase-0 smoke tests."""
    msgs = _coerce(messages)
    req = LLMRequest(
        messages=msgs,
        model=model or os.environ.get("AIOPS_LLM_MODEL"),
        max_tokens=max_tokens,
        temperature=temperature,
        metadata=metadata or {},
    )
    req = _enforce_caps(req)
    return get_provider(provider).complete(req)


async def acomplete(
    messages: list[Message] | list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    provider: str | None = None,
    metadata: dict[str, str] | None = None,
) -> LLMResponse:
    """Async LLM call. Use from agent code."""
    msgs = _coerce(messages)
    req = LLMRequest(
        messages=msgs,
        model=model or os.environ.get("AIOPS_LLM_MODEL"),
        max_tokens=max_tokens,
        temperature=temperature,
        metadata=metadata or {},
    )
    req = _enforce_caps(req)
    return await get_provider(provider).acomplete(req)


def _coerce(messages: list[Message] | list[dict]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if isinstance(m, Message):
            out.append(m)
        elif isinstance(m, dict):
            out.append(Message(role=m["role"], content=m["content"]))
        else:
            raise TypeError(f"Unsupported message type: {type(m).__name__}")
    return out
