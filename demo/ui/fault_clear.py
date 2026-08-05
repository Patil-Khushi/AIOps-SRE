"""Provider for ``automation.fault.clear`` — undo an injected ecommerce fault.

This is what the RCA apply-fix loop actually executes once a human approves a
fix step. ``aiops/tools/rca_remediation.py`` dispatches the approved step to the
``automation.fault.clear`` capability without knowing how faults work; this
module supplies the ecommerce answer.

**Why the provider lives here and not in aiops/.** Undoing a fault means calling
``demo.ecommerce.failure_injection``, and the platform package must never import
the demo package — the dependency arrow runs demo → aiops. Registering the
provider from the demo layer keeps that arrow intact while still letting a
platform-side executor drive it.

Before the OTel Demo was removed, this path called ``feature_flags.set_variant``
to flip a flagd flag off. There is no flag daemon now: an ecommerce fault is an
env var on a Deployment or a StatefulSet scaled to zero.

Importing this module registers the capability, exactly like
``aiops.tools.observability``.
"""

from __future__ import annotations

import logging

from aiops.tools.registry import ToolResult, tool

logger = logging.getLogger(__name__)


@tool(
    name="ecommerce.fault.clear",
    capability="automation.fault.clear",
    provider="ecommerce",
    description="Recover an injected ecommerce failure scenario.",
)
def clear_fault(fault: str = "", target: str = "off", **_: object) -> ToolResult:
    """Recover the failure identified by ``fault`` (a failure key).

    ``fault`` is a ``failure_key`` such as ``order_service.http_500``. It maps
    to the old ``flag`` argument, which is why the RCA verdict's ``flag`` field
    is still populated with the failure key — see demo/ui/scenario_provider.py.

    ``target`` mirrors the old ``variant``. Only ``"off"`` is meaningful: this
    capability *clears* faults. A request to set a fault ON is rejected rather
    than silently ignored — an approved "fix" that broke something further
    would be the worst possible outcome of a HITL flow.
    """
    if not fault:
        return ToolResult(ok=False, error="automation.fault.clear requires a 'fault' key")

    if target not in ("off", "", None):
        return ToolResult(
            ok=False,
            error=(
                f"automation.fault.clear only clears faults; refusing target={target!r}. "
                "Injecting a fault is a scenario action, not a remediation."
            ),
            metadata={"fault": fault, "refused_target": target},
        )

    # Imported lazily so an unavailable SUT package degrades this one capability
    # rather than breaking import of the whole demo server.
    try:
        from demo.ecommerce.failure_injection import FAILURES
    except Exception as exc:
        return ToolResult(ok=False, error=f"failure_injection unavailable: {exc}")

    failure = FAILURES.get(fault)
    if failure is None:
        return ToolResult(
            ok=False,
            error=f"unknown fault {fault!r}",
            metadata={"available_faults": sorted(FAILURES)},
        )

    try:
        from demo.ecommerce.failure_injection import recover
        result = recover(failure)
        if not result["ok"]:
            return ToolResult(
                ok=False,
                error=f"orchestrator recovery failed: {result.get('error')}",
            )
    except Exception as exc:
        logger.exception("clearing fault %s failed", fault)
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    return ToolResult(
        ok=True,
        data={"fault": fault, "service": failure.service, "cleared": True},
        metadata={"provider": "ecommerce"},
    )
