"""Provider-agnostic LLM types and base interface.

A provider implements ``LLMProvider`` for a specific backend (Anthropic, OpenAI,
Ollama, an internal gateway). The gateway dispatches based on ``AIOPS_LLM_PROVIDER``.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass
class LLMRequest:
    messages: list[Message]
    model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.2
    stop: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    # Optional hint for reasoning models (Azure gpt-5 / o-series):
    # "minimal" | "low" | "medium" | "high". Providers that don't support it
    # ignore it. Lower effort = fewer billed reasoning tokens, faster, cheaper.
    reasoning_effort: str | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    raw: dict | None = None


class LLMProvider(ABC):
    """Implement once per backend. Keep stateless."""

    name: str

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse:
        """Synchronous completion."""

    @abstractmethod
    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """Async completion."""


_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(name: str):
    def deco(cls: type[LLMProvider]) -> type[LLMProvider]:
        _REGISTRY[name] = cls
        cls.name = name
        return cls

    return deco


def get_provider(name: str | None = None) -> LLMProvider:
    """Return an instance of the requested provider.

    Falls back to ``AIOPS_LLM_PROVIDER`` env var, then to ``anthropic``.
    """
    chosen = (name or os.environ.get("AIOPS_LLM_PROVIDER") or "anthropic").lower()
    if chosen not in _REGISTRY:
        # Lazy-import provider modules so optional SDKs aren't required.
        if chosen == "anthropic":
            from . import anthropic_provider  # noqa: F401
        elif chosen == "openai":
            from . import openai_provider  # noqa: F401
        elif chosen == "ollama":
            from . import ollama_provider  # noqa: F401
        elif chosen == "stub":
            from . import stub_provider  # noqa: F401
        else:
            raise ValueError(f"Unknown LLM provider: {chosen!r}")
    if chosen not in _REGISTRY:
        raise RuntimeError(
            f"Provider {chosen!r} did not register. Check that the optional dependency "
            f"is installed (e.g. `uv sync --extra llm-{chosen}`)."
        )
    return _REGISTRY[chosen]()
