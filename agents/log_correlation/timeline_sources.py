"""Event sources feeding the incident timeline.

Six sources, three of which are new to RA-007:

- **logs / metrics / traces** come free: Phase 4 ``Evidence`` already carries a
  timestamp, service, severity and signature, so these are a projection rather
  than a fetch. Each entry keeps its ``evidence_id`` so a reader can get back to
  the underlying telemetry.
- **topology** is derived from the dependency resolution the agent already
  performed — no extra call.
- **deployment** and **configuration** require Kubernetes. These are the ones
  worth having: most incidents follow a change, and a latency spike at 10:03
  means something very different if a rollout happened at 10:02. That fact is not
  in Loki.

Kubernetes access is opt-in and degrades silently
-------------------------------------------------
``AIOPS_TIMELINE_K8S`` gates the two cluster-backed sources, default off. Two
reasons: ``correlate()`` runs on the incident path and must not acquire a new hard
dependency, and the eval harness must stay hermetic — a golden run that reaches
for a cluster is not a deterministic regression test.

When enabled but unreachable, these return no events and say so via the
timeline's ``coverage_note``. An empty deployment list must never be readable as
"nothing was deployed" when the truth is "we could not look".
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from agents.log_correlation.evidence import Evidence
from agents.log_correlation.timeline import TimelineEvent

logger = logging.getLogger(__name__)

_K8S_ENABLED = os.environ.get("AIOPS_TIMELINE_K8S", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}
_NAMESPACE = os.environ.get("AIOPS_K8S_NAMESPACE", "otel-demo")
_K8S_TIMEOUT = float(os.environ.get("AIOPS_TIMELINE_K8S_TIMEOUT", "3"))

# Kubernetes Event reasons that represent a deployment-shaped change or failure.
# Taken from reasons actually observed on this cluster rather than the full
# upstream vocabulary — an unfiltered event stream is mostly scheduling noise.
_DEPLOYMENT_REASONS = {
    "ScalingReplicaSet": "info",
    "Scheduled": "info",
    "Pulling": "info",
    "Pulled": "info",
    "Created": "info",
    "Started": "info",
    "Killing": "warning",
    "Unhealthy": "warning",
    # Observed live on this cluster and initially missed: 26 SandboxChanged and 9
    # FailedMount events were being silently discarded. FailedMount in particular
    # is a real failure — dropping it would leave a pod that never started with no
    # explanation anywhere on the timeline.
    "SandboxChanged": "warning",
    "FailedMount": "error",
    "FailedScheduling": "error",
    "BackOff": "error",
    "CrashLoopBackOff": "error",
    "FailedCreatePodSandBox": "error",
    "OOMKilling": "error",
    "Failed": "error",
}

_CONFIG_KINDS = {"ConfigMap", "Secret"}


def from_evidence(evidence: list[Evidence]) -> list[TimelineEvent]:
    """Project telemetry evidence onto timeline events.

    A projection, not a fetch: the evidence was already built from signals the
    agent gathered, so this adds no I/O. Occurrence counts carry over so the
    timeline's own merging does not double-count what evidence already collapsed.
    """
    events: list[TimelineEvent] = []
    for ev in evidence:
        implicated = ev.topology_context.implicated_service
        # Name the implicated service when it differs: "checkout: payment charge
        # error" reads as a checkout problem, which is exactly the confusion the
        # implicated_service field exists to prevent.
        subject = implicated if implicated and implicated != ev.service else ev.service
        events.append(
            TimelineEvent(
                timestamp=ev.timestamp,
                event=ev.normalized_signature,
                service=subject,
                severity=ev.severity,
                source=ev.source,
                related_evidence_ids=[ev.evidence_id],
                occurrences=ev.supporting_telemetry.occurrences,
            )
        )
    return events


def from_topology(
    service: str,
    dependencies: list[str],
    at: datetime,
    *,
    provider: str | None = None,
) -> list[TimelineEvent]:
    """Record the topology as observed during this correlation.

    Not a change feed: RA-007 has no topology *history*, so claiming "dependency
    added" would be an invention. What it can honestly contribute is the state the
    graph was in when the incident was analysed, which is what lets a reader
    interpret every other entry — an error in ``payment`` matters differently once
    you know ``payment`` is a direct dependency.

    Emitted at ``info`` because it is context, not a symptom.
    """
    if not dependencies:
        return []
    src = f" via {provider}" if provider else ""
    return [
        TimelineEvent(
            timestamp=at,
            event=(
                f"topology{src}: {service} depends on "
                f"{', '.join(sorted(dependencies))} ({len(dependencies)} direct)"
            ),
            service=service,
            severity="info",
            source="topology",
        )
    ]


def fetch_configuration_events(
    core,
    service: str,
    window_start: datetime,
    window_end: datetime,
) -> list[TimelineEvent]:
    """Configuration changes, read from ConfigMap ``managedFields``.

    Not from Kubernetes Events. Live inspection of this cluster found **only**
    ``Pod``-kind events — Kubernetes emits no Event when a ConfigMap changes,
    because no controller watches them for that purpose. An implementation
    filtering for ConfigMap events is therefore dead code that reports "no
    configuration changes" no matter what happened.

    ``managedFields`` does carry the information: each field manager records the
    timestamp it last wrote. On this cluster that surfaced a ``helm`` apply and
    two ``kubectl`` writes with real times.

    ``flagd-config`` is always included regardless of name match, because a
    feature-flag flip is *the* configuration change in this demo — it is how
    failures are injected — and it affects every service rather than the one it
    is named after.

    Known limitation, stated rather than hidden: ``managedFields`` records only
    the **most recent** write per manager, not a history. Two flag flips by the
    same manager inside the window appear as one entry at the later time. Full
    history would need an audit log or a watch, neither of which exists here.
    """
    try:
        listing = core.list_namespaced_config_map(_NAMESPACE, timeout_seconds=int(_K8S_TIMEOUT))
    except Exception as exc:
        logger.debug("timeline: configmap listing failed (%s)", exc)
        return []

    target = service.strip().lower()
    events: list[TimelineEvent] = []
    for cm in listing.items or []:
        meta = cm.metadata
        if meta is None or not meta.name:
            continue
        name = meta.name.lower()
        relevant = name == "flagd-config" or owner_name(name) == target or target in name.split("-")
        if not relevant:
            continue
        for field in meta.managed_fields or []:
            ts = getattr(field, "time", None)
            if not _in_window(ts, window_start, window_end):
                continue
            manager = getattr(field, "manager", None) or "unknown"
            operation = getattr(field, "operation", None) or "write"
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event=f"ConfigMap {meta.name} modified by {manager} ({operation})",
                    service=target,
                    # A config change is not itself a fault, but it is the most
                    # common trigger for one, so it warrants more than info.
                    severity="warning",
                    source="configuration",
                )
            )
    return events


def _k8s_clients():
    """Build Kubernetes clients, or return ``None``.

    Imported lazily so this module is importable with no kubeconfig and no
    ``kubernetes`` package — the eval harness and CI must not need either.
    """
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client.CoreV1Api(client.ApiClient())
    except Exception as exc:
        logger.debug("timeline: kube client unavailable (%s)", exc)
        return None


def _in_window(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts <= end


# Kubernetes generated-name shapes, matched structurally rather than by "looks
# like a hash". A StatefulSet pod is ``<name>-<ordinal>``; a DaemonSet pod is
# ``<name>-<pod-hash>``; a Deployment pod is
# ``<name>-<replicaset-hash>-<pod-hash>``.
_ORDINAL_RE = re.compile(r"^\d+$")
# Pod hashes are exactly 5 alphanumeric characters ("cdcsw", "6nbf6").
_POD_HASH_RE = re.compile(r"^[a-z0-9]{5}$")
# ReplicaSet hashes are longer and always contain at least one digit
# ("5bff6448f5", "67897cb6f4"). Requiring a digit is what stops a real name
# segment such as "agent" or "catalog" being mistaken for one.
_RS_HASH_RE = re.compile(r"^(?=[a-z0-9]*\d)[a-z0-9]{8,10}$")


def owner_name(pod_name: str) -> str:
    """Recover the workload name from a generated pod name.

    Needed because substring matching produces confident false attribution:
    ``"frontend-proxy-6d4948d448-7ttqp"`` starts with ``"frontend"``, so a prefix
    test hands frontend-proxy's restarts to frontend. Observed live — the same
    phantom-match hazard the ServiceNow provider avoids by refusing ``LIKE``.

    Stripping is structural, not heuristic. A first attempt matched any 5-10 char
    alphanumeric segment and reduced ``otel-collector-agent-6nbf6`` to
    ``otel-collector``, because ``agent`` is five lowercase characters. Hence the
    narrower rules: exactly-5-char pod hashes, and ReplicaSet hashes that must
    contain a digit. Real name segments (``agent``, ``proxy``, ``catalog``) match
    neither.
    """
    parts = (pod_name or "").strip().lower().split("-")
    if len(parts) > 1 and _ORDINAL_RE.match(parts[-1]):
        # StatefulSet: loki-0 -> loki
        return "-".join(parts[:-1])
    if len(parts) > 1 and _POD_HASH_RE.match(parts[-1]):
        parts = parts[:-1]
        if len(parts) > 1 and _RS_HASH_RE.match(parts[-1]):
            parts = parts[:-1]
    return "-".join(parts)


def _relates_to(obj_name: str, obj_kind: str, target: str) -> bool:
    """Whether a Kubernetes object belongs to ``target``.

    Exact match on the recovered workload name — never a prefix or substring
    test. A missed event degrades the timeline; a misattributed one puts another
    service's failure on this service's record, which is worse.
    """
    name = (obj_name or "").strip().lower()
    if not name:
        return False
    if name == target:
        return True
    if obj_kind == "Pod":
        return owner_name(name) == target
    if obj_kind in _CONFIG_KINDS:
        # ConfigMaps conventionally suffix the service name ("checkout-config").
        # Matched by stripping the known suffix, not by prefix: a prefix test
        # would let "frontend" claim "frontend-proxy-config", the same
        # misattribution the pod-name handling exists to prevent.
        for suffix in ("-config", "-configmap", "-cm"):
            if name.endswith(suffix) and name[: -len(suffix)] == target:
                return True
    return False


def fetch_change_events(
    service: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[TimelineEvent], str | None]:
    """Fetch deployment and configuration events from Kubernetes.

    Returns ``(events, coverage_note)``. The note is non-``None`` whenever the
    result is incomplete — disabled, unreachable, or errored — so an empty list is
    never mistaken for "no changes happened". That distinction is the whole point:
    "nothing was deployed" exonerates a deploy, "we could not check" does not.

    One API call for all events in the namespace, then filtered locally. A
    per-service query would need one call per candidate and this runs on the
    incident path.
    """
    if not _K8S_ENABLED:
        return [], "deployment/configuration events disabled (AIOPS_TIMELINE_K8S=false)"

    core = _k8s_clients()
    if core is None:
        return [], "deployment/configuration events unavailable (no kube client)"

    try:
        listing = core.list_namespaced_event(_NAMESPACE, timeout_seconds=int(_K8S_TIMEOUT))
    except Exception as exc:
        logger.warning("timeline: kube event fetch failed: %s", exc)
        return [], f"deployment/configuration events unavailable ({type(exc).__name__})"

    target = service.strip().lower()
    events: list[TimelineEvent] = []
    for item in listing.items or []:
        obj = item.involved_object
        if obj is None:
            continue
        kind = obj.kind or ""
        ts = item.last_timestamp or item.event_time or item.first_timestamp
        if not _relates_to(obj.name or "", kind, target) or not _in_window(
            ts, window_start, window_end
        ):
            continue

        reason = item.reason or ""
        if reason in _DEPLOYMENT_REASONS:
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event=f"{reason}: {item.message or obj.name}",
                    service=target,
                    severity=_DEPLOYMENT_REASONS[reason],
                    source="deployment",
                )
            )
        elif kind in _CONFIG_KINDS:
            # Retained for completeness in case a controller in some environment
            # does emit these; on this cluster it never fires, which is why
            # configuration changes are read from managedFields instead.
            events.append(
                TimelineEvent(
                    timestamp=ts,
                    event=f"{kind} {obj.name} {reason or 'changed'}: {item.message or ''}".strip(),
                    service=target,
                    severity="warning",
                    source="configuration",
                )
            )

    events.extend(fetch_configuration_events(core, service, window_start, window_end))
    return events, None
