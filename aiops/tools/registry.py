"""Tool registry.

A *tool* is a callable an agent can invoke against the outside world: query
Prometheus, create a ServiceNow ticket, post to Slack, run kubectl. Tools are
registered by capability name (``"observability.metrics.query"``,
``"itsm.incident.create"``); the active *provider* for that capability is
selected via configuration. Swapping providers is a config change, not an
agent rewrite.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., ToolResult]
    capability: str
    provider: str
    requires_hitl: bool = False
    """If True, callers must satisfy ``aiops.policy.gate`` before invocation."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._active: dict[str, str] = {}
        """capability -> tool name. Set by ``select_provider``."""

    def register(self, t: Tool) -> None:
        if t.name in self._tools:
            raise ValueError(f"Tool {t.name!r} already registered")
        self._tools[t.name] = t
        self._active.setdefault(t.capability, t.name)

    def select_provider(self, capability: str, tool_name: str) -> None:
        if tool_name not in self._tools:
            raise KeyError(f"Tool {tool_name!r} not registered")
        if self._tools[tool_name].capability != capability:
            raise ValueError(
                f"Tool {tool_name!r} provides "
                f"{self._tools[tool_name].capability!r}, not {capability!r}"
            )
        self._active[capability] = tool_name

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def by_capability(self, capability: str) -> Tool:
        name = self._active.get(capability)
        if name is None:
            raise KeyError(f"No provider registered for capability {capability!r}")
        return self._tools[name]

    def call(self, capability: str, **kwargs: Any) -> ToolResult:
        t = self.by_capability(capability)
        sig = inspect.signature(t.fn)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        try:
            result = t.fn(**accepted)
        except Exception as exc:  # noqa: BLE001 — boundary
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, ToolResult):
            return ToolResult(ok=True, data=result)
        return result


_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _REGISTRY


def tool(
    *,
    name: str,
    capability: str,
    provider: str,
    description: str = "",
    requires_hitl: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register a function as a tool.

    Usage::

        from aiops.tools import tool, ToolResult

        @tool(name="snow.incident.create", capability="itsm.incident.create",
              provider="servicenow")
        def create_incident(short_description: str, urgency: int) -> ToolResult:
            ...
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY.register(
            Tool(
                name=name,
                description=description or (fn.__doc__ or "").strip().split("\n")[0],
                fn=fn,
                capability=capability,
                provider=provider,
                requires_hitl=requires_hitl,
            )
        )
        return fn

    return deco
