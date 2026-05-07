"""Anthropic provider.

Activated by ``AIOPS_LLM_PROVIDER=anthropic`` (default). Requires:
    uv sync --extra llm-anthropic
    export ANTHROPIC_API_KEY=...
"""

from __future__ import annotations

import os

from .base import LLMProvider, LLMRequest, LLMResponse, register_provider


@register_provider("anthropic")
class AnthropicProvider(LLMProvider):
    def __init__(self) -> None:
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK not installed. Run `uv sync --extra llm-anthropic`."
            ) from exc
        self._sync = anthropic.Anthropic()
        self._async = anthropic.AsyncAnthropic()
        self._default_model = os.environ.get("AIOPS_LLM_MODEL", "claude-sonnet-4-6")

    def _split(self, req: LLMRequest) -> tuple[str | None, list[dict]]:
        system = next((m.content for m in req.messages if m.role == "system"), None)
        user_assistant = [
            {"role": m.role, "content": m.content}
            for m in req.messages
            if m.role in ("user", "assistant")
        ]
        return system, user_assistant

    def complete(self, req: LLMRequest) -> LLMResponse:
        system, msgs = self._split(req)
        kwargs: dict = {
            "model": req.model or self._default_model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": msgs,
        }
        if system:
            kwargs["system"] = system
        resp = self._sync.messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        return LLMResponse(
            text=text,
            model=resp.model,
            provider="anthropic",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        system, msgs = self._split(req)
        kwargs: dict = {
            "model": req.model or self._default_model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": msgs,
        }
        if system:
            kwargs["system"] = system
        resp = await self._async.messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        return LLMResponse(
            text=text,
            model=resp.model,
            provider="anthropic",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )
