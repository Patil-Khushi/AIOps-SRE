"""OpenAI provider.

Activated by ``AIOPS_LLM_PROVIDER=openai``. Requires:
    uv sync --extra llm-openai
    export OPENAI_API_KEY=...
"""

from __future__ import annotations

import os

from .base import LLMProvider, LLMRequest, LLMResponse, register_provider


@register_provider("openai")
class OpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        try:
            from openai import AsyncOpenAI, OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "openai SDK not installed. Run `uv sync --extra llm-openai`."
            ) from exc
        self._sync = OpenAI()
        self._async = AsyncOpenAI()
        self._default_model = os.environ.get("AIOPS_LLM_MODEL") or os.environ.get(
            "OPENAI_MODEL", "gpt-4o"
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._sync.chat.completions.create(
            model=req.model or self._default_model,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            max_tokens=req.max_tokens,
            temperature=req.temperature,
        )
        choice = resp.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=resp.model,
            provider="openai",
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            stop_reason=choice.finish_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        resp = await self._async.chat.completions.create(
            model=req.model or self._default_model,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            max_tokens=req.max_tokens,
            temperature=req.temperature,
        )
        choice = resp.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=resp.model,
            provider="openai",
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            stop_reason=choice.finish_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )
