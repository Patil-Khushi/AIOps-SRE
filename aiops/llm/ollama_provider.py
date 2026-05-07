"""Ollama provider — local fallback when data cannot leave the cluster.

Activated by ``AIOPS_LLM_PROVIDER=ollama``. Requires:
    uv sync --extra llm-ollama
    OLLAMA_HOST=http://localhost:11434
"""

from __future__ import annotations

import os

from .base import LLMProvider, LLMRequest, LLMResponse, register_provider


@register_provider("ollama")
class OllamaProvider(LLMProvider):
    def __init__(self) -> None:
        try:
            import ollama  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "ollama SDK not installed. Run `uv sync --extra llm-ollama`."
            ) from exc
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self._sync = ollama.Client(host=host)
        self._async = ollama.AsyncClient(host=host)
        self._default_model = os.environ.get("AIOPS_LLM_MODEL") or os.environ.get(
            "OLLAMA_MODEL", "llama3.1"
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._sync.chat(
            model=req.model or self._default_model,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            options={"temperature": req.temperature, "num_predict": req.max_tokens},
        )
        return LLMResponse(
            text=resp["message"]["content"],
            model=resp.get("model", self._default_model),
            provider="ollama",
            input_tokens=resp.get("prompt_eval_count", 0),
            output_tokens=resp.get("eval_count", 0),
            stop_reason=resp.get("done_reason", ""),
            raw=dict(resp),
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        resp = await self._async.chat(
            model=req.model or self._default_model,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            options={"temperature": req.temperature, "num_predict": req.max_tokens},
        )
        return LLMResponse(
            text=resp["message"]["content"],
            model=resp.get("model", self._default_model),
            provider="ollama",
            input_tokens=resp.get("prompt_eval_count", 0),
            output_tokens=resp.get("eval_count", 0),
            stop_reason=resp.get("done_reason", ""),
            raw=dict(resp),
        )
