"""Provider-agnostic LLM gateway.

Every LLM call from agent code goes through ``complete()`` (or ``acomplete()``).
The active provider is selected by ``AIOPS_LLM_PROVIDER`` (anthropic / openai / ollama).
Pin model versions explicitly via ``AIOPS_LLM_MODEL``; never use "latest".

Why this exists:
    Solution Design §2 — vendor-neutral by default. Putting
    ``anthropic.messages.create()`` in 47 places in the codebase is the
    documented top failure mode for AIOps POCs. We pay the indirection cost
    on day one.
"""

from .base import LLMProvider, LLMRequest, LLMResponse, Message, get_provider
from .gateway import acomplete, complete

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "acomplete",
    "complete",
    "get_provider",
]
