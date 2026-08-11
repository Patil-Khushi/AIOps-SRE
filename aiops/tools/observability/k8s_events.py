"""Kubernetes provider for ``observability.events.query``.

Why this exists
---------------
Kubernetes Events are the signal that separates two failures sharing one alert:
``BackOff`` before a port binds means a crashloop, ``OOMKilled`` means the memory
leak. ConfigMap writes are how a configuration change becomes visible at all.

Until now the only code fetching either lived in
``agents/log_correlation/timeline_sources.py``, which imports the ``kubernetes``
client directly. That is the sole place in the repo where an *agent* reaches past
the tool registry to a vendor SDK — every other retrieval already goes through a
capability — and nothing tested it, because the existing import guard only covers
``anthropic`` and ``openai``. Promoting the two API calls here closes that hole and
brings the calls under ``resilience.guard``'s timeout, retry and breaker, which they
have never had: today an unreachable API server costs the full timeout on every
correlation with nothing to stop the next one trying again.

This provider fetches and normalises. It does not interpret.
------------------------------------------------------------
Everything that decides *what a Kubernetes event means for a service* stays in the
agent: which reasons count as a deployment, how a generated pod name maps back to a
workload, whether a ConfigMap belongs to this service, the ``flagd-config``
special case, and the decision to read ConfigMap ``managedFields`` rather than
Events. That is domain judgement, argued at length in ``timeline_sources.py``, and
moving it into the platform would be a real architecture regression — the platform
would then own one agent's attribution rules.

So the payload here is deliberately **unfiltered**: every event and every ConfigMap
in the namespace, flattened into plain dicts. The agent filters locally, exactly as
it does today. One API call for the whole namespace rather than one per candidate,
because this runs on the incident path.

Timestamps are ISO-8601 strings
-------------------------------
The Kubernetes client returns ``datetime`` objects, but this payload gets cached and
serialised by ``aiops/context/``, so it has to survive a JSON round-trip. The
consumer parses them back at the boundary. Emitting ``datetime`` here would work
today and break the first time a context is written to a cache or a log.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from aiops.tools.registry import ToolResult, tool

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """Whether this provider may talk to a cluster.

    Read per call and defaulting to off. Per-call because an import-time constant
    cannot be reached by a pytest fixture — the lesson
    ``tests/conftest.py::_opt_in_enrichment_seams_off`` exists to work around for
    three other flags. Off by default because a developer with a live kubeconfig
    must not have every ``correlate()`` silently start hitting their cluster.
    """
    return os.environ.get("AIOPS_K8S_EVENTS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _namespace(explicit: str | None = None) -> str:
    return explicit or os.environ.get("AIOPS_K8S_NAMESPACE", "ecommerce")


def _timeout() -> float:
    return float(os.environ.get("AIOPS_K8S_TIMEOUT", "5"))


def _core_api() -> Any | None:
    """Build a CoreV1Api client, or ``None`` when one is not available.

    The ``kubernetes`` import is **inside** the function on purpose.
    ``aiops/tools/observability/__init__.py`` imports this module at package import
    time, and ``agents/log_correlation/agent.py`` imports that package at module
    scope. A top-level ``from kubernetes import client`` would therefore make the
    Log Correlation agent unimportable on a ``--extra dev`` install, since
    ``kubernetes>=29`` ships only in the ``ui`` extra. Shape copied from
    ``timeline_sources._k8s_clients`` for exactly this reason.
    """
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client.CoreV1Api(client.ApiClient())
    except Exception as exc:
        logger.debug("k8s events: kube client unavailable (%s)", exc)
        return None


def _iso(value: Any) -> str | None:
    """Normalise a client timestamp to an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _flatten_event(item: Any) -> dict[str, Any] | None:
    """One Kubernetes Event as a plain dict, or ``None`` if it carries no object.

    All three timestamp fields are emitted rather than collapsed to one. The
    consumer picks ``last_timestamp or event_time or first_timestamp`` in that
    order, and which of the three is populated varies by Kubernetes version and by
    how the event was recorded — choosing here would silently drop events whose
    only populated field is not the one we picked.
    """
    obj = getattr(item, "involved_object", None)
    if obj is None:
        return None
    return {
        "involved_object": {
            "kind": getattr(obj, "kind", None) or "",
            "name": getattr(obj, "name", None) or "",
            "namespace": getattr(obj, "namespace", None) or "",
        },
        "reason": getattr(item, "reason", None) or "",
        "message": getattr(item, "message", None) or "",
        "type": getattr(item, "type", None) or "",
        "count": getattr(item, "count", None),
        "last_timestamp": _iso(getattr(item, "last_timestamp", None)),
        "event_time": _iso(getattr(item, "event_time", None)),
        "first_timestamp": _iso(getattr(item, "first_timestamp", None)),
    }


def _flatten_configmap(cm: Any) -> dict[str, Any] | None:
    """One ConfigMap's identity and ``managedFields``, without its data.

    The ``data`` block is deliberately **not** included. It is the one field likely
    to hold a credential, it is not needed to detect that a configuration changed,
    and this payload is destined for an LLM prompt and an audit log. Not collecting
    it is a stronger guarantee than redacting it afterwards.
    """
    meta = getattr(cm, "metadata", None)
    if meta is None or not getattr(meta, "name", None):
        return None
    managed = []
    for field in getattr(meta, "managed_fields", None) or []:
        managed.append(
            {
                "manager": getattr(field, "manager", None) or "unknown",
                "operation": getattr(field, "operation", None) or "write",
                "time": _iso(getattr(field, "time", None)),
            }
        )
    return {
        "name": meta.name,
        "namespace": getattr(meta, "namespace", None) or "",
        "resource_version": getattr(meta, "resource_version", None),
        "managed_fields": managed,
    }


@tool(
    name="k8s.observability.events.query",
    capability="observability.events.query",
    provider="kubernetes",
    description=(
        "List Kubernetes Events and ConfigMap managedFields for a namespace. "
        "Read-only and unfiltered; the caller decides which objects are relevant."
    ),
)
def query_events(namespace: str | None = None, include_configmaps: bool = True) -> ToolResult:
    """Every Event and (optionally) ConfigMap in ``namespace``.

    Returns ``{"events": [...], "configmaps": [...], "namespace": str}``.

    Two failure modes are reported differently on purpose. A disabled provider or an
    absent kube client is ``ok=False`` with a ``metadata["reason"]`` a caller can map
    onto its own wording — nobody could look. A failed API call is ``ok=False`` with
    ``error`` — we looked and it broke. Collapsing them would let "we could not
    check" be read as "nothing was deployed", and only the second exonerates a
    deploy.

    ``include_configmaps`` exists because the two reads have different costs and a
    caller wanting only pod restarts should not pay for a ConfigMap listing.
    """
    if not _enabled():
        return ToolResult(
            ok=False,
            error="kubernetes events provider disabled",
            metadata={
                "provider": "kubernetes",
                "reason": "disabled",
                "flag": "AIOPS_K8S_EVENTS_ENABLED",
            },
        )

    core = _core_api()
    if core is None:
        return ToolResult(
            ok=False,
            error="no kube client",
            metadata={"provider": "kubernetes", "reason": "no_client"},
        )

    ns = _namespace(namespace)
    timeout = int(_timeout())

    try:
        listing = core.list_namespaced_event(ns, timeout_seconds=timeout)
    except Exception as exc:
        logger.warning("k8s events: event listing failed: %s", exc)
        return ToolResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"provider": "kubernetes", "reason": "event_list_failed"},
        )

    events = [
        flat
        for flat in (_flatten_event(item) for item in (listing.items or []))
        if flat is not None
    ]

    configmaps: list[dict[str, Any]] = []
    configmaps_error: str | None = None
    if include_configmaps:
        try:
            cm_listing = core.list_namespaced_config_map(ns, timeout_seconds=timeout)
        except Exception as exc:
            # A ConfigMap RBAC denial is common and must not cost the events we
            # already have. Partial success is reported as success with a note, not
            # as failure — the alternative discards good data to report a
            # secondary problem.
            logger.debug("k8s events: configmap listing failed (%s)", exc)
            configmaps_error = f"{type(exc).__name__}: {exc}"
        else:
            configmaps = [
                flat
                for flat in (_flatten_configmap(cm) for cm in (cm_listing.items or []))
                if flat is not None
            ]

    metadata: dict[str, Any] = {
        "provider": "kubernetes",
        "namespace": ns,
        "event_count": len(events),
        "configmap_count": len(configmaps),
    }
    if configmaps_error is not None:
        metadata["configmaps_error"] = configmaps_error

    return ToolResult(
        ok=True,
        data={"namespace": ns, "events": events, "configmaps": configmaps},
        metadata=metadata,
    )
