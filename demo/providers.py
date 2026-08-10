"""Registration of demo-supplied tool providers.

Some platform capabilities are declared by ``aiops/`` but can only be *served*
by the demo layer, because serving them means touching the demo's system under
test. ``automation.fault.clear`` is the case that matters: undoing an injected
fault means calling ``demo.ecommerce.failure_injection``, and the platform
package must never import the demo package — the dependency arrow runs
demo → aiops. Registering from this side keeps that arrow intact while still
letting a platform-side executor drive the capability by name.

Providers register as an import side effect of the ``@tool`` decorator, so a
process that never imports the provider module has no provider for the
capability. That used to be true of everything except the FastAPI server:
``python -m agents.runbook_executor`` and the test suite would dispatch
``automation.fault.clear`` into a registry that had never heard of it. Call this
from every demo entry point instead of scattering side-effect imports.

    from demo.providers import register_demo_providers
    register_demo_providers()

Deliberately a plain function rather than entry-point discovery: entry points
need a reinstall to take effect, add ``importlib.metadata`` scanning to every
``import aiops.tools``, and would make the "no provider registered" state — now
a tested contract — awkward to reach. If third-party providers ever need to
register without editing this file, an ``AIOPS_TOOL_PROVIDERS`` env var listing
importable modules is the natural generalisation.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# capability -> the tool name that serves it.
_PROVIDERS = {"automation.fault.clear": "ecommerce.fault.clear"}


def register_demo_providers() -> None:
    """Import the demo provider modules and bind their capabilities.

    Idempotent, and deliberately does not stop at the import. The ``@tool``
    decorator runs as an import side effect, so it fires exactly once per
    process — a second import is a no-op against Python's module cache. That
    makes the import alone insufficient: if the capability is later rebound or
    unbound (a test swapping providers, or ``select_provider`` pointing
    elsewhere), re-importing cannot restore it, because there is nothing left to
    execute. Re-pointing the registry explicitly is what makes calling this at
    any entry point actually reliable.
    """
    from aiops.tools import get_registry

    registry = get_registry()

    # Registers ecommerce.fault.clear -> automation.fault.clear on first import.
    from demo.ui import fault_clear as _fault_clear  # noqa: F401

    for capability, tool_name in _PROVIDERS.items():
        if tool_name not in {t.name for t in registry.list()}:
            logger.warning("demo provider %s did not register for %s", tool_name, capability)
            continue
        registry.select_provider(capability, tool_name)

    logger.debug("registered demo tool providers: %s", ", ".join(_PROVIDERS))


__all__ = ["register_demo_providers"]
