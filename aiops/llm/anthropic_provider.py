"""Anthropic provider — supports both anthropic.com API and Azure AI Foundry.

Activated by ``AIOPS_LLM_PROVIDER=anthropic`` (default). Requires::

    uv sync --extra llm-anthropic

Endpoint selection by env var:

- If ``ANTHROPIC_BASE_URL`` is set AND contains ``.azure.com``, the provider
  uses ``anthropic.AnthropicFoundry`` (Azure AI Foundry deployment of Claude).
  The ``AIOPS_LLM_MODEL`` value must match the Azure **deployment name**
  (e.g., ``claude-sonnet-4-6``), not the upstream model id.

- Otherwise the standard ``anthropic.Anthropic`` client is used against
  ``api.anthropic.com``. ``ANTHROPIC_BASE_URL`` may still be set to point at
  any other Anthropic-compatible endpoint (e.g., a self-hosted proxy).

In both cases ``ANTHROPIC_API_KEY`` carries the credential.
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

        base_url = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip() or None
        api_key = os.environ.get("ANTHROPIC_API_KEY")

        if base_url and ".azure.com" in base_url:
            # Azure AI Foundry — use the Foundry client class.
            self._sync = anthropic.AnthropicFoundry(api_key=api_key, base_url=base_url)
            self._async = anthropic.AsyncAnthropicFoundry(api_key=api_key, base_url=base_url)
            self._flavor = "anthropic-foundry"
        else:
            kwargs: dict = {}
            if api_key:
                kwargs["api_key"] = api_key
            if base_url:
                kwargs["base_url"] = base_url
            self._sync = anthropic.Anthropic(**kwargs)
            self._async = anthropic.AsyncAnthropic(**kwargs)
            self._flavor = "anthropic"

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
            provider=self._flavor,
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
            provider=self._flavor,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )
