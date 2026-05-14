"""DEMO-2 / #54 — ``aiops.llm.ping()`` is the health-probe primitive that
``/api/health`` calls. It must never raise, must distinguish missing-SDK
from API errors, and must cache so the dashboard chip doesn't flood the API.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import aiops.llm.health as health_mod
from aiops.llm import ping


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    """Default every test to the stub LLM unless it overrides."""
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.delenv("AIOPS_LLM_MODEL", raising=False)
    health_mod.reset_cache_for_tests()
    yield


def test_ping_returns_ok_for_stub_provider():
    """Stub provider always answers — ping reports ok."""
    result = ping()
    assert result["ok"] is True
    assert result["provider"] == "stub"
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)
    assert result["cached"] is False


def test_ping_caches_successful_result(monkeypatch):
    """A second call within the success TTL hits the cache instead of the SDK."""
    first = ping()
    assert first["cached"] is False
    second = ping()
    assert second["cached"] is True
    assert second["ok"] is True
    # Latency from the cached payload is the *original* one, not zero.
    assert second["latency_ms"] == first["latency_ms"]


def test_ping_force_bypasses_cache():
    """``force=True`` re-probes even when a fresh cache entry exists."""
    ping()
    forced = ping(force=True)
    assert forced["cached"] is False


def test_ping_reports_failure_when_provider_raises(monkeypatch):
    """A provider whose complete() raises must surface as ``ok=False`` with
    the exception text in ``error`` — never propagate the exception."""

    class _BoomProvider:
        name = "stub"

        def complete(self, _req: Any) -> Any:
            raise RuntimeError("simulated SDK explosion")

        async def acomplete(self, _req: Any) -> Any:
            raise RuntimeError("unused")

    # Patch the gateway's provider lookup to return our boom.
    import aiops.llm.gateway as gateway

    monkeypatch.setattr(gateway, "get_provider", lambda *_a, **_k: _BoomProvider())

    result = ping()
    assert result["ok"] is False
    assert "RuntimeError" in result["error"]
    assert "simulated SDK explosion" in result["error"]
    assert result["latency_ms"] is None


def test_ping_short_ttl_on_failure(monkeypatch):
    """Failures cache for only 10s so the user sees green quickly after a fix.
    Verify by setting cache TTL behavior — we just check the second call still
    reports cached=True within the window."""
    import aiops.llm.gateway as gateway

    state = {"raise": True}

    class _Boom:
        name = "stub"

        def complete(self, _req: Any) -> Any:
            if state["raise"]:
                raise RuntimeError("first time")
            from aiops.llm import LLMResponse  # type: ignore[import-not-found]

            return LLMResponse(text="pong", model="stub", provider="stub")

        async def acomplete(self, _req: Any) -> Any:
            return self.complete(_req)

    monkeypatch.setattr(gateway, "get_provider", lambda *_a, **_k: _Boom())

    failure = ping()
    assert failure["ok"] is False
    cached_failure = ping()
    assert cached_failure["cached"] is True
    assert cached_failure["ok"] is False


def test_ping_reasoning_model_gets_larger_token_budget(monkeypatch):
    """GPT-5 / o-series pings must request more max_tokens than vanilla
    models because reasoning models burn most of the budget on internal
    thinking before emitting any output. Verify by inspecting the request
    that reaches the provider."""
    import aiops.llm.gateway as gateway

    captured: dict[str, Any] = {}

    class _CapturingProvider:
        name = "openai"

        def complete(self, req: Any) -> Any:
            captured["max_tokens"] = req.max_tokens
            from aiops.llm import LLMResponse  # type: ignore[import-not-found]

            return LLMResponse(text="", model=req.model or "gpt-5", provider="openai")

        async def acomplete(self, req: Any) -> Any:
            return self.complete(req)

    monkeypatch.setattr(gateway, "get_provider", lambda *_a, **_k: _CapturingProvider())
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AIOPS_LLM_MODEL", "gpt-5")

    # Cap is 4096 by default; reasoning ping wants 2048 which is under the cap.
    ping()
    assert captured["max_tokens"] >= 2048


def test_ping_non_reasoning_model_uses_small_token_budget(monkeypatch):
    """Non-reasoning models (e.g. gpt-4o) only need a tiny budget — keeps
    cost negligible even if a tenant disables caching."""
    import aiops.llm.gateway as gateway

    captured: dict[str, Any] = {}

    class _CapturingProvider:
        name = "openai"

        def complete(self, req: Any) -> Any:
            captured["max_tokens"] = req.max_tokens
            from aiops.llm import LLMResponse  # type: ignore[import-not-found]

            return LLMResponse(text="ok", model=req.model or "gpt-4o", provider="openai")

        async def acomplete(self, req: Any) -> Any:
            return self.complete(req)

    monkeypatch.setattr(gateway, "get_provider", lambda *_a, **_k: _CapturingProvider())
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AIOPS_LLM_MODEL", "gpt-4o")

    ping()
    assert captured["max_tokens"] <= 64, "non-reasoning ping should use a tiny budget"


def test_ping_cache_isolation_between_tests():
    """Sanity: the autouse fixture must wipe the cache so adjacent tests are
    independent. Without this, a flaky LLM cached as ``ok=False`` would
    poison every other test."""
    assert "payload" not in health_mod._cache or time.monotonic() > health_mod._cache.get(
        "expires_at", 0.0
    )
