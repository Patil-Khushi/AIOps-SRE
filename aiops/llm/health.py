"""Health probe for the active LLM provider (DEMO-2 / #54).

Exposes ``ping()`` — a cached, never-raises probe that issues one tiny
completion against the configured provider and reports whether it works.
``/api/health`` calls this so the dashboard chip stops lying when the SDK
is missing, the API key is bad, or the deployment name is wrong.

Cache strategy:
- 60 s TTL on success — minimizes per-second cost
- 10 s TTL on failure — recovers fast once the user fixes config
"""

from __future__ import annotations

import os
import time
from threading import Lock
from typing import Any

from .base import Message
from .gateway import complete

_SUCCESS_TTL_SECONDS = 60
_FAILURE_TTL_SECONDS = 10

# GPT-5 + o-series spend most of their token budget on internal reasoning
# before emitting a single output token. ``max_completion_tokens=16`` returns
# an empty string for these models (we saw it during DEMO-2 verification).
# Pad the budget so the ping actually probes the round-trip.
_REASONING_PING_TOKENS = 2048
_DEFAULT_PING_TOKENS = 16

_cache: dict[str, Any] = {}
_lock = Lock()


def _is_reasoning_model(model: str) -> bool:
    """Mirror of ``openai_provider._is_reasoning_model``. Kept local so the
    health module doesn't reach into a provider's privates."""
    m = (model or "").lower()
    if m.startswith(("o1", "o3", "o4")):
        return True
    return "gpt-5" in m


def ping(*, force: bool = False) -> dict[str, Any]:
    """Probe the configured LLM. Cached, never raises.

    Returns a dict::

        {
            "ok":          bool,
            "provider":    str | None,   # configured provider id (or actual on hit)
            "model":       str | None,   # configured model id (or actual on hit)
            "error":       str | None,   # human-readable error on failure
            "latency_ms":  int | None,   # round-trip ms on success
            "cached":      bool,         # was this a cache hit?
        }
    """
    configured_provider = os.environ.get("AIOPS_LLM_PROVIDER")
    configured_model = os.environ.get("AIOPS_LLM_MODEL")
    now = time.monotonic()
    with _lock:
        cached = _cache.get("payload")
        cached_until = _cache.get("expires_at", 0.0)
    if cached and not force and cached_until > now:
        return {**cached, "cached": True}

    payload = _probe(configured_provider, configured_model)
    ttl = _SUCCESS_TTL_SECONDS if payload["ok"] else _FAILURE_TTL_SECONDS
    with _lock:
        _cache["payload"] = payload
        _cache["expires_at"] = time.monotonic() + ttl
    return {**payload, "cached": False}


def _probe(provider: str | None, model: str | None) -> dict[str, Any]:
    max_tokens = (
        _REASONING_PING_TOKENS if model and _is_reasoning_model(model) else _DEFAULT_PING_TOKENS
    )
    t0 = time.perf_counter()
    try:
        resp = complete([Message("user", "ping")], max_tokens=max_tokens)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "provider": resp.provider,
            "model": resp.model,
            "error": None,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider,
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": None,
        }


def reset_cache_for_tests() -> None:
    """Clear the ping cache. Tests call this between cases so each one
    actually exercises ``_probe`` instead of replaying a stale result."""
    with _lock:
        _cache.clear()
