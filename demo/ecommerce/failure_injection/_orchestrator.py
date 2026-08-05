"""Failure Orchestrator — routes a failure to its application and/or infra layer.

Two things decide what actually runs:

  * the **requested mode** (``--mode`` flag, else ``FI_MODE``, else ``hybrid``) —
    what the operator asked for;
  * the failure's **declared layer** (``Failure.layer``) — what that failure can
    actually do.

The intersection is what executes. Asking for ``--mode application`` on an
infrastructure-only failure runs *nothing* and says so, rather than silently
firing real chaos — an infra-only failure keeps its chaos action in ``inject``
(there is no second implementation to put there), so dispatching purely on the
requested mode would fire tc/stress-ng/kubectl for an operator who explicitly
asked for the env-var path.

    FI_MODE=application      env vars / ConfigMaps / scale-to-zero only
    FI_MODE=infrastructure   tc, stress-ng, kubectl, dd, DNS only
    FI_MODE=hybrid           both, where the failure supports both (default)
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from ._base import ChaosUnavailable, InjectionLayer

if TYPE_CHECKING:
    from ._base import Failure

logger = logging.getLogger(__name__)


class OrchestrationMode(Enum):
    """Which layer(s) the operator asked to drive."""
    APPLICATION = "application"
    INFRASTRUCTURE = "infrastructure"
    HYBRID = "hybrid"


def _coerce_mode(mode: Optional[OrchestrationMode | str]) -> OrchestrationMode:
    """Resolve an explicit mode, else FI_MODE, else hybrid."""
    if isinstance(mode, OrchestrationMode):
        return mode
    raw = mode if mode is not None else os.getenv("FI_MODE", "hybrid")
    try:
        return OrchestrationMode(str(raw).lower())
    except ValueError:
        logger.warning(
            "invalid mode %r; expected 'application', 'infrastructure', or 'hybrid'. "
            "Falling back to 'hybrid'.",
            raw,
        )
        return OrchestrationMode.HYBRID


def _plan(
    failure: Failure,
    mode: OrchestrationMode,
    action: str,
) -> dict[str, Optional[Callable[[], None]]]:
    """Resolve which callable runs in each layer slot.

    ``None`` means "this layer does not apply to this failure" — reported as
    skipped, not as a failure. ``action`` is "inject" or "recover".
    """
    app_attr = "inject" if action == "inject" else "recover"
    infra_attr = f"{app_attr}_infra"

    supports_app = failure.layer in (InjectionLayer.APPLICATION, InjectionLayer.HYBRID)
    supports_infra = failure.layer in (InjectionLayer.INFRASTRUCTURE, InjectionLayer.HYBRID)

    want_app = mode in (OrchestrationMode.APPLICATION, OrchestrationMode.HYBRID)
    want_infra = mode in (OrchestrationMode.INFRASTRUCTURE, OrchestrationMode.HYBRID)

    plan: dict[str, Optional[Callable[[], None]]] = {
        "application": None,
        "infrastructure": None,
    }

    if supports_app and want_app:
        plan["application"] = getattr(failure, app_attr)

    if supports_infra and want_infra:
        # A HYBRID failure keeps its chaos action in the *_infra slot. An
        # infra-only failure has no app implementation, so its chaos action is
        # the plain inject/recover — route that here rather than to the app slot.
        plan["infrastructure"] = getattr(failure, infra_attr, None) or (
            getattr(failure, app_attr) if failure.layer is InjectionLayer.INFRASTRUCTURE else None
        )

    return plan


def _execute(failure: Failure, mode: OrchestrationMode, action: str) -> dict:
    """Run `action` ("inject"/"recover") across the planned layers."""
    mode = _coerce_mode(mode)
    plan = _plan(failure, mode, action)

    results: dict = {
        "ok": True,
        "failure_key": failure.key,
        "mode": mode.value,
        "declared_layer": failure.layer.value,
        "layers": {},
    }

    for layer_name, fn in plan.items():
        if fn is None:
            results["layers"][layer_name] = {
                "ok": True,
                "status": "skipped",
                "reason": f"{failure.key} declares layer={failure.layer.value}",
            }
            continue
        try:
            logger.info("%sing %s (%s layer)", action, failure.key, layer_name)
            fn()
            results["layers"][layer_name] = {"ok": True, "status": "ran"}
        except ChaosUnavailable as exc:
            # The environment cannot host this layer (no tc, no CAP_NET_ADMIN).
            # Not an error: if a sibling layer landed, the failure is injected,
            # and failing the whole call would send the operator chasing a fault
            # that is actually active.
            logger.warning("%s layer unavailable for %s: %s", layer_name, failure.key, exc)
            results["layers"][layer_name] = {
                "ok": True,
                "status": "unavailable",
                "reason": str(exc),
            }
        except Exception as exc:
            logger.exception("%s layer %s failed for %s", layer_name, action, failure.key)
            results["layers"][layer_name] = {
                "ok": False,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            results["ok"] = False

    statuses = [step["status"] for step in results["layers"].values()]
    results["degraded"] = "unavailable" in statuses and "ran" in statuses

    # Nothing actually happened — either the operator asked for a layer this
    # failure does not implement, or every applicable layer was unavailable.
    # Surfaced as a failure so a HITL "fix" that quietly did nothing cannot read
    # as success.
    if "ran" not in statuses:
        results["ok"] = False
        blocked = [
            step["reason"]
            for step in results["layers"].values()
            if step["status"] == "unavailable"
        ]
        results["error"] = (
            "; ".join(blocked)
            if blocked
            else (
                f"{failure.key} declares layer={failure.layer.value}; "
                f"nothing to run in mode={mode.value}"
            )
        )

    return results


def inject(failure: Failure, mode: Optional[OrchestrationMode | str] = None) -> dict:
    """Inject `failure` across whichever layers `mode` and the failure both allow."""
    return _execute(failure, _coerce_mode(mode), "inject")


def recover(failure: Failure, mode: Optional[OrchestrationMode | str] = None) -> dict:
    """Recover `failure` across whichever layers `mode` and the failure both allow."""
    return _execute(failure, _coerce_mode(mode), "recover")


__all__ = ["OrchestrationMode", "inject", "recover"]
