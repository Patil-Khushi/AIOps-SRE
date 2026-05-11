"""OpenAI provider — supports both openai.com and Azure OpenAI deployments.

Activated by ``AIOPS_LLM_PROVIDER=openai``. Requires::

    uv sync --extra llm-openai

Endpoint selection by env var:

- If ``AZURE_OPENAI_ENDPOINT`` is set (or ``OPENAI_BASE_URL`` contains
  ``.azure.com``), the provider uses ``openai.AzureOpenAI`` / ``AsyncAzureOpenAI``.
  ``AIOPS_LLM_MODEL`` must match the Azure **deployment name**
  (e.g., ``gpt-5``), not the upstream model id.

- Otherwise the standard ``openai.OpenAI`` client is used against
  ``api.openai.com``.

Credential precedence: ``AZURE_OPENAI_API_KEY`` > ``OPENAI_API_KEY``.

GPT-5 / o-series quirks handled here:
- Use ``max_completion_tokens`` instead of ``max_tokens``.
- Don't pass ``temperature`` (these models only accept the default of 1).
"""

from __future__ import annotations

import os
from typing import Any

from .base import LLMProvider, LLMRequest, LLMResponse, register_provider

_DEFAULT_AZURE_API_VERSION = "2024-12-01-preview"


def _is_reasoning_model(model: str) -> bool:
    """GPT-5 + o1/o3/o4 don't accept ``max_tokens`` or custom ``temperature``."""
    m = (model or "").lower()
    if m.startswith(("o1", "o3", "o4")):
        return True
    return "gpt-5" in m


@register_provider("openai")
class OpenAIProvider(LLMProvider):
    def __init__(self) -> None:
        try:
            from openai import AsyncOpenAI, OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "openai SDK not installed. Run `uv sync --extra llm-openai`."
            ) from exc

        # Pick credentials + endpoint with Azure-first precedence.
        azure_endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip() or None
        openai_base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip() or None
        endpoint = azure_endpoint or openai_base_url
        api_key = (
            os.environ.get("AZURE_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

        use_azure = bool(endpoint and ".azure.com" in endpoint)

        if use_azure:
            try:
                from openai import AsyncAzureOpenAI, AzureOpenAI  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "openai>=1.x with Azure support required. Reinstall: "
                    "`uv sync --extra llm-openai`."
                ) from exc
            api_version = (
                os.environ.get("AZURE_OPENAI_API_VERSION") or _DEFAULT_AZURE_API_VERSION
            )
            self._sync = AzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
            )
            self._async = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
            )
            self._flavor = "openai-azure"
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if endpoint:
                kwargs["base_url"] = endpoint
            self._sync = OpenAI(**kwargs)
            self._async = AsyncOpenAI(**kwargs)
            self._flavor = "openai"

        # Model resolution: AIOPS_LLM_MODEL > AZURE_OPENAI_DEPLOYMENT_NAME > OPENAI_MODEL > 'gpt-4o'.
        self._default_model = (
            os.environ.get("AIOPS_LLM_MODEL")
            or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
            or os.environ.get("OPENAI_MODEL")
            or "gpt-4o"
        )

    def _build_kwargs(self, req: LLMRequest) -> dict[str, Any]:
        model = req.model or self._default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if _is_reasoning_model(model):
            # GPT-5 / o-series: only max_completion_tokens; no temperature.
            kwargs["max_completion_tokens"] = req.max_tokens
        else:
            kwargs["max_tokens"] = req.max_tokens
            kwargs["temperature"] = req.temperature
        return kwargs

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._sync.chat.completions.create(**self._build_kwargs(req))
        choice = resp.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=resp.model,
            provider=self._flavor,
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            stop_reason=choice.finish_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        resp = await self._async.chat.completions.create(**self._build_kwargs(req))
        choice = resp.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=resp.model,
            provider=self._flavor,
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
            stop_reason=choice.finish_reason or "",
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )
