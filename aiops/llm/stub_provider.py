"""Deterministic stub provider for tests and Phase-0 smoke checks.

Returns a constant response so unit tests never call a real LLM. Selected when
``AIOPS_LLM_PROVIDER=stub``.
"""

from __future__ import annotations

from .base import LLMProvider, LLMRequest, LLMResponse, register_provider


@register_provider("stub")
class StubProvider(LLMProvider):
    def complete(self, req: LLMRequest) -> LLMResponse:
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        return LLMResponse(
            text=f"[stub] echoing user message: {last_user[:200]}",
            model=req.model or "stub-1",
            provider="stub",
            input_tokens=sum(len(m.content) for m in req.messages),
            output_tokens=42,
            stop_reason="stop",
        )

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        return self.complete(req)
