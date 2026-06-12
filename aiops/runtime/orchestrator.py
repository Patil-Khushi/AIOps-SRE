"""Reactive-Active orchestrator (INFRA-2, issue #74).

The v0 Orchestrator runtime component. It runs the Reactive-Active flow as an
explicit, straight-line sequence of agent calls:

    RA-001 Alert Triage → RA-002 Incident Classifier → RA-003 Auto-Ticketing
    → RA-005 Notification Router

This is a *pure relocation* of the chain that lived inline in the demo server's
``/api/triage`` route handler (``demo/ui/server.py``). The behavior is byte-for-
byte identical — same call order, same FK guards on persistence, same
route-failure containment, same response shape via :meth:`ReactiveFlowResult.to_api_dict`.
Extracting it gives every caller (the triage endpoint, the live-alert sweep,
the auto-triage loop, and now RA-008 Incident Commander) one seam to call
instead of re-implementing the chain.

Dependency direction: this module imports the agents and ``aiops.state`` /
``aiops.tools``. Agents never import ``aiops.runtime``, so there is no cycle.

Vendor-neutrality (CLAUDE.md #1): no SDK imports here — the agents own their
tool calls through the registry; this module only sequences them.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from agents.alert_triage import Alert, TriageVerdict, triage
from agents.auto_ticketing import TicketRecord
from agents.auto_ticketing import ticket as auto_ticket
from agents.incident_classifier import Classification, ClassificationInput, classify
from agents.notification_router import RoutingDecision
from agents.notification_router import route as route_notification
from aiops.state import repository as state_repo
from aiops.tools.chatops import DeliveryResult

logger = logging.getLogger(__name__)


class ReactiveFlowResult(BaseModel):
    """Structured outcome of one Reactive-Active flow run.

    Carries the typed agent outputs so in-process consumers (RA-008) get the
    models directly, plus the persisted row ids so a consumer can foreign-key
    follow-on writes. :meth:`to_api_dict` reproduces the exact ``/api/triage``
    response shape for the HTTP boundary.

    The ``routing`` / ``deliveries`` pair encodes three distinct states, all of
    which the original inline code produced and which callers depend on:

    - ``routing=None, deliveries=None``  — RA-005 ``route`` raised; pipeline
      still succeeded (routing failure is non-fatal).
    - ``routing=<decision>, deliveries={}`` — verdict was Suppressed, so
      ``route`` returned no chatops deliveries (empty actions short-circuit
      the emit) but the decision object still exists.
    - ``routing=<decision>, deliveries={...}`` — happy path.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    verdict: TriageVerdict
    verdict_id: int | None
    classification: Classification
    classification_id: int | None
    ticket: TicketRecord
    routing: RoutingDecision | None
    deliveries: dict[str, DeliveryResult] | None
    notification_id: int | None

    def to_api_dict(self) -> dict[str, Any]:
        """Reproduce the legacy ``POST /api/triage`` response body verbatim.

        Kept as an explicit method (rather than ``model_dump``) because the
        response shape is a public contract consumed by the React dashboard,
        the classifier SPA, and existing tests — it must not drift if this
        model gains fields.
        """
        notifications = self.routing.model_dump(mode="json") if self.routing is not None else None
        if self.deliveries is None:
            deliveries: dict[str, Any] | None = None
        else:
            deliveries = {name: r.model_dump(mode="json") for name, r in self.deliveries.items()}
        return {
            "verdict": self.verdict.model_dump(mode="json"),
            "ticket": self.ticket.model_dump(mode="json"),
            "classification": self.classification.model_dump(mode="json"),
            "notifications": notifications,
            "deliveries": deliveries,
            "persisted": {
                "verdict_id": self.verdict_id,
                "classification_id": self.classification_id,
                "notification_id": self.notification_id,
            },
        }


def run_reactive_flow(alert: Alert) -> ReactiveFlowResult:
    """Run RA-001 → RA-002 → RA-003 → RA-005 for one alert.

    Caller owns ``Alert`` construction (and any HTTP-level validation mapping);
    this function takes a validated ``Alert`` and never raises on a routing
    failure — that is logged and surfaced as ``routing=None`` so the rest of
    the pipeline's output is still returned.

    Read-only with respect to destructive systems: RA-003's ticket create is
    OPTIONAL-HITL and gated inside the registry; nothing here pages, executes a
    runbook, or runs remediation.
    """
    # RA-001: triage persists its own verdict row and hands back the id so the
    # downstream classification / notification writes can foreign-key it (#61).
    verdict, verdict_id = triage(alert)

    # RA-002: classify BEFORE ticketing (DEMO-3 / #55) so the ServiceNow
    # incident's category + description classification block are populated at
    # create time rather than patched in later. Persistence needs the verdict
    # FK; skip it (not crash) when triage's own persistence failed.
    classification = classify(ClassificationInput(alert=alert, triage_verdict=verdict))
    classification_id: int | None = None
    if verdict_id is not None:
        classification_id = state_repo.save_classification(classification, verdict_id=verdict_id)

    # RA-003: alert_name is the Prometheus rule name (Alert.metric), used to
    # attach the matching Grafana panel to the ServiceNow incident (DEMO-8/#60).
    ticket_record = auto_ticket(
        verdict,
        classification=classification,
        alert_name=alert.metric,
    )

    # RA-005: routing failure must not break the pipeline — the JSONL chatops
    # audit log is the durable record, and the response still returns with
    # everything else populated and notifications=None.
    routing: RoutingDecision | None = None
    deliveries: dict[str, DeliveryResult] | None = None
    notification_id: int | None = None
    try:
        outcome = route_notification(verdict)
        routing = outcome.decision
        deliveries = outcome.deliveries
        # CHAT-2 (#82): persist the structured row alongside the JSONL audit
        # log. A persistence failure must not break the pipeline — the JSONL
        # adapter (source of truth) already wrote.
        try:
            if verdict_id is not None:
                notification_id = state_repo.save_notification(routing, verdict_id=verdict_id)
        except Exception:
            logger.exception(
                "RA-005: persist save_notification failed for verdict %s on %s "
                "(JSONL audit log still written)",
                verdict_id,
                verdict.affected_service,
            )
    except Exception:
        logger.exception("RA-005: routing failed for verdict on %s", verdict.affected_service)

    return ReactiveFlowResult(
        verdict=verdict,
        verdict_id=verdict_id,
        classification=classification,
        classification_id=classification_id,
        ticket=ticket_record,
        routing=routing,
        deliveries=deliveries,
        notification_id=notification_id,
    )
