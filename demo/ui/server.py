"""FastAPI service wrapping the Alert Triage agent for the demo UI.

The full endpoint catalog is served by FastAPI itself — visit ``GET /docs``
(Swagger) or ``GET /openapi.json`` on a running server. We keep the inventory
there instead of in a hand-maintained list that drifts every time someone
adds a route (it last went stale at 12 routes when the file actually had 21).

Roughly, the service hosts: the Alert Triage agent, which now runs triage AND
incident classification (POST /api/triage*, GET /api/fixtures, GET /api/verdicts,
the classification metrics under /api/classifier/*, plus a combined
triage+classification surface at POST /api/combined/run), the failure-injection
scenario endpoints under /api/scenarios/*, the live cluster mirrors
(/api/live-alerts, /api/topology, /api/system/pods), and two WebSocket fan-outs
(/ws/alerts, /ws/chatops). The React dashboard mounts at /dashboard/, the
standalone Incident Classifier SPA at /classifier (linked from the Alert Triage
console sidebar), the combined triage+classification SPA at /combined, and a
legacy vanilla UI at /.

The agent runs in-process (single uvicorn worker = single dedup store) so the
embedding dedup memory persists across triage calls within one server lifetime.

Vendor neutrality: this module uses ``aiops.tools`` capabilities and
``agents.alert_triage`` only — it does not import Prometheus / Jaeger /
Kubernetes clients directly.
"""

from __future__ import annotations

# Make Python's ssl module use the OS trust store (Windows / macOS) so HTTPS
# calls from httpx, openai SDK, etc. accept corporate-proxy re-signed certs
# (Zscaler / Netskope / etc.). Without this, every outbound HTTPS from the
# demo server fails with "CERTIFICATE_VERIFY_FAILED" on machines behind a
# TLS-inspecting proxy — the OS already trusts the corporate CA, but
# Python's bundled certifi store doesn't. Must run BEFORE any module that
# opens an SSL connection (httpx, openai, etc.).
#
# truststore is a hard dep in pyproject.toml, so the ImportError branch is
# theoretically unreachable. Kept defensive intentionally: if a future
# slim/container build trims optional deps or a fresh checkout runs the
# server before `uv sync`, the demo should still boot (with stock certifi)
# instead of crashing the FastAPI startup. The try/except also keeps this
# block a single self-contained import for ruff's E402 rule.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load .env explicitly. ``uv run`` does NOT auto-load .env files, so without
# this every uvicorn launch sees a bare environment and the dashboard header
# chip is stuck red.
from aiops._dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Default to the stub LLM so the demo runs without an API key. Override by
# setting AIOPS_LLM_PROVIDER=anthropic before launching uvicorn (or by
# editing .env, which _load_dotenv above picks up).
os.environ.setdefault("AIOPS_LLM_PROVIDER", "stub")

# Importing the agent triggers @tool registration for prometheus, jaeger,
# and the mock CMDB / on-call providers.
from agents.alert_triage import (  # noqa: E402
    Alert,
    AuditMetadata,
    TriageVerdict,
    triage_and_classify,
)
from agents.auto_healer_lite import ExecutionRequest  # noqa: E402
from agents.auto_healer_lite import execute as auto_heal_execute  # noqa: E402
from agents.incident_commander import command as incident_command  # noqa: E402
from agents.log_correlation import CorrelationInput  # noqa: E402
from agents.log_correlation import correlate as correlate_signals  # noqa: E402
from agents.notification_assembler import (  # noqa: E402
    assemble_war_room,
    decide_war_room,
)
from agents.rca_agent.agent import analyze as rca_analyze  # noqa: E402
from agents.remediation_recommender import RemediationInput  # noqa: E402
from agents.remediation_recommender import recommend as remediate  # noqa: E402
from aiops import llm as aiops_llm  # noqa: E402
from aiops.policy import (  # noqa: E402
    ApprovalError,
    get_approval_registry,
    install_chatops_listener,
    install_default_approver,
)
from aiops.runtime.orchestrator import run_reactive_flow  # noqa: E402
from aiops.state import init_db  # noqa: E402
from aiops.state import repository as state_repo  # noqa: E402
from aiops.tools import (  # noqa: E402
    get_registry,
)
from aiops.tools import (  # noqa: E402
    oncall as _oncall_tool,  # noqa: F401  — DB-backed oncall provider registration
)
from aiops.tools import (  # noqa: E402
    resolvers as _resolvers_tool,  # noqa: F401  — DB-backed incident.resolvers.lookup registration
)
from aiops.tools.alerts.prometheus_adapter import to_canonical_alert  # noqa: E402
from aiops.tools.chatops import register_env_adapters  # noqa: E402
from demo.providers import register_demo_providers  # noqa: E402
from demo.ui import scenario_provider  # noqa: E402
from demo.ui._alert_hub import register_routes as _register_alert_hub_routes  # noqa: E402
from demo.ui.chatops_ws import bootstrap_websocket_adapter  # noqa: E402
from demo.ui.chatops_ws import register_routes as _register_chatops_ws_routes  # noqa: E402
from demo.ui.rca_progress import bootstrap_rca_progress, make_sink  # noqa: E402
from demo.ui.rca_progress import get_hub as _rca_progress_hub  # noqa: E402
from demo.ui.rca_progress import register_routes as _register_rca_progress_routes  # noqa: E402

logger = logging.getLogger(__name__)

# Serves automation.fault.clear, which the RCA apply-fix path and the runbook
# executor's clear_fault steps both dispatch to. Was a bare side-effect import;
# a named call keeps it from being tidied away as unused.
register_demo_providers()

STATIC_DIR = Path(__file__).parent / "static"
FIXTURES_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "alert_triage" / "evals" / "golden.json"
)


def _warn_if_approval_token_unset() -> None:
    """HITL-2 (#102): web approve/deny endpoints are gated by
    AIOPS_HITL_APPROVAL_TOKEN. When unset, anyone reachable by the FastAPI
    server can resolve any pending Required-HITL request — which would
    violate CLAUDE.md principle #3. Log a single loud line so the operator
    knows demo mode is on."""
    if not os.environ.get("AIOPS_HITL_APPROVAL_TOKEN", "").strip():
        logger.warning("HITL web endpoints are unauthenticated")


def _activate_db_oncall_provider() -> None:
    """Switch the active ``oncall.schedule.lookup`` provider from the mock
    to the DB-backed one once the engineers table has been populated.

    The mock auto-registers via @tool decorators in ``mock_providers``;
    the DB provider registers the same way in ``aiops.tools.oncall``.
    The mock is the default winner because it registered first. We flip
    the active pointer here, in lifespan startup, after ``init_db()`` so
    the DB exists. If the engineers table is empty (dev forgot to seed),
    we leave the mock active and log a single warning — paging-by-DB on
    an empty roster would silently page nobody.
    """
    from sqlmodel import Session, func, select

    from aiops.state import get_engine
    from aiops.state.models import EngineerRow

    with Session(get_engine()) as session:
        # COUNT(*) over scalar — avoid materialising every row just to
        # length-check (negligible at POC scale, but it's the right
        # shape and one fewer thing to grow).
        engineer_count = int(session.exec(select(func.count()).select_from(EngineerRow)).one() or 0)

    if engineer_count == 0:
        # Auto-seed so the demo "just works" — the #1 cause of the on-call
        # showing a generic ``oncall@<team>.example.com`` (the mock provider's
        # placeholder) is simply that nobody ran the seed script. Seeding here
        # activates the DB provider with named engineers and, when
        # ``AIOPS_ONCALL_ROSTER_JSON`` is set (`.env` / `.env.shared`), their
        # real emails + Slack IDs. Opt out with ``AIOPS_ONCALL_AUTOSEED=false``
        # (the test suite does, to keep the hermetic DB empty).
        autoseed = os.environ.get("AIOPS_ONCALL_AUTOSEED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not autoseed:
            logger.warning(
                "oncall: engineers table empty and auto-seed disabled "
                "(AIOPS_ONCALL_AUTOSEED=false); keeping mock provider active. "
                "Run `uv run python -m scripts.seed_oncall` to populate it."
            )
            return
        try:
            from scripts.seed_oncall import _seed

            with Session(get_engine()) as seed_session:
                engineer_count = _seed(seed_session, force=False)
            logger.info(
                "oncall: auto-seeded roster (%d engineers). Set "
                "AIOPS_ONCALL_ROSTER_JSON for real identities instead of placeholders.",
                engineer_count,
            )
        except Exception:
            logger.exception("oncall: auto-seed failed; mock provider stays active")
            return
        if engineer_count == 0:
            return

    try:
        get_registry().select_provider("oncall.schedule.lookup", "db.oncall.schedule.lookup")
        logger.info(
            "oncall: activated DB provider (%d engineers in roster)",
            engineer_count,
        )
    except (KeyError, ValueError):
        logger.exception("oncall: failed to activate DB provider; mock stays active")


def _register_chatops_adapters() -> None:
    """Register the chatops sinks (JSONL audit log + Slack + Teams + PagerDuty).

    Idempotent by adapter class so re-running startup (tests, hot-reload)
    does not register duplicate sinks of the same kind. Without this guard
    the same audit JSON line lands twice per send().

    Slack/Teams/PagerDuty keys are read from the env inside
    register_env_adapters, which logs each adapter as it is registered and
    warns (without raising) on an invalid key."""
    audit_path = Path(__file__).resolve().parents[2] / "demo" / "audit" / "chatops.jsonl"
    register_env_adapters(audit_path=audit_path)


def _ensure_hitl_agent_pool() -> None:
    """HITL-3 (#103): recreate the demo agent pool if a prior FastAPI
    shutdown closed it. Matters for tests that open more than one
    TestClient context against the same module; in production this is a
    no-op on the first (and only) startup."""
    global _HITL_AGENT_POOL
    if _HITL_AGENT_POOL._shutdown:
        _HITL_AGENT_POOL = _new_hitl_agent_pool()


def _shutdown_hitl_agent_pool() -> None:
    """HITL-3 (#103): drain the demo agent pool on app shutdown rather
    than relying on the concurrent.futures atexit hook. ``wait=False``
    because demo agents can be blocked on the HITL gate for up to 900s;
    ``cancel_futures`` drops queued work that hasn't started. Running
    workers are daemons, so they don't keep the process alive."""
    _HITL_AGENT_POOL.shutdown(wait=False, cancel_futures=True)


def _warm_incident_history_embeddings() -> None:
    """Pre-load the RA-007 semantic-retrieval index, off the request path.

    The first embedding search costs ~20s (torch import, model construction, then
    embedding the corpus) against a 3s per-provider guard, so without this the
    tier would be cancelled and breaker-tripped on the first few correlations
    after every restart and quietly answer from the keyword mock instead.

    Non-blocking and best-effort: ``warm()`` spawns a daemon thread and returns.
    Skipped entirely unless the chain actually names the provider, so a deployment
    that has not opted in pays nothing.
    """
    chain = os.environ.get("AIOPS_INCIDENT_HISTORY_PROVIDERS", "")
    if "embedding" not in chain:
        return
    try:
        from aiops.tools.incident_history.providers.embedding import warm

        warm()
        logger.info("incident-history embedding index warming in background")
    except Exception as exc:
        # Enrichment only — a failure here must never stop the server booting.
        logger.warning("incident-history embedding warm-up could not start: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup + shutdown wiring for the demo UI.

    Replaces all prior ``@app.on_event`` hooks (deprecated since FastAPI
    0.104). Order matters:

    1. ``init_db()`` so any later step that persists has somewhere to write.
    2. ``_warn_if_approval_token_unset()`` — HITL-2 (#102).
    3. HITL approval listener + default approver — the listener must register
       before any approval is created so the "created" event reaches the
       sinks registered in step 4.
    4. ``_register_chatops_adapters()`` — JSONL audit log (always) + Slack
       webhook + Teams webhook + PagerDuty (each opt-in via env). Idempotent.
    5. ``bootstrap_websocket_adapter()`` / ``bootstrap_rca_progress()`` — both
       must run inside the asyncio loop so ``asyncio.get_running_loop()``
       resolves to the server's loop. This is why the lifespan is ``async``.
    6. ``_ensure_hitl_agent_pool()`` — HITL-3 (#103).
    7. ``_start_auto_triage()`` — auto-triage loop (#130). Runs last so it
       polls a fully wired stack (DB, chatops sinks, websocket bootstrap).

    Teardown (post-yield):
    - ``_stop_auto_triage()`` — cancel the background loop before draining
      the agent pool so any in-flight triage call gets cancelled first.
    - ``_shutdown_hitl_agent_pool()`` — HITL-3 (#103).
    """
    init_db()
    _warn_if_approval_token_unset()
    _activate_db_oncall_provider()
    install_chatops_listener()
    install_default_approver()
    _register_chatops_adapters()
    bootstrap_websocket_adapter()
    bootstrap_rca_progress()
    _ensure_hitl_agent_pool()
    await _start_auto_triage()
    # PRS-007 SNOW watcher: poll ServiceNow for resolved tickets → synthesize.
    # Decoupled + fire-and-forget; never affects the pipeline above.
    from agents.knowledge_synthesizer.snow_watcher import start_watcher

    await start_watcher()
    _warm_incident_history_embeddings()

    yield

    from agents.knowledge_synthesizer.snow_watcher import stop_watcher

    await stop_watcher()
    await _stop_auto_triage()
    _shutdown_hitl_agent_pool()


app = FastAPI(
    title="Adaptive AIOps — Alert Triage demo",
    version="0.1.0",
    lifespan=lifespan,
)

_register_chatops_ws_routes(app)
_register_rca_progress_routes(app)

# PRS-007 Knowledge Synthesizer HTTP surface. Lives in its own module that
# imports only agents/aiops (never this server), so the synthesizer stays fully
# decoupled from the core pipeline — see demo/ui/knowledge_routes.py.
from demo.ui.knowledge_routes import router as knowledge_router  # noqa: E402

app.include_router(knowledge_router)

# RCA chat HTTP surface — same decoupling rule as the knowledge router above.
from demo.ui.rca_chat_routes import router as rca_chat_router  # noqa: E402

app.include_router(rca_chat_router)

# Teams-share endpoint — deliberately its own module, outside the chat
# boundary's restricted aiops.tools.* import surface. See rca_share_routes.py.
from demo.ui.rca_share_routes import router as rca_share_router  # noqa: E402

app.include_router(rca_share_router)


# ─── routes ─────────────────────────────────────────────────────────────────


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Quick probe: agent importable, mocks registered, port-forwards reachable,
    LLM provider actually answering. The LLM probe is cached (60s success /
    10s failure) so frequent dashboard refreshes don't hammer the API."""
    registry = get_registry()
    caps = sorted({t.capability for t in registry.list()})

    prom_ok = False
    jaeger_ok = False
    try:
        res = registry.call("observability.metrics.query", promql="up")
        prom_ok = bool(res.ok)
    except Exception:
        pass
    try:
        res = registry.call("observability.traces.services")
        jaeger_ok = bool(res.ok)
    except Exception:
        pass

    llm = aiops_llm.ping()

    return {
        "status": "ok",
        "llm_provider": llm["provider"],
        "llm_model": llm["model"],
        "llm_ok": llm["ok"],
        "llm_error": llm["error"],
        "llm_latency_ms": llm["latency_ms"],
        "llm_cached": llm["cached"],
        "registered_capabilities": caps,
        "prometheus_reachable": prom_ok,
        "jaeger_reachable": jaeger_ok,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@app.get("/api/fixtures")
def list_fixtures() -> dict[str, Any]:
    """Return the contents of ``evals/golden.json`` for the UI's fixture pane."""
    if not FIXTURES_PATH.exists():
        raise HTTPException(status_code=500, detail=f"fixtures file not found: {FIXTURES_PATH}")
    with FIXTURES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


class TriageRequest(BaseModel):
    alert: dict[str, Any] = Field(..., description="Canonical Alert payload")


# DEMO closure bridge (UI flow): the dashboard analyzes the live-triage verdict,
# which can be a Suppressed duplicate carrying no ServiceNow ticket (dedup → no
# ticket on that triage). The real incident for the service was opened by an
# earlier triage (e.g. the Inject button's background chain). Record
# service → newest incident number here so apply-fix can recover it when the UI
# has none, and the resolution verifier / close gate (the 2nd HITL approval)
# still fires. In-process + best-effort by design (a restart just falls back to
# the UI-supplied incident_id).
_LATEST_INCIDENT_BY_SERVICE: dict[str, str] = {}


def _norm_service(service: str | None) -> str:
    """Collapse service-name spellings to a stable key: lower-case, strip
    separators, drop a trailing ``service`` suffix — so ``product-catalog``,
    ``productcatalog`` and ``productcatalogservice`` all match."""
    s = (service or "").lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def _record_incident_for_service(flow: Any) -> None:
    """Best-effort: remember the incident number RA-003 just opened, keyed by
    the normalised affected service, for the apply-fix closure fallback."""
    try:
        number = getattr(flow.ticket, "ticket_id", None)
        service = getattr(flow.verdict, "affected_service", None)
        if number and service:
            _LATEST_INCIDENT_BY_SERVICE[_norm_service(service)] = str(number)
    except Exception:
        logger.debug("could not record incident-for-service mapping", exc_info=True)


def _incident_is_open(number: str) -> bool:
    """True when a ServiceNow incident exists and isn't Resolved(6)/Closed(7).
    Used to reject a stale in-process hint pointing at an already-closed ticket
    (which the verifier would skip as already-verified → no close card)."""
    if not number:
        return False
    try:
        res = get_registry().call("itsm.incident.get", number=number, fields="number,state")
        if not getattr(res, "ok", False):
            return False
        rec = (getattr(res, "data", None) or {}).get("incident") or {}
        return str(rec.get("state") or "") not in {"6", "7"}
    except Exception:
        return False


def _latest_incident_for_service(service: str | None) -> str:
    """Resolve the OPEN ServiceNow incident for a service, for the apply-fix
    closure fallback when the UI analyzed a Suppressed verdict with no ticket.

    Prefer the in-process hint (the exact incident RA-003 last opened) but only
    if it's still open; otherwise query ServiceNow for the newest *active*
    incident whose short description names the service (RA-003 writes
    ``[Sev-X] {service}: …``). The ``active=true`` filter guarantees we never
    return a ticket the verifier already closed."""
    hint = _LATEST_INCIDENT_BY_SERVICE.get(_norm_service(service), "")
    if hint and _incident_is_open(hint):
        return hint
    raw = (service or "").strip()
    if not raw:
        return ""
    try:
        q = f"active=true^short_descriptionLIKE{raw}^ORDERBYDESCopened_at"
        res = get_registry().call(
            "itsm.incident.query", query=q, fields="number,short_description,state", limit=1
        )
        if getattr(res, "ok", False):
            rows = (getattr(res, "data", None) or {}).get("incidents", []) or []
            if rows:
                return str(rows[0].get("number") or "")
    except Exception:
        logger.debug("incident-for-service query failed for %r", service, exc_info=True)
    return ""


@app.post("/api/triage", response_model=None)
def triage_alert(req: TriageRequest) -> dict[str, Any]:
    """Triage + classify + auto-ticket + notify chatops for a single alert.

    Body: ``{"alert": {<Alert payload>}}``. Pipeline:
    parse → RA-001 triage → persist verdict → RA-002 classify →
    persist classification → RA-003 auto-ticket (with classification so the
    ticket's ``category`` + description's classification block are populated
    at create time, not patched in later) → RA-005 chatops notify.
    A routing failure is logged but the response still returns 200 with
    everything else populated; ``notifications`` is ``null`` in that case.

    Response: ``{"verdict": TriageVerdict, "ticket": TicketRecord,
    "classification": Classification, "notifications": RoutingDecision | null,
    "deliveries": {adapter_name: DeliveryResult} | null,
    "persisted": {verdict_id, classification_id, notification_id}}``.
    ``notification_id`` is ``null`` when routing failed or when the persistence
    write itself raised (the JSONL audit log is the durable record).
    ``deliveries`` is ``null`` when routing failed and ``{}`` when the verdict
    was suppressed (no chatops emit).
    """
    try:
        alert_obj = Alert(**req.alert)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid alert: {exc}") from exc

    # INFRA-2 (#74): the RA-001 → RA-002 → RA-003 → RA-005 chain lives in the
    # orchestrator seam now; ``to_api_dict`` reproduces this route's historical
    # response shape verbatim. Alert construction + the 400 mapping stay here as
    # HTTP concerns, not pipeline logic.
    result = run_reactive_flow(alert_obj)
    # Remember the incident this triage opened (if any), keyed by service, so the
    # apply-fix closure path can recover it when the UI analyzed a Suppressed
    # verdict that carried no ticket (see _record_incident_for_service).
    _record_incident_for_service(result)

    # RA-005+006 Notification Assembler already routed the single notification
    # and (on Sev-1/Sev-2) stood up the war room inside the flow, folding the
    # join link into that one message. Record the assembly + its notification
    # for the incident feed (/api/war-room/recent + /metrics). Best-effort —
    # never break the triage response.
    try:
        if result.war_room is not None:
            _record_war_room(
                result.war_room.model_dump(mode="json"),
                result.verdict,
                notification=result.routing.model_dump(mode="json") if result.routing else None,
            )
    except Exception:
        logger.exception(
            "RA-005+006: recording incident feed row failed for %s",
            result.verdict.affected_service,
        )

    return result.to_api_dict()


@app.post("/api/triage/fixture/{fixture_id}", response_model=None)
def triage_fixture(fixture_id: str) -> dict[str, Any]:
    """Triage a named fixture from ``golden.json``."""
    data = list_fixtures()
    case = next((c for c in data.get("cases", []) if c.get("id") == fixture_id), None)
    if case is None:
        available = [c.get("id") for c in data.get("cases", [])]
        raise HTTPException(
            status_code=404,
            detail=f"unknown fixture {fixture_id!r}; available: {available}",
        )
    return triage_alert(TriageRequest(alert=case["input"]))


def _synthetic_alerts_for_active_scenarios() -> list[dict[str, Any]]:
    """Canonical-alert payloads for every scenario currently injected.

    The UI emits these directly so AlertStream reflects an Inject within one
    broadcaster tick, without waiting for the real Prometheus rule to cross its
    threshold — several ecommerce rules need ~2 minutes of sustained load before
    they fire. The ``alert_id`` matches the canonical Prom shape
    ``PROM-<alertname>-na`` so the real alert overrides the synthetic one in
    dedup once it does fire.

    Active state is read back from the cluster rather than from flagd, which no
    longer exists. See ``scenario_provider.active_state``.
    """
    try:
        current = scenario_provider.active_state(SCENARIOS)
    except Exception:
        logger.debug("could not read scenario state for synthetic alerts", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for sid, s in SCENARIOS.items():
        if current.get(sid, "off") == "off":
            continue
        out.append(_synthetic_alert_for_scenario(sid, s))
    return out


def _synthetic_alert_for_scenario(sid: str, s: dict[str, Any]) -> dict[str, Any]:
    """Build one canonical Alert payload for a scenario.

    Shared by ``_synthetic_alerts_for_active_scenarios`` (Alert Stream feed)
    and the inject-triggered triage (Notification page + Slack) so both
    surfaces describe the injected failure identically. ``alert_id`` matches
    the canonical Prom shape ``PROM-<alertname>-na`` so a real Prometheus
    alert of the same name dedups against it.
    """
    service = s.get("service") or "unknown"
    # Every flag-bearing scenario in demo/scenarios/*.yaml declares its own
    # ``alert:`` name (inject_scenario reads s["alert"] directly), so this
    # fallback only guards a malformed scenario — it never surfaces a generic
    # catch-all alert name.
    alertname = s.get("alert") or f"{service}-alert"
    flag = s.get("flag") or ""
    # Per-scenario severity (declared in demo/scenarios/*.yaml) so injected
    # failures classify across the full Sev-1..Sev-3 range instead of all
    # landing on Sev-2. RA-001's rule-based classifier maps the hint:
    # critical→Sev-1 (page), high→Sev-2 (notify), warning→Sev-3 (daytime).
    severity = str(s.get("severity") or "high")
    return {
        "alert_id": f"PROM-{alertname}-na",
        "service": service,
        "metric": alertname,
        "value": 1.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "severity_hint": severity,
        "labels": {
            "alertname": alertname,
            "service": service,
            "service_name": service,
            "severity": severity,
            "scenario_id": sid,
            "flag": flag,
            "synthetic": "true",
        },
        "annotations": {
            "summary": s.get("title") or f"Scenario {sid} active on {service}",
            "description": s.get("description") or f"flag={flag} active; injected via dashboard",
        },
    }


@app.get("/api/live-alerts")
def live_alerts() -> dict[str, Any]:
    """Pull currently-firing alerts from Prometheus + merge synthetic alerts
    for any active scenario so Inject is always reflected in AlertStream.

    Real Prometheus alerts win on ``alert_id`` collision — the synthetic
    fallback fills the gap where the OTel demo's ``STATUS_CODE_UNSET`` spans
    keep the upstream ``*ErrorRateHigh`` rules from firing.
    """
    registry = get_registry()
    try:
        res = registry.call("observability.metrics.alerts")
    except KeyError:
        raise HTTPException(  # noqa: B904
            status_code=503, detail="observability.metrics.alerts not registered"
        )
    if not res.ok:
        raise HTTPException(status_code=502, detail=f"prometheus error: {res.error}")
    raw_alerts = (res.data or {}).get("alerts", [])
    real = [to_canonical_alert(a) for a in raw_alerts]
    real_ids = {a["alert_id"] for a in real}
    synthetic = [
        a for a in _synthetic_alerts_for_active_scenarios() if a["alert_id"] not in real_ids
    ]
    candidates = real + synthetic
    return {
        "count": len(candidates),
        "alerts": candidates,
        "raw_count": len(raw_alerts),
        "synthetic_count": len(synthetic),
    }


@app.post("/api/triage/live", response_model=None)
async def triage_live() -> dict[str, Any]:
    """Fetch firing Prometheus alerts, triage + auto-ticket each in parallel.

    Each ``triage_alert`` call is synchronous (blocking LLM + HTTP), so we wrap
    each in ``asyncio.to_thread`` and gather them. With N firing alerts, total
    latency is ~max-per-alert instead of ~sum.

    Returns ``{"count": N, "results": [{"verdict": ..., "ticket": ...}, ...]}``
    — each entry pairs the RA-001 verdict with RA-003's ticket record.
    """
    payload = live_alerts()
    candidates = payload["alerts"]

    def _triage_one(candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            return triage_alert(TriageRequest(alert=candidate))
        except HTTPException as exc:
            return {"error": exc.detail, "alert": candidate}

    tasks = [asyncio.to_thread(_triage_one, c) for c in candidates]
    results = list(await asyncio.gather(*tasks)) if tasks else []
    return {"count": len(results), "results": results}


# ─── DEMO-AUTO-TRIAGE (#130) ─────────────────────────────────────────────
#
# Background loop that auto-fires the triage pipeline on any alert that
# appears in /api/live-alerts and hasn't been triaged yet. Without this,
# the 6-minute demo path stalls after Inject — alerts sit in /api/live-
# alerts until the presenter manually clicks "Triage". With it, the
# chain (alert → triage → classify → ticket → notify → RCA gate) fires
# automatically.


class _AutoTriageLoop:
    """Periodic poller that triages new alerts as they appear.

    Dedup is by ``alert_id`` against a process-local set. The triage
    agent has its own idempotency window (see
    ``agents.alert_triage._IDEMPOTENCY_WINDOW``), so even if the same
    id slips through, the verdict is suppressed at the agent layer.

    Disabled by default in tests (``AIOPS_AUTO_TRIAGE_ENABLED=false``)
    so the test suite doesn't generate background traffic.
    """

    def __init__(self, interval_seconds: float = 3.0) -> None:
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._seen: set[str] = set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="auto-triage")

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def forget(self, alert_id: str) -> None:
        """Drop an id from the seen set so the next poll re-triages it.

        Hook for ``/api/scenarios/reset-all`` — when a scenario is reset
        and re-injected, we want the new alert to flow through even
        though its id may match the previous one.
        """
        self._seen.discard(alert_id)

    def forget_all(self) -> None:
        self._seen.clear()

    def mark_seen(self, alert_id: str) -> None:
        """Record ``alert_id`` as already handled so the poller skips it.

        Used by the inject endpoint, which triages the injected alert
        directly: marking it seen here stops the background poller (when
        enabled) from triaging the same alert again and emitting a
        duplicate notification.
        """
        if alert_id:
            self._seen.add(alert_id)

    async def _run(self) -> None:
        logger.info("auto-triage loop started (interval=%.1fs)", self._interval)
        while True:
            try:
                payload = await asyncio.to_thread(live_alerts)
                candidates = payload.get("alerts", [])
                fresh = [c for c in candidates if c.get("alert_id") not in self._seen]
                if fresh:
                    for c in fresh:
                        aid = c.get("alert_id")
                        if aid:
                            self._seen.add(aid)
                    tasks = [asyncio.to_thread(self._safe_triage, c) for c in fresh]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    logger.info(
                        "auto-triage processed %d new alert(s); seen=%d",
                        len(fresh),
                        len(self._seen),
                    )
            except asyncio.CancelledError:
                logger.info("auto-triage loop cancelled")
                raise
            except Exception:
                logger.exception("auto-triage loop iteration failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                logger.info("auto-triage loop cancelled")
                return

    @staticmethod
    def _safe_triage(candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            return triage_alert(TriageRequest(alert=candidate))
        except HTTPException as exc:
            logger.warning(
                "auto-triage skipped alert %s: %s",
                candidate.get("alert_id"),
                exc.detail,
            )
            return {"error": exc.detail, "alert": candidate}
        except Exception:
            logger.exception("auto-triage failed on alert %s", candidate.get("alert_id"))
            return {"error": "internal", "alert": candidate}


_AUTO_TRIAGE = _AutoTriageLoop(
    interval_seconds=float(os.environ.get("AIOPS_AUTO_TRIAGE_INTERVAL_SECONDS", "3"))
)


async def _start_auto_triage() -> None:
    enabled = os.environ.get("AIOPS_AUTO_TRIAGE_ENABLED", "true").lower() == "true"
    if not enabled:
        logger.info("auto-triage loop disabled via AIOPS_AUTO_TRIAGE_ENABLED")
        return
    _AUTO_TRIAGE.start()


async def _stop_auto_triage() -> None:
    await _AUTO_TRIAGE.stop()


class RcaRequest(BaseModel):
    triage_verdict: dict[str, Any] = Field(
        ..., description="The RA-001 TriageVerdict dict (as emitted by POST /api/triage)."
    )
    scenario_id: str | None = Field(
        None,
        description=(
            "Optional locked-scenario hint (e.g. 'slow-product-catalog'). When the "
            "LLM provider is unavailable, the agent uses this + the verdict's "
            "affected_service to pick the deterministic fallback verdict. Safe to omit."
        ),
    )
    run_id: str | None = Field(
        None,
        description=(
            "Client-generated UUIDv4 scoping this run's real-time progress stream "
            "(GET /api/rca/stream/{run_id}) and, once seeded, its RCA chat session. "
            "Omitting it reproduces today's behavior byte-for-byte — no stream, no "
            "session, no persisted verdict."
        ),
    )
    incident_id: str | None = Field(
        None,
        description=(
            "When present, the verdict is best-effort persisted (repository.save_rca_result) "
            "so a page reload — or a chat session seeded from this run — can rehydrate it."
        ),
    )


@app.post("/api/rca", response_model=None)
async def rca_endpoint(req: RcaRequest) -> dict[str, Any]:
    """Run the RCA Agent (PRS-008) against a prior triage verdict.

    Body: ``{"triage_verdict": {<TriageVerdict dict>}, "scenario_id"?: str,
    "run_id"?: str, "incident_id"?: str}``. Returns an ``RCAVerdict`` with
    ``root_cause``, ``ranked_fix_steps`` (each with ``blast_radius`` +
    ``rollback``), and ``confidence_score``. Every fix step is tagged
    ``requires_hitl=true``; the platform HITL gate enforces approval at the
    action boundary — this endpoint does NOT execute the fix.

    The agent's LLM call can take 5–15 s (Claude via Foundry); ``rca_analyze``
    is sync + blocking, so we wrap it in ``asyncio.to_thread`` to keep the
    event loop free. When ``run_id`` is supplied, the SAME call also streams
    real stage-progress events to ``GET /api/rca/stream/{run_id}`` — a
    ``HubSink`` bound to that run_id, not a global broadcast, so two
    concurrent RCA runs for different incidents never cross-talk.
    """
    sink = make_sink(req.run_id)
    try:
        verdict = await asyncio.to_thread(
            rca_analyze,
            req.triage_verdict,
            scenario_id=req.scenario_id,
            progress=sink,
            run_id=req.run_id or "",
        )
    except Exception as exc:
        logger.exception(
            "RCA agent raised on payload for %s", req.triage_verdict.get("affected_service")
        )
        if req.run_id:
            _push_terminal_progress(req.run_id, ok=False, detail=str(exc))
        raise HTTPException(status_code=500, detail=f"RCA failed: {exc}") from exc

    payload = verdict.model_dump(mode="json")

    # The RCA Agent now also drives remediation: it presents the operator a ranked
    # set of executable options (formerly the standalone PRS-001 Remediation
    # Recommender), each REQUIRED-HITL-gated, so the RCA surface is the single
    # place a human sees the root cause AND picks + approves the fix. Compose the
    # options here. Best-effort: a remediation failure never blocks the RCA
    # verdict — the UI falls back to rendering ``ranked_fix_steps`` directly.
    try:
        remediation = await asyncio.to_thread(
            remediate,
            RemediationInput.model_validate(
                {"rca_verdict": payload, "triage_verdict": req.triage_verdict}
            ),
        )
        rem = remediation.model_dump(mode="json")
        payload["remediation_options"] = rem.get("options", [])
        payload["recommended_option_id"] = rem.get("recommended_option_id")
    except Exception:
        logger.exception(
            "remediation options failed for %s; returning RCA verdict only",
            payload.get("affected_service"),
        )
        payload["remediation_options"] = []
        payload["recommended_option_id"] = None

    if req.incident_id:
        # Best-effort: a persistence hiccup must cost a rehydration option on
        # a later reload, never this response. Same posture as the
        # remediation-composition try/except immediately above.
        try:
            state_repo.save_rca_result(
                incident_id=req.incident_id,
                verdict=payload,
                affected_service=str(payload.get("affected_service") or ""),
            )
        except Exception:
            logger.exception("save_rca_result failed for incident_id=%s", req.incident_id)

    if req.run_id:
        # Seeds the chat session here — after remediation composition — so
        # its stored verdict matches exactly what this response returns to
        # the UI, not a pre-remediation snapshot.
        try:
            from agents.rca_agent import chat as rca_chat
            from demo.ui.rca_sessions import RcaSession
            from demo.ui.rca_sessions import get_session_store as _get_rca_sessions

            pack = rca_chat.build_grounding_pack(
                verdict, verdict.investigation, verdict.affected_service
            )
            now = datetime.now(UTC)
            _get_rca_sessions().put(
                RcaSession(
                    run_id=req.run_id,
                    created_at=now,
                    last_used_at=now,
                    incident_id=req.incident_id,
                    affected_service=verdict.affected_service,
                    triage_verdict=req.triage_verdict,
                    verdict=payload,
                    investigation=verdict.investigation,
                    grounding_pack=pack,
                )
            )
        except Exception:
            logger.exception("failed to seed chat session for run_id=%s", req.run_id)

    if req.run_id:
        _push_terminal_progress(
            req.run_id,
            ok=True,
            detail="RCA verdict produced",
            root_cause_status=payload.get("root_cause_status"),
            confidence_score=payload.get("confidence_score"),
        )

    return payload


def _push_terminal_progress(run_id: str, *, ok: bool, detail: str, **data: Any) -> None:
    """Push the ``complete``/``failed`` terminal event onto ``run_id``'s stream.

    Fired from here, not from ``agent.py`` — the agent does not know about
    remediation composition or persistence, both of which happen after
    ``rca_analyze`` returns, and the terminal event should reflect the whole
    request, not just the analysis half of it.
    """
    from agents.rca_agent.progress import RcaStage, StageEvent, StageOutcome

    hub = _rca_progress_hub()
    hub.push(
        run_id,
        StageEvent(
            run_id=run_id,
            seq=hub.next_seq(run_id),
            stage=RcaStage.COMPLETE if ok else RcaStage.FAILED,
            outcome=StageOutcome.OK if ok else StageOutcome.FAILED,
            label=detail,
            data=data,
        ).model_dump(mode="json"),
    )


class CorrelateRequest(BaseModel):
    service: str = Field(
        ..., description="Affected service (e.g. 'product-catalog') to correlate signals for."
    )
    window_minutes: int = Field(
        15,
        ge=1,
        le=1440,
        description="Look-back window (minutes, ending now) used when start/end are omitted.",
    )
    start: str | None = Field(None, description="ISO-8601 window start; overrides window_minutes.")
    end: str | None = Field(None, description="ISO-8601 window end; defaults to now (UTC).")
    triage_verdict: dict[str, Any] | None = Field(
        None, description="Optional upstream RA-001 verdict dict — enriches the evidence summary."
    )
    classification: dict[str, Any] | None = Field(
        None, description="Optional upstream RA-002 classification dict."
    )
    topology: dict[str, list[str]] | None = Field(
        None, description="Optional service -> [downstream deps] map for topology-aware suspects."
    )


@app.post("/api/correlate", response_model=None)
async def correlate_endpoint(req: CorrelateRequest) -> dict[str, Any]:
    """Run Log Correlation (RA-007) for a service + time window.

    Body: ``{"service": "product-catalog", "window_minutes"?: 15, "start"?,
    "end"?, "triage_verdict"?, "classification"?, "topology"?}``. Returns a
    ``CorrelationResult`` — the correlated evidence pack (timeline, top
    signatures, suspected components, confidence) plus ``audit_metadata`` whose
    ``signal_source`` is ``live`` when the observability backends (Loki / Jaeger
    / Prometheus) were reachable, or ``synthetic`` when they weren't.

    Unlike the eval-harness ``run()`` shim, this does NOT force the synthetic
    path — it attempts the live fan-out so the dashboard reflects real cluster
    state. The agent is read-only (HITL None) and sync + blocking (parallel
    fan-out + an LLM summary), so we wrap it in ``asyncio.to_thread``.
    """
    end = datetime.fromisoformat(req.end.replace("Z", "+00:00")) if req.end else datetime.now(UTC)
    start = (
        datetime.fromisoformat(req.start.replace("Z", "+00:00"))
        if req.start
        else end - timedelta(minutes=req.window_minutes)
    )
    try:
        payload = CorrelationInput(
            service=req.service,
            window={"start": start.isoformat(), "end": end.isoformat()},
            triage_verdict=req.triage_verdict,
            classification=req.classification,
            topology=req.topology,
        )
        result = await asyncio.to_thread(correlate_signals, payload)
    except Exception as exc:
        logger.exception("Log Correlation raised for %s", req.service)
        raise HTTPException(status_code=500, detail=f"Correlation failed: {exc}") from exc
    return result.model_dump(mode="json")


class IncidentCommandRequest(BaseModel):
    alert: dict[str, Any] = Field(..., description="Canonical Alert payload")
    scenario_id: str | None = Field(
        None,
        description=(
            "Optional locked-scenario hint forwarded to the RCA Agent (e.g. "
            "'slow-product-catalog'). Lets RCA pick its deterministic fallback "
            "verdict when the LLM provider is unavailable. Safe to omit."
        ),
    )


@app.post("/api/incident-commander", response_model=None)
async def incident_commander_endpoint(req: IncidentCommandRequest) -> dict[str, Any]:
    """Run the Incident Commander (RA-008) for a single alert.

    Body: ``{"alert": {<Alert payload>}, "scenario_id"?: str}``. RA-008 chains
    the Reactive-Active flow (RA-001 → RA-002 → RA-003 → RA-005, via the
    orchestrator seam) and — for Sev-1/Sev-2 — runs RCA, posts an IC context
    pack + human-IC handoff through chatops, and seeds a postmortem.

    RA-008 takes no destructive action: RCA fix-step execution stays on the
    separately HITL-gated ``/api/demo/rca/apply-fix`` path. The agent is sync +
    blocking (reactive flow + RCA LLM call), so we wrap it in
    ``asyncio.to_thread`` to keep the event loop free.

    Returns an ``IncidentCommandResult`` dict: ``engaged``, ``severity``,
    ``reactive`` (the ``/api/triage`` bundle), ``rca`` (null below Sev-2),
    ``timeline``, ``postmortem_seed`` (null below Sev-2), ``handoff_requested``.
    """
    try:
        alert_obj = Alert(**req.alert)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid alert: {exc}") from exc
    try:
        result = await asyncio.to_thread(incident_command, alert_obj, scenario_id=req.scenario_id)
    except Exception as exc:
        logger.exception("RA-008 raised on alert for %s", alert_obj.service)
        raise HTTPException(status_code=500, detail=f"incident command failed: {exc}") from exc
    return result.model_dump(mode="json")


@app.get("/api/verdicts")
def list_verdicts_endpoint(
    limit: int = 50,
    service: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Newest-first list of persisted verdicts for the dashboard history view."""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    verdicts = state_repo.list_verdicts(limit=limit, service=service, severity=severity)
    return {"count": len(verdicts), "verdicts": verdicts}


def _runbook_html(title: str, body: str) -> str:
    """Render a runbook as a standalone, readable HTML page. Kept dependency-free
    (no markdown lib): the body is HTML-escaped and shown in a styled <pre> so the
    procedure's formatting, links text, and step numbering survive intact."""
    safe_title = html_escape(title)
    safe_body = html_escape(body)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title>"
        "<style>"
        "body{font:15px/1.6 system-ui,Segoe UI,sans-serif;max-width:820px;"
        "margin:2.5rem auto;padding:0 1.25rem;color:#1c2230;background:#fafafa}"
        "h1{font-size:1.4rem;border-bottom:1px solid #e2e5ea;padding-bottom:.5rem}"
        "pre{white-space:pre-wrap;word-wrap:break-word;background:#fff;border:1px solid #e2e5ea;"
        "border-radius:8px;padding:1.25rem;font:13px/1.55 ui-monospace,Consolas,monospace}"
        "</style></head><body>"
        f"<h1>{safe_title}</h1><pre>{safe_body}</pre></body></html>"
    )


@app.get("/api/runbooks/by-service/{service}", response_class=HTMLResponse)
def get_runbook_by_service(service: str) -> HTMLResponse:
    """Open the runbook for ``service`` from the executor's version-controlled
    library (``agents/runbook_executor/runbooks``), rendered as a readable HTML
    page. This is what the dashboard's verdict "Runbook" link points at —
    replacing the placeholder ``runbooks.example.com`` CMDB URL with the real,
    on-disk procedure (the recommended/auto-selected runbook for the service)."""
    from agents.runbook_executor import Incident, load_runbooks, select

    chosen = select(Incident(incident_id="viewer", service=service))
    if chosen is None:
        # Fall back to any service match so the link still opens something useful.
        svc = _normalize_runbook_service(service)
        chosen = next(
            (rb for rb in load_runbooks() if _normalize_runbook_service(rb.service) == svc), None
        )
    if chosen is None:
        return HTMLResponse(
            _runbook_html(
                f"No runbook for “{service}”",
                f"No executable runbook is published for service '{service}'. Add one "
                "under agents/runbook_executor/runbooks/ (a markdown file with a "
                "steps: block).",
            ),
            status_code=404,
        )
    return HTMLResponse(_runbook_html(chosen.title, chosen.body))


# ─── Prescriptive chain HTTP surface (PRS-001 + PRS-002) ───────────────────
#
# Three endpoints let the demo drive the full Reactive→Prescriptive loop
# without Python imports:
#
#   /api/remediation  — POST RCA verdict + triage context → ranked options
#                       (Remediation Recommender, PRS-001).  No side effects.
#   /api/execute      — POST a single chosen option → real tool dispatch
#                       (Auto-Healer Lite, PRS-002).  Gated by platform
#                       HITL; dry_run defaults True for safety.
#   /api/triage-full  — POST an Alert → chain triage → classify → ticket →
#                       notify → RCA → remediation in one call.  Stops at
#                       the operator's decision point: execution stays a
#                       separate /api/execute call so the human is always
#                       in the loop.


class RemediationHttpRequest(BaseModel):
    rca_verdict: dict[str, Any] = Field(
        ..., description="RCAVerdict-shape dict (root_cause, ranked_fix_steps, …)"
    )
    triage_verdict: dict[str, Any] | None = Field(
        default=None, description="Optional upstream TriageVerdict for incident summary"
    )
    environment: str = Field(
        default="production",
        description="production | staging | dev — influences blast-radius preference",
    )
    operator_preferences: dict[str, Any] = Field(
        default_factory=dict, description="Future v1 hook; only 'prefer_safe' honoured today"
    )


@app.post("/api/remediation", response_model=None)
def remediation_endpoint(req: RemediationHttpRequest) -> dict[str, Any]:
    """Rank remediation options for a diagnosed incident (PRS-001).

    Body: ``{"rca_verdict": {...}, "triage_verdict"?: {...},
    "environment"?: "production", "operator_preferences"?: {...}}``.
    Returns a ``RemediationVerdict`` with ``options`` (sorted, len ≥ 1),
    ``recommended_option_id``, ``confidence_score``, and the audit
    trace. Auto-pick eligibility is hard-False — every option still
    flows through the HITL gate when the operator picks one and POSTs
    to ``/api/execute``.

    No tool dispatch happens here; this endpoint is pure data
    (read-only on the platform tool registry).
    """
    try:
        typed = RemediationInput.model_validate(
            {
                "rca_verdict": req.rca_verdict,
                "triage_verdict": req.triage_verdict,
                "environment": req.environment,
                "operator_preferences": req.operator_preferences,
            }
        )
        verdict = remediate(typed)
    except Exception as exc:
        logger.exception("PRS-001 remediation endpoint raised")
        raise HTTPException(status_code=500, detail=f"remediation failed: {exc}") from exc
    return verdict.model_dump(mode="json")


class ExecuteHttpRequest(BaseModel):
    option: dict[str, Any] = Field(
        ..., description="A single RemediationOption (dict-form) the operator chose."
    )
    incident_id: str | None = None
    affected_service: str = Field(..., min_length=1)
    operator: str | None = Field(
        default=None,
        description="Operator initiating the execute call — recorded in the audit row.",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default): validate + consult the HITL gate but do not call "
            "the tool. False: full execution after gate clears."
        ),
    )
    hitl_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/execute", response_model=None)
def execute_endpoint(req: ExecuteHttpRequest) -> dict[str, Any]:
    """Execute a chosen remediation option through Auto-Healer Lite (PRS-002).

    Body: ``{"option": {<RemediationOption dict>}, "affected_service": "...",
    "incident_id"?: "...", "operator"?: "...", "dry_run"?: bool,
    "hitl_context"?: {...}}``. Returns an ``ExecutionVerdict``
    (status ∈ refused / pending_approval / blocked / dry_run_ok /
    executed / execution_failed), the platform's gate decision, the
    tool result (when status=executed), and the audit trace.

    HITL: the gate action ``auto_heal.lite.execute`` is REQUIRED in
    ``aiops/policy/gate.py:DEFAULT_LEVELS``. With a real approver
    installed (``install_default_approver`` runs at startup) the
    operator's approve/deny click on the /hitl page (or the Slack
    interactive prompt) drives the outcome. Without one, the default
    fail-closed approver blocks every REQUIRED action.

    Persists an ``ExecutionRow`` for every attempt — REFUSED, BLOCKED,
    DRY_RUN_OK, EXECUTED, and EXECUTION_FAILED — so the dashboard
    history view + future historical-effectiveness loop both query
    the same source of truth.
    """
    try:
        typed = ExecutionRequest.model_validate(req.model_dump())
        verdict = auto_heal_execute(typed)
    except Exception as exc:
        logger.exception("PRS-002 execute endpoint raised")
        raise HTTPException(status_code=500, detail=f"execute failed: {exc}") from exc
    return verdict.model_dump(mode="json")


class TriageFullRequest(BaseModel):
    alert: dict[str, Any] = Field(..., description="Canonical Alert payload (RA-001 input)")
    scenario_id: str | None = Field(
        default=None,
        description="Optional scenario id forwarded to RCA for deterministic fallback.",
    )
    environment: str = Field(
        default="production",
        description="Forwarded to PRS-001 for environment-aware ranking.",
    )


@app.post("/api/triage-full", response_model=None)
async def triage_full_endpoint(req: TriageFullRequest) -> dict[str, Any]:
    """Run the full Reactive→Prescriptive chain on one alert.

    Pipeline (each step's output feeds the next):

    1. RA-001 Alert Triage → ``TriageVerdict`` (severity, team, on-call)
    2. RA-002 Incident Classifier → ``Classification`` (incident_type, root cause text)
    3. RA-003 Auto-Ticketing → ``TicketRecord`` (real ServiceNow PDI or mock)
    4. RA-005 Notification Router → ``RoutingDecision`` (chatops fan-out)
    5. PRS-008 RCA Agent → ``RCAVerdict`` (ranked fix steps with rollback)
    6. PRS-001 Remediation Recommender → ``RemediationVerdict`` (ranked options)

    The chain **stops here** — execution (PRS-002) stays a separate
    ``/api/execute`` call so a human is always in the loop. This
    endpoint is the "what to consider" half of the demo; the operator
    picks an option from ``remediation.options`` and POSTs it to
    ``/api/execute``.

    Response shape::

        {
          "verdict": TriageVerdict,
          "classification": Classification,
          "ticket": TicketRecord,
          "notifications": RoutingDecision | null,
          "deliveries": {adapter_name: DeliveryResult} | null,
          "rca": RCAVerdict | null,
          "remediation": RemediationVerdict | null,
          "persisted": {verdict_id, classification_id, notification_id},
          "errors": {step_name: error_string}   # populated when a step soft-failed
        }

    Each prescriptive step (RCA + recommendation) soft-fails: an
    LLM hiccup on RCA leaves ``rca: null`` and ``errors.rca`` set, but
    the reactive half of the chain still returns a usable response.
    """
    # Reactive half — reuse the existing ``triage_alert`` to avoid drift.
    try:
        reactive = triage_alert(TriageRequest(alert=req.alert))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("triage-full: reactive chain raised")
        raise HTTPException(status_code=500, detail=f"reactive chain failed: {exc}") from exc

    errors: dict[str, str] = {}
    verdict_dict = reactive["verdict"]

    # 5. RCA — best-effort. An LLM blip should NOT lose the rest of the chain.
    rca_dict: dict[str, Any] | None = None
    try:
        rca_verdict = await asyncio.to_thread(
            rca_analyze, verdict_dict, scenario_id=req.scenario_id
        )
        rca_dict = rca_verdict.model_dump(mode="json")
    except Exception as exc:
        logger.exception("triage-full: RCA step raised; continuing without it")
        errors["rca"] = f"{type(exc).__name__}: {exc}"

    # 6. Remediation Recommender — only runs if RCA produced a verdict.
    remediation_dict: dict[str, Any] | None = None
    if rca_dict is not None:
        try:
            reco_input = RemediationInput.model_validate(
                {
                    "rca_verdict": rca_dict,
                    "triage_verdict": verdict_dict,
                    "environment": req.environment,
                    "operator_preferences": {},
                }
            )
            reco_verdict = remediate(reco_input)
            remediation_dict = reco_verdict.model_dump(mode="json")
        except Exception as exc:
            logger.exception("triage-full: PRS-001 step raised; continuing without it")
            errors["remediation"] = f"{type(exc).__name__}: {exc}"
    else:
        errors["remediation"] = "skipped because RCA did not produce a verdict"

    return {
        **reactive,
        "rca": rca_dict,
        "remediation": remediation_dict,
        "errors": errors,
    }


# ─── HITL demo agent trigger (issue #77) ───────────────────────────────────
#
# The standalone HITL approval UI at /hitl POSTs here to kick off the
# auto_healer_lite agent in-process.  The agent blocks on the gate's
# approver, which posts an interactive prompt through chatops.  When the
# operator approves/denies via the UI or Slack, the agent thread unblocks
# and the outcome is parked in ``_HITL_OUTCOMES`` for the UI to pick up.
#
# Storing the outcome in-memory (not the SQL state store) because this is
# a demo path — restarts wipe it intentionally so each demo starts clean.


def _uuid_hex() -> str:
    """Short helper so the demo endpoint and tests share one id source."""
    return uuid.uuid4().hex


# ─── HITL-3 (#103): bounded outcome store + pooled agent threads ──────────


class _BoundedOutcomeStore:
    """LRU-evicted store for ``/api/demo/auto-heal/restart`` outcomes.

    The prior implementation was a bare ``dict[str, dict[str, Any]]`` that
    accumulated one entry per request for the lifetime of the process —
    fine for a 10-minute demo, a slow leak for a server left running.
    """

    _MAX_ENTRIES = 100

    def __init__(self) -> None:
        self._data: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Writes happen on pool workers, reads on the FastAPI handler thread.
        self._lock = threading.Lock()

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            while len(self._data) > self._MAX_ENTRIES:
                self._data.popitem(last=False)

    def __getitem__(self, key: str) -> dict[str, Any]:
        with self._lock:
            return self._data[key]

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_HITL_OUTCOMES = _BoundedOutcomeStore()


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """``ThreadPoolExecutor`` whose worker threads are daemons.

    The old per-request ``threading.Thread(daemon=True)`` design let the
    process exit even when an agent was blocked in a long HITL gate wait
    (up to 900s). Stock ``ThreadPoolExecutor`` workers are non-daemon
    and would otherwise block process exit until those waits expire.

    ``threading.Thread.daemon`` is read-only after the thread starts, so
    we can't simply chain ``super()._adjust_thread_count()`` and flip
    the flag afterwards — the stock implementation starts the thread on
    the same line that creates it. Instead we copy the body of
    ``_adjust_thread_count`` and pass ``daemon=True`` at construction.
    Couples us to a CPython internal (``_worker``, ``_threads_queues``)
    that has been stable since 3.7.
    """

    def _adjust_thread_count(self) -> None:
        import weakref
        from concurrent.futures.thread import _threads_queues, _worker

        if self._idle_semaphore.acquire(timeout=0):
            return

        def _weakref_cb(_: Any, q: Any = self._work_queue) -> None:
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, _weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)
            _threads_queues[t] = self._work_queue


def _new_hitl_agent_pool() -> _DaemonThreadPoolExecutor:
    return _DaemonThreadPoolExecutor(
        max_workers=8,
        thread_name_prefix="hitl-demo-",
    )


_HITL_AGENT_POOL: _DaemonThreadPoolExecutor = _new_hitl_agent_pool()


class HitlDemoRestartRequest(BaseModel):
    deployment: str = Field("product-catalog")
    namespace: str = Field("ecommerce")
    reason: str = Field("Demo: agent recommends a restart to clear stuck state.")
    timeout_seconds: int = Field(120, ge=5, le=900)


@app.post("/api/demo/auto-heal/restart")
async def trigger_auto_heal_restart(req: HitlDemoRestartRequest) -> dict[str, Any]:
    """Fire the auto_healer_lite agent in a background thread and return
    immediately with the approval id.

    The agent blocks inside ``recommend_restart`` until the human resolves
    the request — we don't wait for that here (browsers would time out).
    Poll ``/api/demo/auto-heal/outcome/{approval_id}`` for the result.
    """
    from agents.auto_healer_lite import RestartRecommendation, recommend_restart

    rec = RestartRecommendation(
        deployment=req.deployment,
        namespace=req.namespace,
        reason=req.reason,
    )
    # Pre-mint the approval id so we can return it before the gate runs.
    approval_id = _uuid_hex()
    ctx = {"approval_id": approval_id, "approval_timeout_seconds": req.timeout_seconds}

    def _run_agent() -> None:
        outcome = recommend_restart(rec, hitl_context=ctx)
        _HITL_OUTCOMES[approval_id] = outcome.model_dump(mode="json")

    # Bounded pool (HITL-3, #103): a fast-clicking presenter or misbehaving
    # client can't pile up unbounded in-flight threads, each holding a
    # 900s registry-wait.
    _HITL_AGENT_POOL.submit(_run_agent)

    return {
        "approval_id": approval_id,
        "deployment": req.deployment,
        "namespace": req.namespace,
        "status": "pending",
        "timeout_seconds": req.timeout_seconds,
    }


@app.get("/api/demo/auto-heal/outcome/{approval_id}")
def get_auto_heal_outcome(approval_id: str) -> dict[str, Any]:
    """Return the agent's outcome dict once the approval has been resolved.

    Returns ``{"status": "pending"}`` until the agent thread completes,
    *or* once the outcome has been evicted from the bounded LRU store
    (after 100 newer requests). The dashboard polls this after the
    approval flips out of PENDING.
    """
    if approval_id in _HITL_OUTCOMES:
        return _HITL_OUTCOMES[approval_id]
    return {"status": "pending", "approval_id": approval_id}


# ─── Auto-Healer Lite demo (PRS-002): gated, non-blocking execute ──────────
#
# The dashboard's Auto-Healer page POSTs a chosen RemediationOption (from the
# Remediation Recommender, PRS-001) here. Unlike the synchronous /api/execute
# above — which calls gate.enforce() inline and would block the request thread
# for the whole approval window — this mirrors the auto-heal-restart and
# runbook-executor pattern: pre-mint the approval id, fire the agent on a pool
# thread, return immediately, and park the ExecutionVerdict in the shared
# _HITL_OUTCOMES store. Poll /api/demo/auto-heal/outcome/{approval_id} for it.
#
# The gate action ``auto_heal.lite.execute`` is REQUIRED, so the agent blocks
# at the platform HITL gate until a human resolves the approval in /hitl (or
# Slack). dry_run defaults True — the Day-1 stub never fires a real tool.


class HitlDemoExecuteRequest(BaseModel):
    option: dict[str, Any] = Field(
        ..., description="A single RemediationOption (dict-form) the operator chose."
    )
    affected_service: str = Field(..., min_length=1)
    incident_id: str | None = None
    operator: str | None = None
    dry_run: bool = Field(
        default=True,
        description="Day-1 stub forces dry_run; kept here so the contract matches v1.",
    )
    timeout_seconds: int = Field(120, ge=5, le=900)


@app.post("/api/demo/auto-heal/execute")
async def trigger_auto_heal_execute(req: HitlDemoExecuteRequest) -> dict[str, Any]:
    """Fire Auto-Healer Lite (PRS-002 generic path) on a pool thread and return
    the approval id immediately.

    The agent validates the option, then blocks inside ``execute`` at the
    REQUIRED HITL gate until the human resolves the approval. We don't wait for
    that here (browsers would time out). Poll
    ``/api/demo/auto-heal/outcome/{approval_id}`` for the final ExecutionVerdict.
    """
    approval_id = _uuid_hex()
    hitl_ctx = {"approval_id": approval_id, "approval_timeout_seconds": req.timeout_seconds}
    execution_req = ExecutionRequest(
        option=req.option,
        affected_service=req.affected_service,
        incident_id=req.incident_id,
        operator=req.operator,
        dry_run=req.dry_run,
        hitl_context=hitl_ctx,
    )

    def _run_agent() -> None:
        verdict = auto_heal_execute(execution_req)
        _HITL_OUTCOMES[approval_id] = verdict.model_dump(mode="json")

    _HITL_AGENT_POOL.submit(_run_agent)

    return {
        "approval_id": approval_id,
        "status": "pending",
        "option_id": req.option.get("option_id"),
        "affected_service": req.affected_service,
        "dry_run": req.dry_run,
        "timeout_seconds": req.timeout_seconds,
    }


# ─── Runbook Executor demo (RA-004): select → simulate → gated execute ─────
#
# The /agents/runbook-executor dashboard page POSTs here to run the *real*
# Runbook Executor against the mock automation providers. Same HITL shape as
# the auto-heal restart above: fire the agent on a pool thread, return the
# pre-minted approval id immediately, and park the RunbookExecution outcome in
# the shared _HITL_OUTCOMES store (polled via /api/demo/auto-heal/outcome/{id}).
#
# The destructive step routes through the REQUIRED-gated
# automation.runbook.execute capability, so the platform HITL gate creates the
# approval and blocks until a human resolves it in the /hitl approver console.


class HitlDemoRunbookRequest(BaseModel):
    service: str = Field("cart", min_length=1)
    severity: str | None = Field("sev2")
    tags: list[str] = Field(default_factory=list)
    incident_id: str = Field("INC-DEMO-RB")
    # Free-text alert/triage summary — keyword-scanned into symptom tags so a
    # live triaged incident (which carries no explicit tags) still scores
    # against the runbook library.
    summary: str = Field("")
    # Explicit runbook id to run instead of the auto-selected match. Lets the
    # operator override the recommendation from the dashboard's runbook picker.
    # When None, the agent selects by service + tags + severity as before.
    runbook_id: str | None = None
    timeout_seconds: int = Field(120, ge=5, le=900)


# Symptom keyword → runbook tags. The runbook selector matches on service
# first (mandatory) then scores tag overlap, so these only refine the match /
# drive the "matched on" display — service alone already picks the runbook.
_RUNBOOK_TAG_KEYWORDS: dict[str, list[str]] = {
    "latency": ["latency", "load"],
    "slow": ["latency", "load"],
    "p95": ["latency"],
    "saturat": ["load", "saturation"],
    "load": ["load"],
    "oom": ["oom", "crash"],
    "memory": ["oom", "memory"],
    "leak": ["memory"],
    "crash": ["crash", "restart"],
    "restart": ["restart"],
    "loop": ["crashloop"],
    "deploy": ["deploy", "regression"],
    "regression": ["regression"],
    "rollback": ["deploy"],
    "error": ["error"],
    "5xx": ["error"],
    "500": ["error"],
    "fail": ["error"],
    "cpu": ["cpu"],
    "queue": ["queue", "load"],
    "backpressure": ["queue"],
}


def _derive_runbook_tags(summary: str, provided: list[str]) -> list[str]:
    """Merge caller-supplied tags with symptom tags scanned from the summary,
    de-duplicated and order-stable."""
    tags = list(provided)
    blob = (summary or "").lower()
    for needle, mapped in _RUNBOOK_TAG_KEYWORDS.items():
        if needle in blob:
            tags.extend(mapped)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _verify_flag_resolution(runbook: Any, status: str) -> dict[str, Any]:
    """Real verification for the Runbook Executor's ⑤ Verify stage: re-read the
    feature flags the runbook reset and confirm they are now ``off`` — i.e. the
    injected scenario actually cleared. Flag-state based (mirrors the
    resolution-verifier's "re-check the signal" idea) so it works without
    Prometheus. A flag whose seam is unreachable is reported as skipped, not
    failed, so an off-cluster run degrades gracefully."""
    if runbook is None or status != "resolved":
        return {"status": "skipped", "reason": "no resolved runbook to verify", "checks": []}
    flags = [
        s.target.split("/", 1)[1].strip()
        for s in runbook.steps
        if s.action in ("clear_fault", "reset_feature_flag")
        and (s.target or "").startswith(("fault/", "flag/"))
    ]
    flags = [f for f in flags if f]
    if not flags:
        return {"status": "skipped", "reason": "runbook clears no injected fault", "checks": []}

    checks: list[dict[str, Any]] = []
    failures = skips = 0
    for flag in flags:
        variant: Any = None
        available = True
        try:
            # Was feature_flags.get_variant; flagd is gone. Read the fault
            # state back from the cluster instead — "off" still means resolved.
            variants = _live_variants()
            if flag in variants:
                variant = variants[flag]
            else:
                available = False
        except Exception:
            available = False
        ok = available and variant == "off"
        if not available:
            skips += 1
        elif not ok:
            failures += 1
        checks.append(
            {
                "name": f"flag {flag} is off",
                "flag": flag,
                "variant": variant,
                "ok": ok,
                "available": available,
            }
        )

    if failures:
        st = "unverified"
    elif skips and skips == len(checks):
        st = "skipped"  # seam unreachable (off-cluster) — couldn't confirm
    else:
        st = "verified"
    return {"status": st, "checks": checks}


@app.post("/api/demo/runbook-executor/run")
async def trigger_runbook_executor(req: HitlDemoRunbookRequest) -> dict[str, Any]:
    """Kick off the Runbook Executor and return the approval id immediately.

    Runbook selection and the read-only dry-run preview are cheap and
    synchronous, so we do them here and return them up-front: the dashboard
    renders the selected runbook, the match criteria, the planned steps, and the
    per-step dry-run results before the gated execution finishes. The agent then
    runs on a pool thread; the destructive step blocks at the HITL gate until a
    human resolves the approval in /hitl. Poll
    ``/api/demo/auto-heal/outcome/{approval_id}`` for the final RunbookExecution.
    """
    from agents.runbook_executor import Incident, execute_runbook, run_plan, select
    from agents.runbook_executor.library import get_runbook

    tags = _derive_runbook_tags(req.summary, req.tags)
    incident = Incident(
        incident_id=req.incident_id,
        service=req.service,
        severity=req.severity,
        tags=tags,
    )
    # Operator override: run the explicitly chosen runbook (from the picker)
    # instead of the auto-selected match. Falls back to selection when the id
    # is unknown so a stale picker can't wedge the run.
    overridden = bool(req.runbook_id) and get_runbook(req.runbook_id) is not None
    runbook = get_runbook(req.runbook_id) if overridden else select(incident)

    # Read-only dry-run preview per step (NONE-level simulate capability — never
    # gated, makes no changes). Surfaces stage-2 results before the gated run.
    registry = get_registry()
    planned_steps: list[dict[str, Any]] = []
    for s in runbook.steps if runbook else []:
        try:
            sim = registry.call(
                "automation.runbook.simulate",
                step=s.name,
                target=s.target or req.service,
                namespace=s.namespace,
                action=s.action,
            )
            simulate = sim.data if sim.ok else {"error": sim.error}
        except Exception as exc:  # pragma: no cover - defensive
            simulate = {"error": f"{type(exc).__name__}: {exc}"}
        planned_steps.append(
            {"name": s.name, "action": s.action, "destructive": s.destructive, "simulate": simulate}
        )

    approval_id = _uuid_hex()
    ctx = {"approval_id": approval_id, "approval_timeout_seconds": req.timeout_seconds}

    def _run_agent() -> None:
        # When the operator picked a specific runbook, run it directly so the
        # executor doesn't re-select a different match; otherwise use the normal
        # select-then-run entry point.
        if overridden and runbook is not None:
            execution = run_plan(incident, runbook, hitl_context=ctx)
        else:
            execution = execute_runbook(incident, hitl_context=ctx)
        out = execution.model_dump(mode="json")
        # Computed properties don't serialize — flatten them for the UI.
        out["steps_total"] = execution.steps_total
        out["steps_executed"] = execution.steps_executed
        out["destructive_steps"] = execution.destructive_steps
        # Real post-run verification: re-read the flags the runbook reset and
        # confirm the injected scenario actually cleared (⑤ Verify stage).
        out["verification"] = _verify_flag_resolution(runbook, execution.status)
        _HITL_OUTCOMES[approval_id] = out

    _HITL_AGENT_POOL.submit(_run_agent)

    return {
        "approval_id": approval_id,
        "status": "pending" if runbook is not None else "no_runbook",
        "service": req.service,
        "incident_id": req.incident_id,
        "selected_runbook": runbook.id if runbook else None,
        "runbook_title": runbook.title if runbook else None,
        "matched_on": {"service": req.service, "severity": req.severity, "tags": tags},
        "overridden": overridden,
        "planned_steps": planned_steps,
        "timeout_seconds": req.timeout_seconds,
    }


@app.get("/api/runbook-executor/runbooks")
def list_runbook_executor_runbooks(
    service: str | None = None,
    severity: str | None = None,
    summary: str = "",
) -> dict[str, Any]:
    """Available runbooks for the picker. Returns every runbook in the library
    with its steps (so the operator can review them) plus, when ``service`` is
    given, which one the agent would auto-select — so the UI can mark the
    recommendation and let the operator choose a different one. Runbooks whose
    service matches are listed first."""
    from agents.runbook_executor import Incident, load_runbooks, select

    recommended_id: str | None = None
    if service:
        tags = _derive_runbook_tags(summary, [])
        chosen = select(
            Incident(incident_id="picker", service=service, severity=severity, tags=tags)
        )
        recommended_id = chosen.id if chosen else None

    svc = (service or "").lower()
    items = []
    for rb in load_runbooks():
        matches_service = bool(svc) and _normalize_runbook_service(
            rb.service
        ) == _normalize_runbook_service(svc)
        # Relevance: when a service is given, only surface runbooks for THAT
        # service (the recommendation + same-service alternatives). Without a
        # service filter (general library view) show everything.
        if svc and not matches_service:
            continue
        items.append(
            {
                "id": rb.id,
                "title": rb.title,
                "service": rb.service,
                "severity": rb.severity,
                "tags": rb.tags,
                "matches_service": matches_service,
                "recommended": rb.id == recommended_id,
                "steps": [
                    {"name": s.name, "action": s.action, "destructive": s.destructive}
                    for s in rb.steps
                ],
            }
        )
    # Recommended first within the relevant set.
    items.sort(key=lambda r: (not r["recommended"], r["id"]))
    return {"count": len(items), "recommended": recommended_id, "runbooks": items}


def _normalize_runbook_service(service: str) -> str:
    """Normalize a service name for runbook matching — mirrors the executor's
    selector so 'product-catalog' / 'productcatalogservice' compare equal."""
    s = (service or "").lower().strip()
    for suffix in ("service",):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s.replace("-", "").replace("_", "")


# ─── RCA fix-step remediation (RCA → approve → apply) ──────────────────────
#
# The RCA panel's "Approve & apply" button POSTs here. Same shape as the
# auto-heal restart above: fire the gated executor on a background thread,
# return the approval id immediately, and park the outcome in the shared
# _HITL_OUTCOMES store (polled via /api/demo/auto-heal/outcome/{id}).
#
# The executor calls the REQUIRED-gated rca.fix_step.execute capability, so the
# platform HITL gate posts the Slack approve/deny prompt and blocks until a
# human resolves it — then flips the flag through the feature_flags seam.


class RcaApplyFixRequest(BaseModel):
    flag: str = Field(..., min_length=1, description="flagd flag to flip (e.g. paymentFailure)")
    variant: str = Field("off", description="Target defaultVariant — 'off' disables the failure")
    action_type: str = Field(
        "set_flag",
        description="The RCA fix step's action_type. v0 only executes 'set_flag'.",
    )
    reason: str = Field("RCA-recommended remediation: disable the injected failure flag.")
    timeout_seconds: int = Field(120, ge=5, le=900)
    # ── Resolution-verifier context (all optional + additive). When present,
    # the RCA verdict is stored keyed by incident_id (so the SNOW watcher can
    # attach RCA later) and the verifier is fired after a successful apply.
    incident_id: str | None = Field(
        None, description="ServiceNow incident number (e.g. INC0010502)"
    )
    service: str | None = Field(None, description="Affected service")
    alert_signature: str | None = Field(
        None, description="PromQL/expr for the triggering signature"
    )
    metric_query: str | None = Field(None, description="PromQL for the key service metric")
    threshold: float | None = Field(None, description="Normal-range threshold for the metric")
    health_query: str | None = Field(None, description="PromQL for service health (e.g. up{...})")
    rca_verdict: dict[str, Any] | None = Field(
        None, description="Full RCA verdict to persist by incident"
    )


def _live_variants() -> dict[str, str]:
    """Current fault state as ``{failure_key: "on"|"off"}``.

    Keyed by failure key rather than scenario id because every caller here
    reasons in terms of the RCA verdict's ``flag`` field, which carries the
    failure key. Replaces the flagd variant map; returns {} if the cluster is
    unreachable so callers fail open exactly as before.
    """
    try:
        by_scenario = scenario_provider.active_state(SCENARIOS)
    except Exception:
        return {}
    return {
        str(s["flag"]): by_scenario.get(sid, "off") for sid, s in SCENARIOS.items() if s.get("flag")
    }


def _norm_service(service: str | None) -> str:
    s = (service or "").lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def _resolve_apply_flag(
    requested_flag: str | None, service: str | None
) -> tuple[str | None, str | None]:
    """Resolve the flag an RCA apply should ACTUALLY flip.

    The LLM sometimes emits a flag that isn't in flagd (e.g. 'emailGatewayProvider'
    for 'email', whose real flag is 'emailMemoryLeak'), and cached verdicts carry
    stale flags. Rather than fail with FlagNotFound — or flip the wrong flag on a
    service with several scenarios (payment: paymentFailure vs paymentUnreachable) —
    resolve to the flag CURRENTLY FIRING for the affected service, using the
    scenario catalog (SCENARIOS) as the source of truth. Returns
    ``(flag_to_flip, trace_note)``. Fails open: with flagd unreachable it returns
    the requested flag unchanged.
    """
    variants = _live_variants()
    if not variants:
        return requested_flag, None
    req = (requested_flag or "").strip() or None
    # Requested flag is real AND currently firing → honor it.
    if req and variants.get(req, "off") != "off":
        return req, None
    # Otherwise flip whatever flag is firing for this service.
    norm = _norm_service(service)
    firing = [
        s["flag"]
        for s in SCENARIOS.values()
        if _norm_service(s.get("service")) == norm and variants.get(s["flag"], "off") != "off"
    ]
    if firing:
        chosen = firing[0]
        if chosen != req:
            return chosen, (
                f"requested flag {req!r} is not the active failure for service "
                f"{service!r}; flipping the firing flag {chosen!r} instead"
            )
        return chosen, None
    # Nothing firing for this service. Keep a real (already-off) flag for an
    # idempotent no-op; else pass through so the executor reports cleanly.
    if req and req in variants:
        return req, None
    return req, f"no active failure flag for service {service!r} (requested {req!r})"


@app.post("/api/demo/rca/apply-fix")
async def trigger_rca_apply_fix(req: RcaApplyFixRequest) -> dict[str, Any]:
    """Kick off the gated RCA fix-step remediation and return the approval id.

    The executor follows the fix step's ``action_type`` (v0 runs ``set_flag``;
    other types come back ``unsupported``). It blocks inside the HITL gate
    until the human resolves the Slack/dashboard approval; we don't wait for
    that here. Poll ``/api/demo/auto-heal/outcome/{approval_id}`` for the
    result.
    """
    from aiops.tools.rca_remediation import request_fix_step

    approval_id = _uuid_hex()
    # Resolve the flag to the one actually FIRING for this service — the LLM can
    # emit a non-existent flag (e.g. 'emailGatewayProvider'), and cached verdicts
    # carry stale ones. This makes apply flip the REAL failure regardless of what
    # the verdict guessed, and picks the right flag on multi-scenario services.
    resolved_flag = req.flag
    if req.action_type == "set_flag":
        resolved_flag, resolve_note = _resolve_apply_flag(req.flag, req.service)
        if resolve_note:
            logger.info("apply-fix flag resolution (service=%s): %s", req.service, resolve_note)
    ctx: dict[str, Any] = {
        "approval_id": approval_id,
        "approval_timeout_seconds": req.timeout_seconds,
        "reason": req.reason,
        "action_type": req.action_type,
        "flag": resolved_flag,
        "variant": req.variant,
    }

    def _run_executor() -> None:
        outcome = request_fix_step(
            action_type=req.action_type,
            flag=resolved_flag,
            variant=req.variant,
            hitl_context=ctx,
        )
        _HITL_OUTCOMES[approval_id] = outcome
        if isinstance(outcome, dict) and outcome.get("status") == "executed":
            # No propagation kick needed any more. The old flagd path wrote a
            # ConfigMap the running pod would not re-read for ~60-120s, so it had
            # to force a rollout. Clearing an ecommerce fault mutates the pod
            # template directly, which rolls immediately.
            #
            # Fire-and-forget resolution verification after a successful apply.
            # Wrapped so it can never affect the fix-apply outcome (CLAUDE.md:
            # the verifier is fully decoupled).
            try:
                _post_fix_verify(req)
            except Exception:
                logger.exception("post-fix verification trigger failed (non-fatal)")

    _HITL_AGENT_POOL.submit(_run_executor)

    return {
        "approval_id": approval_id,
        "action_type": req.action_type,
        "flag": resolved_flag,
        "variant": req.variant,
        "status": "pending",
        "timeout_seconds": req.timeout_seconds,
    }


def _post_fix_verify(req: RcaApplyFixRequest) -> None:
    """Persist the RCA verdict by incident id and fire the resolution verifier.

    Additive + decoupled: imports the verifier lazily, needs an incident id to
    do anything, and the caller already wraps this so it can't affect the
    fix-apply outcome."""
    incident_id = (req.incident_id or "").strip()
    if not incident_id and req.service:
        # The UI may have no incident number when the analyzed verdict was a
        # Suppressed duplicate (dedup → no ticket on that triage). Fall back to
        # the most recent incident RA-003 opened for this service — e.g. the one
        # the Inject button's background triage created — so the close gate (2nd
        # HITL approval) still fires.
        incident_id = _latest_incident_for_service(req.service)
        if incident_id:
            logger.info(
                "post-fix verify: UI sent no incident_id; recovered %s for service=%r",
                incident_id,
                req.service,
            )
    if not incident_id:
        return  # no ServiceNow ticket to verify/close against
    service = req.service or (req.rca_verdict or {}).get("affected_service") or "unknown"
    if req.rca_verdict:
        with contextlib.suppress(Exception):
            from aiops.state import repository as _repo

            _repo.save_rca_result(
                incident_id=incident_id, verdict=req.rca_verdict, affected_service=service
            )
    from agents.resolution_verifier.verifier import VerifyContext, trigger

    trigger(
        VerifyContext(
            incident_id=incident_id,
            service=service,
            alert_signature=req.alert_signature or "",
            metric_query=req.metric_query or "",
            threshold=req.threshold,
            health_query=req.health_query or "",
        )
    )


@app.get("/api/rca/verify-status/{incident_id}")
def rca_verify_status(incident_id: str) -> dict[str, Any]:
    """Poll target for the Incident Workspace's "Verifying" lifecycle stage.

    Read-only, side-effect-free. ``status`` is one of ``not_triggered``
    (nothing to check yet — apply-fix never fired a verification, or this
    incident has no ServiceNow ticket to key it by), ``in_progress`` (the
    resolution_verifier's stabilization windows are still running — up to a
    few minutes, see VerifyContext), or the completed run's own verdict
    (``pass`` / ``fail`` / ...).
    """
    from agents.resolution_verifier.verifier import get_status

    return get_status(incident_id)


# ─── HITL approval surface (issue #77) ─────────────────────────────────────
#
# Two callback paths land here:
#   1. Slack interactivity → POST /api/approvals/slack/callback (signed)
#   2. Web dashboard → POST /api/approvals/{id}/approve|deny  (session id)
# Both resolve the pending request via aiops.policy.get_approval_registry()
# which unblocks the agent thread waiting inside ToolRegistry.call().


class ApprovalDecisionRequest(BaseModel):
    """Body for the web dashboard's approve/deny actions."""

    approver: str = Field(..., min_length=1, description="Approver identity")
    reason: str = Field("", description="Optional free-text justification")


def _is_same_origin_console(request: Request) -> bool:
    """True when the request looks like it came from this app's own console.

    The dashboard and the /hitl approver are served same-origin by this very
    FastAPI app, so a browser approve/deny carries an ``Origin`` (or
    ``Referer``) whose host equals the request's own ``Host``. We use that as
    the signal to authorize the served console without a bearer token.

    This is a *convenience for the browser console*, not a hardened boundary:
    ``Origin``/``Referer`` are trivially forgeable by a non-browser client, so
    a determined caller could set a matching header. Cross-origin/programmatic
    callers that send no matching header still must present the bearer token.
    A hardened deployment should replace this with a server-set HttpOnly
    session cookie or OPA identity.
    """
    from urllib.parse import urlparse

    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return False
    netloc = urlparse(source).netloc.strip().lower()
    return bool(netloc) and netloc == host


def _require_approval_token(request: Request) -> None:
    """Authenticate the web approve/deny endpoints against
    ``AIOPS_HITL_APPROVAL_TOKEN``.

    Phase 1 of HITL-2 (#102): a lightweight shared-secret bearer-token
    check.  When the env var is **unset** we accept every request so the
    current localhost-only demo flow keeps working (the startup hook
    above logs a loud warning).  When **set**, the same-origin browser
    console (dashboard / hitl-ui) is authorized automatically — see
    :func:`_is_same_origin_console` and its limitations — while every other
    caller must present ``Authorization: Bearer <token>``, compared with
    ``hmac.compare_digest`` for constant-time matching.

    All failure modes raise the *same* 401 with the *same* detail so a
    prober can't tell a missing header from a wrong token.  Phase 2
    will replace this with OPA-gated identity verification once
    ``policies/hitl.rego`` is wired up.

    The env var is read on every call (not captured at import) so the
    operator can rotate the token without restarting the server, and
    tests can set it per-test without module reloads — matching the
    pattern used by ``_verify_slack_signature``.
    """
    token = os.environ.get("AIOPS_HITL_APPROVAL_TOKEN", "").strip()
    if not token:
        return
    # Same-origin browser console (dashboard / hitl-ui) is authorized without a
    # bearer token so the operator never pastes the secret into the UI. See
    # _is_same_origin_console for the (deliberate) limitation.
    if _is_same_origin_console(request):
        return
    auth = request.headers.get("authorization", "")
    presented = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="invalid approval token")


@app.get("/api/approvals")
def list_approvals(include_resolved: bool = False) -> dict[str, Any]:
    """List pending HITL approval requests (or every request when
    ``include_resolved=true``).  Used by the dashboard's notifications panel
    to render the approve/deny buttons."""
    reg = get_approval_registry()
    requests = reg.list_all() if include_resolved else reg.list_pending()
    return {
        "count": len(requests),
        "approvals": [r.to_record() for r in requests],
    }


@app.get("/api/approvals/{approval_id}")
def get_approval(approval_id: str) -> dict[str, Any]:
    try:
        req = get_approval_registry().get(approval_id)
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return req.to_record()


@app.post("/api/approvals/{approval_id}/approve")
def approve_request(
    approval_id: str,
    body: ApprovalDecisionRequest,
    _auth: None = Depends(_require_approval_token),
) -> dict[str, Any]:
    try:
        req = get_approval_registry().decide(
            approval_id,
            approved=True,
            approver=body.approver,
            reason=body.reason,
        )
    except ApprovalError as exc:
        # The registry distinguishes unknown id (truly 404) from already-
        # decided (409 conflict).  Read the message rather than introducing
        # a typed exception hierarchy for a single distinction.
        status = 404 if "unknown" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return req.to_record()


@app.post("/api/approvals/{approval_id}/deny")
def deny_request(
    approval_id: str,
    body: ApprovalDecisionRequest,
    _auth: None = Depends(_require_approval_token),
) -> dict[str, Any]:
    try:
        req = get_approval_registry().decide(
            approval_id,
            approved=False,
            approver=body.approver,
            reason=body.reason or "denied via web",
        )
    except ApprovalError as exc:
        status = 404 if "unknown" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return req.to_record()


# Slack's recommended replay window: reject requests >5 min old.
_SLACK_SIG_MAX_AGE_SECONDS = 60 * 5


def _verify_slack_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Validate the Slack signing-secret HMAC.

    The signing secret is read on every call (not captured at import) so the
    operator can rotate ``AIOPS_SLACK_SIGNING_SECRET`` without restarting the
    server, and tests can set it per-test without module reloads.

    Returns False (rather than raising) for *every* failure mode so the
    callback always returns the same 401 to remote clients regardless of
    whether the failure was a stale timestamp, a missing secret, or a
    mismatched HMAC — denying probers a side channel.
    """
    import hashlib
    import hmac
    import time

    secret = os.environ.get("AIOPS_SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > _SLACK_SIG_MAX_AGE_SECONDS:
        return False
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


@app.post("/api/approvals/slack/callback")
async def slack_interactivity_callback(request: Request) -> dict[str, Any]:
    """Slack Interactivity → approve/deny decision.

    Slack POSTs ``payload=<urlencoded-json>``.  We validate the signing-secret
    HMAC, parse the action, look up the approval, and call ``decide()``.
    The waiting tool-call thread is unblocked by the registry.
    """
    raw = await request.body()
    sig = request.headers.get("x-slack-signature", "")
    ts = request.headers.get("x-slack-request-timestamp", "")
    if not _verify_slack_signature(ts, raw, sig):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    # Slack sends ``payload=<json>`` URL-encoded.  Parse without bringing in
    # a multipart dep — the body is just form-urlencoded with one key.
    from urllib.parse import parse_qs

    form = parse_qs(raw.decode("utf-8"))
    payloads = form.get("payload") or []
    if not payloads:
        raise HTTPException(status_code=400, detail="missing payload")
    try:
        payload = json.loads(payloads[0])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"bad payload json: {exc}") from exc

    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(status_code=400, detail="no actions in payload")
    action = actions[0]
    value = str(action.get("value", ""))
    if "|" not in value:
        raise HTTPException(status_code=400, detail="malformed action value")
    approval_id, verdict = value.split("|", 1)
    approver = (
        (payload.get("user") or {}).get("username")
        or (payload.get("user") or {}).get("id")
        or "slack-user"
    )

    try:
        req = get_approval_registry().decide(
            approval_id,
            approved=(verdict == "approve"),
            approver=f"slack:{approver}",
            reason=f"via Slack action {action.get('action_id')}",
        )
    except ApprovalError as exc:
        status = 404 if "unknown" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    # Slack replaces the original message if we respond with one — keep the
    # response short and to the point so the channel history stays readable.
    return {
        "response_type": "ephemeral",
        "replace_original": False,
        "text": (
            f":white_check_mark: Recorded {req.status.value} for `{req.action}` by {req.approver}"
        ),
    }


@app.get("/api/notifications")
def list_notifications_endpoint(
    limit: int = 50,
    service: str | None = None,
) -> dict[str, Any]:
    """Newest-first list of persisted RA-005 notifications.

    CHAT-2 (#82): SQL counterpart to ``demo/audit/chatops.jsonl`` so the
    dashboard's history view can query "notifications by service over the
    last week" without re-parsing JSONL.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    rows = state_repo.list_notifications(limit=limit, service=service)
    return {"count": len(rows), "notifications": rows}


# ─── RA-002 Incident Classifier surface (standalone dashboard) ─────────────
#
# Independent endpoints / page so RA-002 reads as its own product. The
# accuracy metric on the dashboard is the eval-harness pass rate (cached
# in-memory below). "Misroute" = an eval case whose ``incident_type`` check
# failed, i.e. the classifier put the incident in the wrong category.

_LAST_EVAL: dict[str, Any] | None = None
_EVAL_RUNNING: bool = False


@app.get("/api/classifier/classifications")
def list_classifications_endpoint(
    limit: int = 50,
    incident_type: str | None = None,
) -> dict[str, Any]:
    """Newest-first list of RA-002 classifications for the dashboard table."""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    rows = state_repo.list_classifications(limit=limit, incident_type=incident_type)
    return {"count": len(rows), "classifications": rows}


@app.get("/api/classifier/metrics")
def classifier_metrics() -> dict[str, Any]:
    """Aggregate metrics for the RA-002 dashboard.

    ``eval`` is the cached result of the last harness run (accuracy %,
    misroute rate). ``live`` reflects the persisted-classifications store.
    Returns ``eval=None`` if no eval has run in this server's lifetime.
    """
    avg_conf = state_repo.average_classification_confidence()
    return {
        "eval": _LAST_EVAL,
        "live": {
            "total_classifications": state_repo.count_classifications(),
            "avg_confidence": avg_conf,
        },
        "running": _EVAL_RUNNING,
        "llm_provider": os.environ.get("AIOPS_LLM_PROVIDER"),
        "checked_at": datetime.now(UTC).isoformat(),
    }


@app.post("/api/classifier/evaluate")
async def classifier_evaluate() -> dict[str, Any]:
    """Re-run the eval harness for RA-002 and cache the result. Slow
    (~1-2 min with a real LLM, ~10 s with the stub). Returns the new
    metric block so the UI doesn't need a second round-trip."""
    global _LAST_EVAL, _EVAL_RUNNING
    if _EVAL_RUNNING:
        raise HTTPException(status_code=409, detail="eval already running")
    _EVAL_RUNNING = True
    try:
        # Run in a thread so we don't block the event loop while the LLM
        # is being queried 5x back-to-back.
        from evals.harness import REPO_ROOT, run_agent

        # Classification now lives inside the one Alert Triage agent; its golden
        # set mixes triage-only cases with cases that also assert a
        # classification (incident_type). Only the latter are meaningful for the
        # classifier accuracy / misroute metrics, so filter to cases that carry
        # an ``incident_type`` check.
        agent_dir = REPO_ROOT / "agents" / "alert_triage"
        run = await asyncio.to_thread(run_agent, agent_dir)

        misroute = 0
        per_case: list[dict[str, Any]] = []
        classification_results = []
        for r in run.results:
            type_check = next(
                (c for c in r.details.get("checks", []) if c["check"] == "incident_type"),
                None,
            )
            if type_check is None:
                continue  # triage-only case — not a classification eval
            classification_results.append(r)
            type_ok = bool(type_check["passed"])
            if not type_ok:
                misroute += 1
            per_case.append(
                {
                    "case_id": r.case_id,
                    "passed": r.passed,
                    "incident_type_ok": type_ok,
                    "duration_ms": r.duration_ms,
                    "checks": r.details.get("checks", []),
                }
            )

        total = len(classification_results)
        passed = sum(1 for r in classification_results if r.passed)
        _LAST_EVAL = {
            "total_cases": total,
            "passed_cases": passed,
            "accuracy_pct": (passed / total * 100) if total else 0.0,
            "misroute_cases": misroute,
            "misroute_pct": (misroute / total * 100) if total else 0.0,
            "ran_at": datetime.now(UTC).isoformat(),
            "per_case": per_case,
        }
        return classifier_metrics()
    finally:
        _EVAL_RUNNING = False


# ─── Alert Triage combined surface (triage + classification, one agent) ─────
#
# The Alert Triage agent runs the full 8-step triage workflow then classifies
# the incident, returning both results. This backs the Alert Triage console UI
# (demo/combined-ui, served at /combined) whose sidebar exposes the triage
# verdict and the incident-classification views. Read-only: opens no ticket and
# pages no one.

COMBINED_FIXTURES_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "alert_triage" / "evals" / "golden.json"
)


@app.get("/api/combined/fixtures")
def combined_fixtures() -> dict[str, Any]:
    """Return the combined agent's golden fixtures for the UI's picker."""
    if not COMBINED_FIXTURES_PATH.exists():
        raise HTTPException(
            status_code=500, detail=f"fixtures file not found: {COMBINED_FIXTURES_PATH}"
        )
    with COMBINED_FIXTURES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


class CombinedRunRequest(BaseModel):
    alert: dict[str, Any] = Field(..., description="Canonical Alert payload (RA-001 input)")


@app.post("/api/combined/run", response_model=None)
async def combined_run(req: CombinedRunRequest) -> dict[str, Any]:
    """Run the Alert Triage agent (triage → classification) on one alert.

    Body: ``{"alert": {<Alert payload>}}``. Returns a ``CombinedResult`` dict:
    ``{alert_id, affected_service, verdict: TriageVerdict,
    classification: Classification, verdict_id}``. The agent is sync + blocking
    (embedding search + up to two LLM calls), so it runs in a worker thread to
    keep the event loop free."""
    try:
        alert_obj = Alert(**req.alert)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid alert: {exc}") from exc
    try:
        result = await asyncio.to_thread(triage_and_classify, alert_obj)
    except Exception as exc:
        logger.exception("Alert Triage agent raised on alert for %s", alert_obj.service)
        raise HTTPException(status_code=500, detail=f"combined run failed: {exc}") from exc
    return result.model_dump(mode="json")


# ─── RA-006 War-Room Assembler surface (standalone dashboard) ──────────────
#
# Independent endpoints / page so RA-006 reads as its own product.
#   - ``/api/war-room/assemble`` is the *try-it inspector*: build a verdict
#     from simple form fields and run ``decide`` (pure — no chatops emit) so
#     you can see exactly how RA-006 reacts to any severity / status without
#     touching the live pipeline.
#   - ``/api/war-room/recent`` is the *live feed*: assemblies produced by the
#     real ``/api/triage`` pipeline, newest first, from an in-memory ring
#     buffer (RA-006 has no DB table — the demo doesn't need durable history).

_RECENT_WAR_ROOMS: deque[dict[str, Any]] = deque(maxlen=100)
_WAR_ROOM_SEQ = 0

# War-room lifecycle the dashboard board advances through:
# open → in_call → call_ended → resolved. ``no_room`` is the terminal state
# for minor/suppressed verdicts (no room was opened).
WAR_ROOM_STATUSES = ("open", "in_call", "call_ended", "resolved", "no_room")


def _record_war_room(
    assembly: dict[str, Any],
    verdict: TriageVerdict,
    notification: dict[str, Any] | None = None,
) -> str:
    """Append a compact feed row for one incident and return its feed id.

    Each row carries a lifecycle ``status`` the board can advance:
    ``open`` (room created, responders gathering) → ``in_call`` (live huddle)
    → ``resolved``. Non-assembled verdicts land terminal as ``no_room``.

    ``notification`` is the RA-005+006 ``RoutingDecision`` dict for this
    incident: now that notification + war room are one agent, the feed row
    carries the routed notification (channel, response mode, body) alongside
    the war-room assembly so the combined dashboard renders the whole incident
    — the one message *and* the room — from a single row. Best-effort — never
    raises into the triage pipeline."""
    global _WAR_ROOM_SEQ
    _WAR_ROOM_SEQ += 1
    wid = str(_WAR_ROOM_SEQ)
    assembled = assembly.get("assembled", False)
    # Seed each invited SME's attendance so the board can track who actually
    # joined the bridge. Starts at "invited"; an operator marks joined/declined.
    for person in assembly.get("invited", []):
        person.setdefault("attendance", "invited")
    _RECENT_WAR_ROOMS.append(
        {
            "id": wid,
            "status": "open" if assembled else "no_room",
            "assembled": assembled,
            "channel": assembly.get("channel"),
            "severity": verdict.severity,
            "chat_severity": assembly.get("chat_severity"),
            "service": verdict.affected_service,
            "team": verdict.assigned_team,
            "sme_count": len(assembly.get("invited", [])),
            "reason": assembly.get("reason"),
            "bridge_url": assembly.get("bridge_url"),
            "bridge_status": assembly.get("bridge_status"),
            "assembled_at": assembly.get("assembled_at"),
            "assembly": assembly,
            # The single notification this incident produced (None for the
            # try-it inspector, which previews the war room in isolation).
            "notification": notification,
        }
    )
    return wid


class WarRoomTryRequest(BaseModel):
    """Try-it inspector input — simple fields the dashboard form collects.

    Deliberately not a full ``TriageVerdict``: the operator picks a severity
    and service and we synthesize the rest, so the page is about *RA-006's*
    behaviour, not about reproducing the whole upstream triage."""

    affected_service: str = Field(default="payment")
    severity: str = Field(default="Sev-1")
    assigned_team: str = Field(default="Payments Team")
    assigned_engineer: str | None = Field(default="oncall@payments.example.com")
    alert_summary: str | None = None
    recommended_runbook: str | None = None
    status: str = Field(default="Active")
    incident_id: str | None = None
    create_bridge: bool = Field(default=False)
    """Safe by default: a pure ``decide`` preview with NO side effects, so
    clicking *try-it* on the dashboard never touches the live workspace. Set
    ``create_bridge=true`` to explicitly opt in to actually standing up the
    Slack war room (``assemble`` — creates the channel, invites SMEs, returns a
    join link)."""


@app.post("/api/war-room/assemble")
def war_room_assemble_endpoint(req: WarRoomTryRequest) -> dict[str, Any]:
    """Try-it inspector: synthesize a verdict and run RA-006. Safe by default —
    a pure ``decide`` preview with no side effects. Pass ``create_bridge=true``
    to explicitly opt in to creating the real Slack war room and returning the
    join link."""
    if req.severity not in ("Sev-1", "Sev-2", "Sev-3", "Sev-4"):
        raise HTTPException(status_code=400, detail="severity must be Sev-1..Sev-4")
    if req.status not in ("Active", "Suppressed"):
        raise HTTPException(status_code=400, detail="status must be Active or Suppressed")
    verdict = TriageVerdict(
        incident_id=req.incident_id,
        affected_service=req.affected_service,
        severity=req.severity,  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary=req.alert_summary or f"{req.severity} on {req.affected_service}",
        assigned_team=req.assigned_team,
        assigned_engineer=req.assigned_engineer,
        recommended_runbook=req.recommended_runbook,
        status=req.status,  # type: ignore[arg-type]
        audit_metadata=AuditMetadata(created_at=datetime.now(UTC), created_by="war-room-ui"),
    )
    if not req.create_bridge:
        return decide_war_room(verdict).model_dump(mode="json")
    assembly = assemble_war_room(verdict)
    # Surface it in the live feed too, so the try-it and pipeline share one view.
    _record_war_room(assembly.model_dump(mode="json"), verdict)
    return assembly.model_dump(mode="json")


@app.get("/api/war-room/recent")
def war_room_recent_endpoint(limit: int = 50) -> dict[str, Any]:
    """Newest-first feed of war rooms the live pipeline has assembled."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    rows = list(reversed(_RECENT_WAR_ROOMS))[:limit]
    return {"count": len(rows), "war_rooms": rows}


@app.get("/api/war-room/metrics")
def war_room_metrics_endpoint() -> dict[str, Any]:
    """Header metrics for the RA-006 dashboard, derived from the live feed."""
    rows = list(_RECENT_WAR_ROOMS)
    assembled = [r for r in rows if r.get("assembled")]
    total_smes = sum(r.get("sme_count", 0) for r in assembled)
    return {
        "total_seen": len(rows),
        "assembled": len(assembled),
        "suppressed_or_minor": len(rows) - len(assembled),
        "open": sum(1 for r in rows if r.get("status") in ("open", "in_call", "call_ended")),
        "resolved": sum(1 for r in rows if r.get("status") == "resolved"),
        "avg_smes": round(total_smes / len(assembled), 2) if assembled else None,
        "checked_at": datetime.now(UTC).isoformat(),
    }


class WarRoomStatusRequest(BaseModel):
    status: str


def _record_resolvers_from_row(row: dict[str, Any]) -> int:
    """On resolve, remember who fixed it so the war-room assembler can re-invite
    them next time this class of incident recurs (institutional memory).

    "Who fixed it" = the invited SMEs whose attendance is ``joined``; if none
    joined, fall back to the on-call engineer (the ``oncall``-sourced SME, else
    the first invited). Scoped to the incident's service + failure sub-domain
    (``notification.category_display``). Best-effort — never raises into the
    status transition. Returns the number of resolver rows written."""
    assembly = row.get("assembly") or {}
    invited = assembly.get("invited") or []
    if not invited:
        return 0
    joined = [s for s in invited if s.get("attendance") == "joined"]
    if not joined:
        # Fallback: the on-call SME (or, failing that, the first invited).
        oncall = [s for s in invited if s.get("source") == "oncall"]
        joined = oncall[:1] or invited[:1]

    service = row.get("service")
    if not service:
        return 0
    category = ((row.get("notification") or {}).get("category_display")) or None

    written = 0
    for sme in joined:
        handle = (sme.get("handle") or "").strip()
        if not handle:
            continue
        try:
            state_repo.save_incident_resolver(
                affected_service=service,
                category=category,
                resolver_handle=handle,
                resolver_name=sme.get("name"),
                resolver_email=None,
            )
            written += 1
        except Exception:
            logger.exception("resolver-memory: failed to record %s for %s", handle, service)
    return written


@app.post("/api/war-room/{wid}/status")
def war_room_set_status_endpoint(wid: str, req: WarRoomStatusRequest) -> dict[str, Any]:
    """Advance a war room's lifecycle (open → in_call → resolved). The board
    uses this so an operator can mark the bridge call in progress or the
    incident resolved. ``no_room`` rows are terminal and can't be advanced.

    On the first transition to ``resolved`` we record who fixed it (the joined
    SMEs, or the on-call as fallback) via ``_record_resolvers_from_row`` so a
    recurrence of the same service + sub-domain re-invites them."""
    if req.status not in WAR_ROOM_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {WAR_ROOM_STATUSES}")
    for row in _RECENT_WAR_ROOMS:
        if row.get("id") == wid:
            if row.get("status") == "no_room":
                raise HTTPException(status_code=409, detail="no_room war rooms are terminal")
            newly_resolved = req.status == "resolved" and row.get("status") != "resolved"
            row["status"] = req.status
            recorded = _record_resolvers_from_row(row) if newly_resolved else 0
            return {"id": wid, "status": req.status, "resolvers_recorded": recorded}
    raise HTTPException(status_code=404, detail=f"war room {wid!r} not found")


ATTENDANCE_STATUSES = ("invited", "joined", "declined")


class AttendeeStatusRequest(BaseModel):
    handle: str
    attendance: str


@app.post("/api/war-room/{wid}/attendee")
def war_room_set_attendee_endpoint(wid: str, req: AttendeeStatusRequest) -> dict[str, Any]:
    """Set one invited SME's attendance (invited → joined / declined) on a war
    room. Manual for now; an RSVP/presence feed can drive it automatically
    later. Matches the person by their ``handle`` within the war room."""
    if req.attendance not in ATTENDANCE_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"attendance must be one of {ATTENDANCE_STATUSES}"
        )
    for row in _RECENT_WAR_ROOMS:
        if row.get("id") != wid:
            continue
        for person in row.get("assembly", {}).get("invited", []):
            if person.get("handle") == req.handle:
                person["attendance"] = req.attendance
                return {"id": wid, "handle": req.handle, "attendance": req.attendance}
        raise HTTPException(
            status_code=404, detail=f"attendee {req.handle!r} not in war room {wid!r}"
        )
    raise HTTPException(status_code=404, detail=f"war room {wid!r} not found")


# ─── RA-002 Classifier UI mount (standalone Vite app under demo/classifier-ui) ─

CLASSIFIER_DIST = Path(__file__).parent.parent / "classifier-ui" / "dist"


@app.get("/classifier")
def classifier_root() -> FileResponse:
    """Serve the standalone RA-002 Incident Classifier dashboard root."""
    index = CLASSIFIER_DIST / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "classifier dashboard not built — "
                "run `cd demo/classifier-ui && npm install && npm run build`"
            ),
        )
    return FileResponse(index)


@app.get("/classifier/{path:path}", response_model=None)
def classifier_spa(path: str) -> FileResponse:
    """SPA-friendly catch-all for the RA-002 classifier dashboard.

    Serves real files from ``dist/`` when they exist (CSS, JS, images);
    otherwise falls back to ``index.html`` so the single-page app boots.
    """
    if not CLASSIFIER_DIST.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "classifier dashboard not built — "
                "run `cd demo/classifier-ui && npm install && npm run build`"
            ),
        )
    root = CLASSIFIER_DIST.resolve()
    target = (CLASSIFIER_DIST / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid classifier path") from exc
    if target.is_file():
        return FileResponse(target)
    return FileResponse(CLASSIFIER_DIST / "index.html")


# ─── RA-001+002 Combined UI mount (standalone Vite app under demo/combined-ui) ─

COMBINED_DIST = Path(__file__).parent.parent / "combined-ui" / "dist"


@app.get("/combined")
def combined_root() -> FileResponse:
    """Serve the standalone RA-001+002 Combined Triage + Classifier UI root."""
    index = COMBINED_DIST / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "combined dashboard not built — "
                "run `cd demo/combined-ui && npm install && npm run build`"
            ),
        )
    return FileResponse(index)


@app.get("/combined/{path:path}", response_model=None)
def combined_spa(path: str) -> FileResponse:
    """SPA-friendly catch-all for the RA-001+002 combined dashboard.

    Serves real files from ``dist/`` when they exist (CSS, JS, images);
    otherwise falls back to ``index.html`` so the single-page app boots.
    """
    if not COMBINED_DIST.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "combined dashboard not built — "
                "run `cd demo/combined-ui && npm install && npm run build`"
            ),
        )
    root = COMBINED_DIST.resolve()
    target = (COMBINED_DIST / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid combined path") from exc
    if target.is_file():
        return FileResponse(target)
    return FileResponse(COMBINED_DIST / "index.html")


# ─── HITL Approver UI mount (standalone Vite app under demo/hitl-ui) ──────

HITL_DIST = Path(__file__).parent.parent / "hitl-ui" / "dist"


@app.get("/hitl")
def hitl_root() -> FileResponse:
    """Serve the standalone HITL approver console (issue #77)."""
    index = HITL_DIST / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "hitl approver console not built — "
                "run `cd demo/hitl-ui && npm install && npm run build`"
            ),
        )
    return FileResponse(index)


@app.get("/hitl/{path:path}", response_model=None)
def hitl_spa(path: str) -> FileResponse:
    """SPA-friendly catch-all for the HITL approver console.

    Real assets (``assets/*.js``, ``assets/*.css``) are served as-is;
    everything else falls back to ``index.html`` so the SPA boots and
    React Router (if any) can take over.
    """
    if not HITL_DIST.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "hitl approver console not built — "
                "run `cd demo/hitl-ui && npm install && npm run build`"
            ),
        )
    root = HITL_DIST.resolve()
    target = (HITL_DIST / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid hitl path") from exc
    if target.is_file():
        return FileResponse(target)
    return FileResponse(HITL_DIST / "index.html")


# ─── scenarios (ecommerce SUT + matching alert rule) ───────────────────────
#
# Each scenario applies a fault to the ecommerce app in the `ecommerce`
# namespace — an env var on a Deployment, or a datastore StatefulSet scaled to
# zero. The matching Prometheus alert rule (the `ecommerce` group in
# infra/observability/prometheus-values.yaml) fires when the resulting anomaly
# crosses its threshold.
#
# Previously this flipped a flagd feature flag in the otel-demo namespace.
# flagd shipped as part of the OpenTelemetry Demo chart, which was removed in
# migration Phase 6, so there is no flag daemon any more. The catalog, the
# inject/reset actions and the "is it currently active?" read-back all live in
# demo/ui/scenario_provider.py.
#
# Still requires `kubectl` on the PATH of the uvicorn process — start.ps1 does
# that automatically. If running uvicorn directly, prepend
# %LOCALAPPDATA%\Programs\kubectl to PATH first.

SCENARIOS: dict[str, dict[str, Any]] = scenario_provider.load()


def _run_kubectl(args: list[str], *, input_text: str | None = None) -> str:
    """Invoke kubectl. Returns stdout. Raises HTTPException on non-zero exit."""
    try:
        r = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            input=input_text,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail="kubectl not on PATH — start the server via .\\start.ps1 (it prepends the kubectl path).",
        ) from exc
    if r.returncode != 0:
        raise HTTPException(
            status_code=502, detail=f"kubectl error: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout


def _apply_scenario(scenario_id: str, *, on: bool) -> dict[str, Any]:
    """Inject or recover a scenario on the ecommerce SUT.

    Replaces ``_toggle_flagd_flag``. There is no flag daemon any more, so no
    equivalent of ``_kick_flagd`` is needed either: the fault is applied by
    ``kubectl set env`` / ``kubectl scale``, which mutates the pod template and
    triggers a rollout immediately. The old flagd path had to force a restart
    because a ConfigMap edit is invisible to the running pod until kubelet's
    ~60-120s sync — that whole class of latency is gone.

    Both actions are idempotent, so a double-click on Inject is harmless.
    """
    result = (
        scenario_provider.inject(scenario_id, SCENARIOS)
        if on
        else scenario_provider.reset(scenario_id, SCENARIOS)
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "scenario action failed")
    return {
        "flag": result.get("failure_key"),
        "variant": "on" if on else "off",
        "applied_at": datetime.now(UTC).isoformat(),
    }


@app.get("/api/scenarios")
def list_scenarios() -> dict[str, Any]:
    """List the failure scenarios + whether each is currently active.

    ``current_variant`` used to come from flagd's in-memory variant map. There
    is no flag daemon now, so it is read back from the live cluster (env vars on
    Deployments, replica counts on datastore StatefulSets) — see
    ``scenario_provider.active_state``. If the cluster is unreachable every row
    reports "off" rather than erroring the page.
    """
    out: list[dict[str, Any]] = []
    current = scenario_provider.active_state(SCENARIOS)
    for sid, s in SCENARIOS.items():
        out.append(
            {
                **s,
                "scenario_id": sid,
                "current_variant": current.get(sid, "off"),
            }
        )
    return {"scenarios": out}


def _variant_on(s: dict[str, Any]) -> str:
    return str(s.get("variant_on") or "on")


def _clear_scenario_clusters(s: dict[str, Any]) -> None:
    """Best-effort: drop the dedup clusters for a scenario's service.

    A failed clear must not fail the reset — worst case the next inject is
    Suppressed for up to the 5-minute cluster window, which is the old
    (pre-fix) behaviour, not a new failure mode.
    """
    service = s.get("service")
    if not service:
        return
    try:
        removed = state_repo.clear_clusters_for_service(str(service))
        if removed:
            logger.info(
                "scenario reset: cleared %d dedup cluster(s) for service=%r", removed, service
            )
    except Exception:
        logger.exception("scenario reset: cluster clear failed for service=%r", service)


def _triage_injected_scenario(scenario_id: str, s: dict[str, Any]) -> None:
    """Run triage→classify→ticket→notify for a freshly injected scenario.

    This is what connects the dashboard's **Inject** button to the
    Notification page + Slack. Previously inject only flipped the flag and
    surfaced a synthetic alert in Alert Stream; turning that into an actual
    notification depended on the background auto-triage poller — which is
    off by default here (``AIOPS_AUTO_TRIAGE_ENABLED``) and, even when on,
    could suppress a re-inject against a still-warm dedup cluster.

    Running the chain directly makes inject deterministic: every click
    produces one Active verdict whose RA-005 routing emits to every chatops
    sink (WebSocket → Notification page, Slack DM/channel, JSONL audit) and
    persists a NotificationRow for the page's backfill.

    Best-effort: a failure here must not break the (already-succeeded) flag
    flip, so everything is caught and logged.
    """
    alert = _synthetic_alert_for_scenario(scenario_id, s)
    try:
        triage_alert(TriageRequest(alert=alert))
        logger.info("inject: triage chain fired for scenario %s -> chatops", scenario_id)
    except HTTPException as exc:
        logger.warning("inject: triage chain skipped for %s: %s", scenario_id, exc.detail)
    except Exception:
        logger.exception("inject: triage chain failed for scenario %s", scenario_id)


@app.post("/api/scenarios/{scenario_id}/inject")
def inject_scenario(scenario_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Flip the scenario's flag on AND fire the triage→notify chain.

    Returns immediately; the chain runs as a background task so the button
    doesn't block on the LLM. The notification lands on the Notification
    page + Slack within a couple of seconds.
    """
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(
            status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}"
        )
    result = _apply_scenario(scenario_id, on=True)
    # This fault only shows up under traffic (rate()/histogram rules, per-request
    # CPU burn), so un-pause the load generator before it settles. Otherwise the
    # fault is active and completely invisible. Scale-up only; see
    # scenario_provider.ensure_loadgen_running.
    loadgen = (
        scenario_provider.ensure_loadgen_running()
        if s.get("needs_load")
        else {"action": "not_needed", "detail": "fault is observable on an idle cluster"}
    )
    # Explicit inject = a fresh incident. Clear the service's dedup clusters
    # so triage yields an Active verdict (not Suppressed against a warm
    # cluster from a prior inject), and mark the synthetic alert id seen so
    # the background poller (if enabled) doesn't double-fire it.
    _clear_scenario_clusters(s)
    alert = _synthetic_alert_for_scenario(scenario_id, s)
    _AUTO_TRIAGE.mark_seen(alert.get("alert_id", ""))
    background_tasks.add_task(_triage_injected_scenario, scenario_id, s)
    return {
        **s,
        "scenario_id": scenario_id,
        **result,
        "expected_alert": s["alert"],
        "triage_triggered": True,
        "loadgen": loadgen,
    }


@app.post("/api/scenarios/{scenario_id}/reset")
def reset_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag back to ``off``."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(
            status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}"
        )
    result = _apply_scenario(scenario_id, on=False)
    # DEMO-AUTO-TRIAGE (#130): drop this scenario's alert ids from the
    # auto-triage seen set so a re-inject of the same scenario fires the
    # chain again. The Prometheus alert_id is stable per (alertname,
    # instance) so the previous id is the same — without this, the loop
    # would silently dedupe the re-inject.
    _AUTO_TRIAGE.forget_all()
    # Reset ends the incident: clear this service's dedup clusters so the
    # next inject triages as a NEW incident (Active verdict → chatops emit
    # → Notifications page) instead of getting Suppressed against the
    # previous run's still-warm cluster. Scoped to the one service so
    # dedup keeps working for unrelated still-firing alerts.
    _clear_scenario_clusters(s)
    return {**s, "scenario_id": scenario_id, **result}


@app.post("/api/scenarios/reset-all")
def reset_all_scenarios() -> dict[str, Any]:
    """Recover every scenario on the ecommerce SUT.

    NOT atomic, unlike the flagd version this replaces — that wrote one
    server-side-apply patch covering all flags, so flagd reloaded once. Here each
    recovery is its own kubectl call, so a partial failure is possible. The
    per-scenario ``results`` say which ones landed; the endpoint still returns
    200 so one unhealthy workload cannot block resetting the rest.
    """
    outcome = scenario_provider.reset_all(SCENARIOS)
    # DEMO-AUTO-TRIAGE (#130): see comment in reset_scenario above.
    _AUTO_TRIAGE.forget_all()
    # Same incident-over semantics as the single reset, for every scenario.
    for s in SCENARIOS.values():
        _clear_scenario_clusters(s)
    return {
        "reset_count": outcome.get("reset_count", 0),
        "touched": outcome.get("touched", []),
        "results": outcome.get("results", []),
        "applied_at": datetime.now(UTC).isoformat(),
    }


# ─── /api/topology ─────────────────────────────────────────────────────────
#
# Two data sources from Jaeger:
#   1. /api/services       — service inventory  (always available)
#   2. /api/dependencies   — directed call graph (requires the dependencies
#                            job to have run; may return empty in fresh clusters)
# The OTel demo's Jaeger v2 mounts these under /jaeger/ui/api/*.


_JAEGER_URL = os.environ.get("AIOPS_JAEGER_URL", "http://localhost:16686")
_JAEGER_PREFIX = os.environ.get("AIOPS_JAEGER_API_PREFIX", "/jaeger/ui")


@app.get("/api/topology")
def get_topology() -> dict[str, Any]:
    """Return a {nodes, edges} payload derived from Jaeger.

    Nodes come from /api/services (filtered to drop jaeger-internal services).
    Edges come from /api/dependencies if available; otherwise edges=[] and the
    UI shows a hint about generating load.
    """
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=8.0) as client:
            svcs = client.get(f"{_JAEGER_URL}{_JAEGER_PREFIX}/api/services")
            svcs.raise_for_status()
            for name in svcs.json().get("data") or []:
                if name and "jaeger" not in name.lower():
                    nodes.append({"id": name, "label": name})

            end_ms = int(datetime.now(UTC).timestamp() * 1000)
            try:
                deps = client.get(
                    f"{_JAEGER_URL}{_JAEGER_PREFIX}/api/dependencies",
                    params={"endTs": str(end_ms), "lookback": str(3600 * 1000)},
                )
                deps.raise_for_status()
                for d in deps.json().get("data") or []:
                    if d.get("parent") and d.get("child"):
                        edges.append(
                            {
                                "source": d["parent"],
                                "target": d["child"],
                                "call_count": int(d.get("callCount") or 0),
                            }
                        )
            except httpx.HTTPError:
                pass  # dependencies job may not have run yet
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"jaeger unreachable: {exc}") from exc
    return {"nodes": nodes, "edges": edges, "source": "jaeger"}


# ─── /api/system/pods ──────────────────────────────────────────────────────


_AGE_PATTERN = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+(\S+)")


@app.get("/api/system/pods")
def get_pods(namespace: str = "ecommerce") -> dict[str, Any]:
    """List pods in the namespace using ``kubectl get pods``.

    Parses the default columnar output (NAME READY STATUS RESTARTS AGE). We
    parse plaintext rather than JSON because the JSON path requires loading
    each pod's full status object, which is much heavier.
    """
    raw = _run_kubectl(["get", "pods", "-n", namespace, "--no-headers"])
    rows: list[dict[str, Any]] = []
    ready_count = 0
    not_ready_count = 0
    for line in raw.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        name, ready, status, restarts, age = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            r, t = ready.split("/")
            is_ready = int(r) == int(t) and int(t) > 0 and status in {"Running", "Completed"}
        except (ValueError, TypeError):
            is_ready = False
        if is_ready:
            ready_count += 1
        else:
            not_ready_count += 1
        try:
            restarts_int = int(restarts)
        except (ValueError, TypeError):
            restarts_int = 0
        rows.append(
            {
                "name": name,
                "ready": ready,
                "status": status,
                "restarts": restarts_int,
                "age": age,
            }
        )
    return {
        "namespace": namespace,
        "pods": rows,
        "total": len(rows),
        "ready_count": ready_count,
        "not_ready_count": not_ready_count,
    }


# ─── WebSocket /ws/alerts ──────────────────────────────────────────────────
#
# Single broadcaster task polls Prometheus once per N seconds and pushes the
# result to every connected client.  The hub itself lives in
# ``demo.ui._alert_hub`` (extracted in #68); this module owns only the
# frame-shaping function that knows about ``live_alerts()``.


def _collect_alerts_frame() -> dict[str, Any]:
    """Same shape as /api/live-alerts but wrapped as a WS frame."""
    try:
        data = live_alerts()
    except HTTPException as exc:
        return {"type": "error", "detail": str(exc.detail)}
    return {
        "type": "alerts",
        "alerts": data["alerts"],
        "fetched_at": datetime.now(UTC).isoformat(),
    }


_register_alert_hub_routes(app, _collect_alerts_frame)


# ─── React dashboard mount (alongside the vanilla UI) ──────────────────────
#
# Build with: cd demo/dashboard && npm install && npm run build
# Output goes to demo/dashboard/dist/ which we mount at /dashboard.

DASHBOARD_DIST = Path(__file__).parent.parent / "dashboard" / "dist"


@app.get("/dashboard")
def dashboard_root() -> FileResponse:
    """Redirect bare /dashboard to /dashboard/ so relative asset URLs resolve."""
    index = DASHBOARD_DIST / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=503,
            detail="dashboard not built — run `cd demo/dashboard && npm install && npm run build`",
        )
    return FileResponse(index)


@app.get("/dashboard/{path:path}", response_model=None)
def dashboard_spa(path: str) -> FileResponse:
    """SPA-friendly fallback for React Router deep links.

    - Real assets that exist on disk (``assets/*.js``, ``assets/*.css``,
      ``index.html``) are returned as-is.
    - Anything else falls back to ``index.html`` so React Router can take
      over (e.g. ``/dashboard/notifications`` or ``/dashboard/classifier``
      typed directly into the URL bar still load the SPA).
    """
    if not DASHBOARD_DIST.exists():
        raise HTTPException(
            status_code=503,
            detail="dashboard not built — run `cd demo/dashboard && npm install && npm run build`",
        )
    # Block path traversal — resolved file must stay under DASHBOARD_DIST.
    root = DASHBOARD_DIST.resolve()
    target = (DASHBOARD_DIST / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid dashboard path") from exc
    if target.is_file():
        return FileResponse(target)
    return FileResponse(DASHBOARD_DIST / "index.html")


# Mount static files AFTER routes so /api/*, /ws/*, and / aren't overshadowed.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
