"""HTTP surface for the production Runbook Executor flow (§33/§34).

A sibling router, not more of ``server.py``: it imports only ``agents`` and ``aiops``
(never ``demo.ui.server``), so a slow or broken runbook surface cannot take the
Triage→…→RCA pipeline down with it, and there is no circular import to arrange.

Five endpoints, one per step of the flow the frontend renders:

    POST /api/runbook-executor/candidates          discover + rank (read-only)
    POST /api/runbook-executor/plan                select + re-validate + dry run
    POST /api/runbook-executor/execute             execute once, behind the HITL gate
    GET  /api/runbook-executor/executions[/{id}]   durable state, steps, audit, UI state
    GET  /api/runbook-executor/metrics             the §31 counters

Two responsibilities live here rather than in the agent, deliberately:

- **Deployment-specific translation.** Which Prometheus alert implies which failure
  category, and which words in an alert summary imply which observed signal, are facts
  about *this* deployment. The agent stays vendor-neutral and takes them as input.
- **Triggering verification.** The executor hands off a payload; this module fires
  ``resolution_verifier.trigger`` fire-and-forget, exactly the way the RCA fix-apply
  path already does (``server.py::_post_fix_verify``). The agent never imports the
  verifier, so it cannot grow an opinion about recovery.

The existing endpoints (``POST /api/demo/runbook-executor/run``,
``GET /api/runbook-executor/runbooks``) are untouched and keep working.
"""

from __future__ import annotations

import contextlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from agents.runbook_executor import (
    ExecutionRecord,
    IncidentContext,
    discover_candidates,
    execute_plan,
    metrics,
    plan_execution,
    ui_state_for,
)
from aiops.state import repository
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runbook-executor", tags=["runbook-executor"])

# Execution blocks inside the platform HITL gate while a human decides, so a gated run
# cannot be awaited on the request thread (the browser would time out long before the
# approval window closes). Same shape as server.py's _HITL_AGENT_POOL: fire it on a
# pool thread, return the execution handle, let the client poll.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="runbook-exec")

# Alert name → the generic failure category the runbook library declares. This is
# deployment knowledge (these rules live in infra/observability/prometheus-values.yaml),
# so it belongs on this side of the seam. Mirrors the FAULT_FACETS table in
# scripts/generate_runbooks.py, which is what the runbooks were generated from.
ALERT_CATEGORY: dict[str, str] = {
    "EcommerceMySQLDown": "dependency_unavailable",
    "EcommercePostgresDown": "dependency_unavailable",
    "EcommerceRedisDown": "dependency_unavailable",
    "EcommercePaymentGatewayUnreachable": "dependency_unavailable",
    "EcommerceServiceDown": "pod_crashloop",
    "EcommercePaymentTimeouts": "dependency_timeout",
    "EcommerceOrderErrorRateHigh": "application_error",
    "EcommerceUserLoginFailures": "application_error",
    "EcommerceOrderLatencyHigh": "latency_degradation",
    "EcommerceUserServiceCPUHigh": "resource_saturation_cpu",
    "EcommercePaymentServiceCPUHigh": "resource_saturation_cpu",
    "EcommerceOrderServiceMemoryHigh": "resource_saturation_memory",
}

# Alert name → the signals that alert is evidence of.
ALERT_SIGNALS: dict[str, list[str]] = {
    "EcommerceMySQLDown": ["dependency_unavailable", "error_rate_high"],
    "EcommercePostgresDown": ["dependency_unavailable", "error_rate_high"],
    "EcommerceRedisDown": ["dependency_unavailable", "error_rate_high"],
    "EcommercePaymentGatewayUnreachable": ["dependency_unavailable"],
    "EcommerceServiceDown": ["service_down", "pod_restarting"],
    "EcommercePaymentTimeouts": ["timeouts", "latency_high"],
    "EcommerceOrderErrorRateHigh": ["error_rate_high"],
    "EcommerceUserLoginFailures": ["error_rate_high"],
    "EcommerceOrderLatencyHigh": ["latency_high"],
    "EcommerceUserServiceCPUHigh": ["cpu_saturation", "latency_high"],
    "EcommercePaymentServiceCPUHigh": ["cpu_saturation", "latency_high"],
    "EcommerceOrderServiceMemoryHigh": ["memory_saturation", "pod_restarting"],
}

# Free-text symptom → observed signal. Used when no alert name is available (a manually
# raised incident, or a summary-only hand-off), so matching still has something real to
# score rather than falling back to keyword tag overlap alone.
SUMMARY_SIGNALS: dict[str, list[str]] = {
    "5xx": ["error_rate_high"],
    "500": ["error_rate_high"],
    "error rate": ["error_rate_high"],
    "errors": ["error_rate_high"],
    "timeout": ["timeouts"],
    "timing out": ["timeouts"],
    "latency": ["latency_high"],
    "slow": ["latency_high"],
    "p95": ["latency_high"],
    "cpu": ["cpu_saturation"],
    "throttl": ["cpu_saturation"],
    "memory": ["memory_saturation"],
    "oom": ["memory_saturation", "pod_restarting"],
    "crashloop": ["pod_restarting", "service_down"],
    "restarting": ["pod_restarting"],
    "unavailable": ["dependency_unavailable"],
    "connection refused": ["dependency_unavailable"],
    "down": ["service_down"],
    # The two faults with no Prometheus rule (payment_service.disk_full,
    # order_service.packet_loss) can only ever arrive as a manually raised or
    # summary-only incident, so free text is the ONLY route by which their runbooks
    # become reachable. Without these needles those runbooks would score on service
    # alone and never rank above the catch-all restart.
    "disk": ["disk_saturation"],
    "no space": ["disk_saturation"],
    "enospc": ["disk_saturation"],
    "packet loss": ["packet_loss"],
    "retransmit": ["packet_loss"],
}


def _environment() -> str:
    """Which environment this deployment is. Read per call, never at import.

    Defaults to ``demo`` rather than ``production``: claiming production would inflate
    every risk assessment on a laptop, and a real deployment sets the variable.
    """
    return os.environ.get("AIOPS_ENVIRONMENT", "").strip() or "demo"


def _derive_signals(alert_name: str, summary: str, provided: list[str]) -> list[str]:
    """Observed signals from the alert name plus the summary text, order-stable."""
    out: list[str] = list(provided)
    out += ALERT_SIGNALS.get(alert_name, [])
    blob = (summary or "").lower()
    for needle, signals in SUMMARY_SIGNALS.items():
        if needle in blob:
            out += signals
    seen: set[str] = set()
    unique: list[str] = []
    for signal in out:
        key = signal.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _alert_firing(alert_name: str) -> bool | None:
    """Is this alert currently firing? ``None`` when Prometheus cannot be reached.

    Tri-state on purpose: "we could not check" must not read as "it stopped firing",
    which would make the executor refuse every remediation whenever the metrics stack
    is down. The prerequisite treats ``None`` as SKIPPED, not FAILED.
    """
    if not alert_name:
        return None
    try:
        res = get_registry().call("observability.metrics.alerts")
    except Exception:  # pragma: no cover - defensive
        return None
    if not res.ok:
        return None
    alerts = (res.data or {}).get("alerts", []) or []
    for alert in alerts:
        labels = (alert or {}).get("labels", {}) or {}
        if labels.get("alertname") == alert_name:
            return str((alert or {}).get("state", "firing")).lower() in ("firing", "pending")
    return False


class IncidentPayload(BaseModel):
    """What the frontend knows about the incident, as the API receives it.

    Every field is length-capped. There is no auth on this API (the deliberate POC
    posture documented at the HITL-2 note in ``server.py``), so cheap caps are what stop
    an unbounded field from being keyword-scanned, ranked, and then persisted verbatim
    into an execution row — the same class of compensating control ``rca_chat_routes.py``
    applies to its own unauthenticated surface. The limits are generous for real inputs:
    an alert summary is a sentence, not a document.
    """

    incident_id: str = Field("", max_length=128, description="ServiceNow / internal incident id")
    service: str = Field(..., min_length=1, max_length=128)
    severity: str | None = Field(None, max_length=32)
    alert_name: str = Field("", max_length=128, description="Prometheus alertname, when known")
    summary: str = Field(
        "", max_length=4000, description="Triage summary — keyword-scanned for signals"
    )
    tags: list[str] = Field(default_factory=list, max_length=32)
    environment: str = Field("", max_length=64, description="Defaults to AIOPS_ENVIRONMENT")
    failure_category: str = Field(
        "", max_length=64, description="Overrides the alert-derived category"
    )
    incident_type: str = Field("", max_length=64, description="RA-002 classification, when known")
    observed_signals: list[str] = Field(default_factory=list, max_length=64)
    incident_status: str = Field(
        "active", max_length=32, description="active / resolved / cancelled / …"
    )
    detected_at: datetime | None = None
    probe_alert: bool = Field(True, description="Ask Prometheus whether the alert is still firing")

    @field_validator("tags", "observed_signals")
    @classmethod
    def _cap_items(cls, value: list[str]) -> list[str]:
        """Cap each element too — 32 items of 1 MB each is still unbounded."""
        return [item[:64] for item in value]

    def to_context(self) -> IncidentContext:
        """Build the agent's :class:`IncidentContext` from the payload.

        Everything the agent cannot know for itself is resolved here: the environment,
        the alert→category translation, the observed signals, and the live alert state.
        """
        alert = self.alert_name.strip()
        return IncidentContext(
            incident_id=self.incident_id.strip(),
            service=self.service.strip(),
            environment=self.environment.strip() or _environment(),
            severity=self.severity,
            alert_name=alert,
            failure_category=self.failure_category.strip() or ALERT_CATEGORY.get(alert, ""),
            incident_type=self.incident_type.strip(),
            tags=list(self.tags),
            observed_signals=_derive_signals(alert, self.summary, self.observed_signals),
            summary=self.summary,
            incident_status=self.incident_status.strip() or "active",
            detected_at=self.detected_at or datetime.now(UTC),
            alert_firing=_alert_firing(alert) if self.probe_alert else None,
        )


class PlanRequest(IncidentPayload):
    """A plan request: an incident, plus optionally the runbook an SRE picked."""

    runbook_id: str | None = Field(
        None,
        max_length=128,
        description="SRE selection. Omitted = auto-select when exactly one applies.",
    )
    selected_by: str = Field("", max_length=128, description="Who picked it (audit)")


class ExecuteRequest(PlanRequest):
    """An execute request. Re-plans from scratch so nothing is trusted from the client.

    The client cannot hand us a plan to run — it names the incident and the runbook, and
    the server re-derives candidates, applicability and the dry run before executing.
    A client-supplied plan would be an unvalidated execution path, which is the thing
    §11 exists to prevent.
    """

    approval_timeout_seconds: int = Field(120, ge=5, le=900)
    approver: str = Field("", max_length=128, description="Recorded on the execution when known")
    synchronous: bool = Field(
        False,
        description=(
            "Wait for the run to finish. Only for ungated plans and tests — a gated run "
            "blocks until a human answers."
        ),
    )
    # Verification hand-off (§29) — passed straight to resolution_verifier, which
    # re-reads the DETECTION-time signals. The executor computes none of this itself.
    alert_signature: str = Field("", max_length=2000)
    metric_query: str = Field("", max_length=2000)
    health_query: str = Field("", max_length=2000)
    threshold: float | None = None


def _verification_status(incident_id: str) -> str:
    """The Resolution Verifier's current verdict for this incident, read-only."""
    if not incident_id:
        return ""
    try:
        from agents.resolution_verifier.verifier import get_status

        status = get_status(incident_id) or {}
    except Exception:  # pragma: no cover - the verifier is optional
        return ""
    return str(status.get("status") or status.get("verdict") or "")


def _execution_view(record: ExecutionRecord) -> dict[str, Any]:
    """One execution, shaped for the frontend state model (§34).

    ``ui_state`` is computed here — from the durable state *and* the verifier's verdict
    — so the UI never has to infer it, and never shows "resolved" off the back of a
    completed execution alone.
    """
    verification = _verification_status(record.incident_id)
    # Feed the §31 pass-rate from the verdict as it is observed, once per execution —
    # the executor still does not *decide* it, it only counts what the verifier said.
    if record.is_terminal and verification:
        verdict = verification.strip().lower()
        if verdict in ("pass", "passed"):
            metrics.incr_once("verification_pass", record.execution_id)
        elif verdict in ("fail", "failed"):
            metrics.incr_once("verification_fail", record.execution_id)
    payload = record.model_dump(mode="json")
    payload["ui_state"] = ui_state_for(state=record.state, verification=verification).value
    payload["verification_status"] = verification
    payload["is_terminal"] = record.is_terminal
    return payload


@router.post("/candidates")
def post_candidates(req: IncidentPayload) -> dict[str, Any]:
    """Rank every runbook that could handle this incident (§4). Read-only."""
    ctx = req.to_context()
    result = discover_candidates(ctx)
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "auto_selected": result.auto_selected,
        "incident": ctx.model_dump(mode="json"),
        "candidates": [c.model_dump(mode="json") for c in result.candidates],
        "ui_state": ("RUNBOOKS_FOUND" if result.candidates else "NO_RUNBOOK"),
    }


@router.post("/plan")
def post_plan(req: PlanRequest) -> dict[str, Any]:
    """Select, re-validate and dry-run (§15–§17). Mutates nothing in production.

    Reserving an execution row is the only write, and only for a READY dry run — that
    reservation is what makes the subsequent execute idempotent.
    """
    ctx = req.to_context()
    plan = plan_execution(
        ctx,
        runbook_id=req.runbook_id,
        selected_by=req.selected_by or "operator",
    )
    return {
        "decision": plan.decision.value,
        "reason": plan.reason,
        "ui_state": plan.ui_state.value,
        "selected_runbook_id": plan.selected_runbook_id,
        "selected_runbook_version": plan.selected_runbook_version,
        "selected_by": plan.selected_by,
        "execution_id": plan.execution_id,
        "execution_state": plan.execution_state.value if plan.execution_state else None,
        "already_executed": plan.already_executed,
        "blocking_reasons": plan.blocking_reasons,
        "warnings": plan.warnings,
        "candidates": [c.model_dump(mode="json") for c in plan.candidates],
        "dry_run": plan.dry_run.model_dump(mode="json") if plan.dry_run else None,
        "incident": ctx.model_dump(mode="json"),
    }


def _trigger_verification(req: ExecuteRequest, result: Any) -> None:
    """Fire the Resolution Verifier for an executed plan (§29). Never raises.

    Deliberately the same wiring as the RCA fix-apply path: the executor produced a
    handoff describing what it *did*, and the verifier independently re-reads the
    detection-time signals to decide whether the incident recovered. Nothing about the
    verdict flows back into the executor's result.
    """
    handoff = getattr(result, "verification_handoff", None)
    if handoff is None or not handoff.incident_id:
        return
    with contextlib.suppress(Exception):
        from agents.resolution_verifier.verifier import VerifyContext, trigger

        trigger(
            VerifyContext(
                incident_id=handoff.incident_id,
                service=handoff.service,
                alert_signature=req.alert_signature or "",
                metric_query=req.metric_query or "",
                threshold=req.threshold,
                health_query=req.health_query or "",
            )
        )
        logger.info(
            "runbook execution %s handed off to resolution_verifier for %s",
            handoff.execution_id,
            handoff.incident_id,
        )


@router.post("/execute")
def post_execute(req: ExecuteRequest) -> dict[str, Any]:
    """Execute once, behind the platform HITL gate (§18–§21).

    Re-plans server-side first, so the only thing the client controls is *which* runbook
    to evaluate. A gated plan is dispatched to a pool thread and this returns
    ``WAITING_APPROVAL`` immediately — the run then blocks inside the registry until a
    human resolves the approval in ``/hitl`` (or Slack/Teams). Poll
    ``GET /api/runbook-executor/executions/{execution_id}`` for progress; the execution
    row is the durable source of truth, not this response.
    """
    ctx = req.to_context()
    plan = plan_execution(
        ctx,
        runbook_id=req.runbook_id,
        selected_by=req.selected_by or "operator",
    )
    if not plan.ready:
        if plan.already_executed and plan.execution_id:
            row = repository.get_runbook_execution(plan.execution_id)
            if row is not None:
                record = ExecutionRecord.from_row(row)
                return {
                    "accepted": False,
                    "duplicate": True,
                    "reason": plan.reason,
                    "execution": _execution_view(record),
                }
        return {
            "accepted": False,
            "duplicate": False,
            "decision": plan.decision.value,
            "reason": plan.reason or "no authorized plan to execute",
            "ui_state": plan.ui_state.value,
            "blocking_reasons": plan.blocking_reasons,
            "candidates": [c.model_dump(mode="json") for c in plan.candidates],
            "dry_run": plan.dry_run.model_dump(mode="json") if plan.dry_run else None,
        }

    hitl_context: dict[str, Any] = {
        "approval_id": plan.execution_id,
        "approval_timeout_seconds": req.approval_timeout_seconds,
    }
    if req.approver:
        hitl_context["approver"] = req.approver

    if req.synchronous:
        result = execute_plan(plan, ctx, hitl_context=hitl_context)
        _trigger_verification(req, result)
        row = repository.get_runbook_execution(plan.execution_id or "")
        return {
            "accepted": True,
            # ``duplicate_of`` is set whenever this request collapsed onto an existing
            # execution rather than starting one, whatever that execution's outcome was.
            "duplicate": result.duplicate_of is not None,
            "result": result.to_api_dict(),
            "execution": _execution_view(ExecutionRecord.from_row(row)) if row else None,
        }

    def _run() -> None:
        try:
            result = execute_plan(plan, ctx, hitl_context=hitl_context)
            _trigger_verification(req, result)
        except Exception:  # pragma: no cover - a pool thread must not die silently
            logger.exception("runbook execution %s crashed", plan.execution_id)

    _POOL.submit(_run)
    row = repository.get_runbook_execution(plan.execution_id or "")
    return {
        "accepted": True,
        "duplicate": False,
        "execution_id": plan.execution_id,
        "approval_id": plan.execution_id if plan.dry_run and plan.dry_run.hitl_required else None,
        "hitl_required": plan.dry_run.hitl_required if plan.dry_run else True,
        "risk_level": plan.dry_run.risk_level.value if plan.dry_run else None,
        "ui_state": (
            "WAITING_APPROVAL" if plan.dry_run and plan.dry_run.hitl_required else "EXECUTING"
        ),
        "dry_run": plan.dry_run.model_dump(mode="json") if plan.dry_run else None,
        "execution": _execution_view(ExecutionRecord.from_row(row)) if row else None,
    }


@router.get("/executions/{execution_id}")
def get_execution(execution_id: str) -> dict[str, Any]:
    """Durable state, steps, audit trail and UI state for one execution."""
    row = repository.get_runbook_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no execution {execution_id!r}")
    return _execution_view(ExecutionRecord.from_row(row))


@router.get("/executions")
def list_executions(
    incident_id: str | None = None,
    service: str | None = None,
    runbook_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Newest-first execution history, optionally filtered."""
    rows = repository.list_runbook_executions(
        incident_id=incident_id,
        service=service,
        runbook_id=runbook_id,
        state=state,
        limit=max(1, min(limit, 200)),
    )
    records = [ExecutionRecord.from_row(r) for r in rows]
    return {
        "count": len(records),
        "executions": [_execution_view(r) for r in records],
    }


@router.get("/metrics")
def get_metrics() -> dict[str, Any]:
    """The §31 counters, rates and durations for this process."""
    return metrics.snapshot()


@router.post("/executions/{execution_id}/verification")
def record_verification(execution_id: str, verdict: str = "") -> dict[str, Any]:
    """Record the verifier's verdict against an execution (metrics only).

    The verdict itself is owned by ``resolution_verifier``; this endpoint exists so the
    "verification pass rate after execution" metric has a numerator. It never changes
    the execution's state — an execution that ran is ``completed`` whether or not the
    incident recovered, and conflating the two is exactly what §26 forbids.
    """
    row = repository.get_runbook_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no execution {execution_id!r}")
    outcome = verdict.strip().lower()
    if outcome in ("pass", "passed"):
        metrics.incr("verification_pass")
    elif outcome in ("fail", "failed"):
        metrics.incr("verification_fail")
    else:
        raise HTTPException(status_code=400, detail="verdict must be 'pass' or 'fail'")
    record = ExecutionRecord.from_row(row)
    return {
        "execution_id": execution_id,
        "recorded": outcome,
        "ui_state": ui_state_for(state=record.state, verification=outcome).value,
    }


def register_routes(app: Any) -> None:
    """Mount this router. Called from ``demo/ui/server.py``.

    A function rather than a bare ``include_router`` at import time so the mount point
    is explicit and testable — the same idiom ``chatops_ws`` / ``_alert_hub`` /
    ``rca_progress`` already use.
    """
    app.include_router(router)


__all__ = [
    "ALERT_CATEGORY",
    "ALERT_SIGNALS",
    "ExecuteRequest",
    "IncidentPayload",
    "PlanRequest",
    "register_routes",
    "router",
]
