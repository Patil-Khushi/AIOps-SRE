"""Reactive-Active orchestrator (INFRA-2, issue #74).

The v0 Orchestrator runtime component. It runs the Reactive-Active flow as an
explicit, straight-line sequence of agent calls:

    RA-001 Alert Triage → RA-002 Incident Classifier → RA-003 Auto-Ticketing
    → RA-005+006 Notification Assembler

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
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from agents.alert_triage import (
    Alert,
    Classification,
    ClassificationInput,
    TriageVerdict,
    classify,
    triage,
)
from agents.auto_ticketing import TicketRecord
from agents.auto_ticketing import ticket as auto_ticket
from agents.notification_assembler import RoutingDecision, WarRoomAssembly
from agents.notification_assembler import notify as notify_incident
from aiops.state import repository as state_repo
from aiops.tools.chatops import DeliveryResult

logger = logging.getLogger(__name__)

# How far back the shared context's window reaches. Matches the lookback every
# consumer already used independently — RCA's log window, notification's trace
# lookback, alert_triage's trace candidates all default to "the last 15
# minutes" — so building one shared window does not change what any of them
# would have asked for on their own.
_CONTEXT_WINDOW = timedelta(minutes=15)


def _build_shared_context(alert: Alert) -> dict[str, Any] | None:
    """Collect once, for every consumer of this incident, instead of each agent
    fetching its own evidence.

    Returns ``None`` when the Context Engineering Layer is off — building a
    context nobody will read is wasted work, and every downstream ``triage()``/
    ``notify()`` call already tolerates ``context=None`` as "fetch live" (see
    ``aiops/context/config.py`` for why the mode is read per call rather than
    cached). A build failure degrades the same way: this function never raises,
    because a missing context must cost evidence, not the reactive flow.
    """
    from aiops.context import config as context_config

    if context_config.context_mode() == "off":
        return None

    try:
        from agents.alert_triage.context_adapter import (
            build_context_request_specs as triage_specs,
        )
        from agents.notification_assembler.context_adapter import (
            build_context_request_specs as notification_specs,
        )
        from aiops.context.builder import ContextBuilder, ContextRequest

        window_end = alert.timestamp
        window_start = window_end - _CONTEXT_WINDOW
        specs = [*triage_specs(alert), *notification_specs(alert.service)]
        request = ContextRequest(
            service=alert.service,
            window_start=window_start,
            window_end=window_end,
            specs=specs,
            severity=alert.severity_hint or "unknown",
            alert_id=alert.alert_id,
            alert_name=alert.metric,
        )
        return ContextBuilder().build(request).model_dump(mode="json")
    except Exception:
        logger.exception(
            "shared context build failed for alert %s on %s; agents will fetch live",
            alert.alert_id,
            alert.service,
        )
        return None


class ReactiveFlowResult(BaseModel):
    """Structured outcome of one Reactive-Active flow run.

    Carries the typed agent outputs so in-process consumers (RA-008) get the
    models directly, plus the persisted row ids so a consumer can foreign-key
    follow-on writes. :meth:`to_api_dict` reproduces the exact ``/api/triage``
    response shape for the HTTP boundary.

    The ``routing`` / ``deliveries`` pair encodes three distinct states, all of
    which the original inline code produced and which callers depend on:

    - ``routing=None, deliveries=None``  — the Notification Assembler raised;
      pipeline still succeeded (notification failure is non-fatal).
    - ``routing=<decision>, deliveries={}`` — verdict was Suppressed, so
      ``notify`` returned no chatops deliveries (empty actions short-circuit
      the emit) but the decision object still exists.
    - ``routing=<decision>, deliveries={...}`` — happy path.

    ``war_room`` is the combined agent's RA-006 half: ``None`` when notification
    failed/was unavailable, ``assembled=False`` for Sev-3/Sev-4 (no room), and a
    bridge-enriched assembly for Sev-1/Sev-2. The demo server records it for the
    incident feed.
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
    war_room: WarRoomAssembly | None = None

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
    """Run RA-001 → RA-002 → RA-003 → RA-005+006 for one alert.

    Caller owns ``Alert`` construction (and any HTTP-level validation mapping);
    this function takes a validated ``Alert`` and never raises on a routing
    failure — that is logged and surfaced as ``routing=None`` so the rest of
    the pipeline's output is still returned.

    Read-only with respect to destructive systems: RA-003's ticket create is
    OPTIONAL-HITL and gated inside the registry; nothing here pages, executes a
    runbook, or runs remediation.
    """
    # Built once, before RA-001, so both triage() and notify_incident() below
    # draw from the SAME collected evidence instead of each independently
    # querying Prometheus/Jaeger for the same service. A no-op (returns None)
    # unless AIOPS_CONTEXT_LAYER is on.
    shared_context = _build_shared_context(alert)
    # The kwarg is passed only when there is something to pass. triage()/
    # notify_incident() already default context to None, so omitting it when
    # AIOPS_CONTEXT_LAYER is off (the common case) is behaviourally identical
    # AND keeps this call compatible with any caller — including test stubs —
    # written against the pre-migration one-argument signature.
    context_kwargs: dict[str, Any] = (
        {"context": shared_context} if shared_context is not None else {}
    )

    # RA-001: triage persists its own verdict row and hands back the id so the
    # downstream classification / notification writes can foreign-key it (#61).
    verdict, verdict_id = triage(alert, **context_kwargs)

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

    # RA-005+006 Notification Assembler: route ONE notification and, on
    # Sev-1/Sev-2, stand up the war room and fold its join link into that same
    # message. A failure here must not break the pipeline — the JSONL chatops
    # audit log is the durable record, and the response still returns with
    # everything else populated and notifications=None.
    routing: RoutingDecision | None = None
    deliveries: dict[str, DeliveryResult] | None = None
    notification_id: int | None = None
    war_room: WarRoomAssembly | None = None
    try:
        outcome = notify_incident(verdict, **context_kwargs)
        routing = outcome.decision
        deliveries = outcome.deliveries
        war_room = outcome.war_room
        # CHAT-2 (#82): persist the structured row alongside the JSONL audit
        # log. A persistence failure must not break the pipeline — the JSONL
        # adapter (source of truth) already wrote.
        try:
            if verdict_id is not None:
                notification_id = state_repo.save_notification(routing, verdict_id=verdict_id)
        except Exception:
            logger.exception(
                "RA-005+006: persist save_notification failed for verdict %s on %s "
                "(JSONL audit log still written)",
                verdict_id,
                verdict.affected_service,
            )
    except Exception:
        logger.exception(
            "RA-005+006: notification/war-room failed for verdict on %s",
            verdict.affected_service,
        )

    return ReactiveFlowResult(
        verdict=verdict,
        verdict_id=verdict_id,
        classification=classification,
        classification_id=classification_id,
        ticket=ticket_record,
        routing=routing,
        deliveries=deliveries,
        notification_id=notification_id,
        war_room=war_room,
    )
