"""FastAPI service wrapping the Alert Triage agent for the demo UI.

The full endpoint catalog is served by FastAPI itself — visit ``GET /docs``
(Swagger) or ``GET /openapi.json`` on a running server. We keep the inventory
there instead of in a hand-maintained list that drifts every time someone
adds a route (it last went stale at 12 routes when the file actually had 21).

Roughly, the service hosts: the Alert Triage agent (POST /api/triage*,
GET /api/fixtures, GET /api/verdicts), the Incident Classifier (RA-002) under
/api/classifier/*, the failure-injection scenario endpoints under
/api/scenarios/*, the live cluster mirrors (/api/live-alerts, /api/topology,
/api/system/pods), and two WebSocket fan-outs (/ws/alerts, /ws/chatops). The
React dashboard mounts at /dashboard/, the standalone classifier SPA at
/classifier, and a legacy vanilla UI at /.

The agent runs in-process (single uvicorn worker = single dedup store) so the
embedding dedup memory persists across triage calls within one server lifetime.

Vendor neutrality: this module uses ``aiops.tools`` capabilities and
``agents.alert_triage`` / ``agents.incident_classifier`` only — it does not
import Prometheus / Jaeger / Kubernetes clients directly.
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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
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
from agents.alert_triage import Alert, TriageVerdict, triage  # noqa: E402
from agents.auto_ticketing import ticket as auto_ticket  # noqa: E402
from agents.incident_classifier import ClassificationInput, classify  # noqa: E402
from agents.notification_router import route as route_notification  # noqa: E402
from agents.rca_agent.agent import analyze as rca_analyze  # noqa: E402
from aiops import llm as aiops_llm  # noqa: E402
from aiops.policy import (  # noqa: E402
    ApprovalError,
    get_approval_registry,
    install_chatops_listener,
    install_default_approver,
)
from aiops.state import init_db  # noqa: E402
from aiops.state import repository as state_repo  # noqa: E402
from aiops.tools import (  # noqa: E402
    feature_flags,  # noqa: F401  — ARCH-1 @tool registration
    get_registry,
)
from aiops.tools.alerts.prometheus_adapter import to_canonical_alert  # noqa: E402
from aiops.tools.chatops import register_env_adapters  # noqa: E402
from demo.ui.chatops_ws import bootstrap_websocket_adapter  # noqa: E402
from demo.ui.chatops_ws import register_routes as _register_chatops_ws_routes  # noqa: E402

logger = logging.getLogger(__name__)

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


def _register_chatops_adapters() -> None:
    """Register the chatops sinks (JSONL audit log + Slack + PagerDuty).
    Idempotent by adapter class so re-running startup (tests, hot-reload)
    does not register duplicate sinks of the same kind. Without this guard
    the same audit JSON line lands twice per send()."""
    audit_path = Path(__file__).resolve().parents[2] / "demo" / "audit" / "chatops.jsonl"
    try:
        register_env_adapters(
            audit_path=audit_path,
            slack_webhook_url=os.environ.get("AIOPS_SLACK_WEBHOOK_URL", "").strip(),
            pagerduty_integration_key=os.environ.get(
                "AIOPS_PAGERDUTY_INTEGRATION_KEY", ""
            ).strip(),
        )
        logger.info("chatops: registered env-driven chatops adapters")
    except ValueError as exc:
        logger.warning("chatops: invalid chatops adapter config (%s); skipping invalid adapters", exc)


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
       webhook + PagerDuty (both opt-in via env). Idempotent.
    5. ``bootstrap_websocket_adapter()`` — must run inside the asyncio loop
       so ``asyncio.get_running_loop()`` resolves to the server's loop.
       This is why the lifespan is ``async``.
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
    install_chatops_listener()
    install_default_approver()
    _register_chatops_adapters()
    bootstrap_websocket_adapter()
    _ensure_hitl_agent_pool()
    await _start_auto_triage()

    yield

    await _stop_auto_triage()
    _shutdown_hitl_agent_pool()


app = FastAPI(
    title="Adaptive AIOps — Alert Triage demo",
    version="0.1.0",
    lifespan=lifespan,
)

_register_chatops_ws_routes(app)


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


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint. Currently exposes ``aiops_scenario_active``
    only — the gauge that Plan B's ScenarioActive alert rule keys on. Refresh
    is synchronous (re-reads flagd configmap) so every scrape reflects current
    truth, not cached state. If flagd is unreachable the gauge is left as-is
    rather than 500ing the scrape."""
    _refresh_scenario_gauge()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/fixtures")
def list_fixtures() -> dict[str, Any]:
    """Return the contents of ``evals/golden.json`` for the UI's fixture pane."""
    if not FIXTURES_PATH.exists():
        raise HTTPException(status_code=500, detail=f"fixtures file not found: {FIXTURES_PATH}")
    with FIXTURES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


class TriageRequest(BaseModel):
    alert: dict[str, Any] = Field(..., description="Canonical Alert payload")


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

    verdict: TriageVerdict = triage(alert_obj)
    verdict_id = state_repo.save_verdict(verdict, cluster_key=alert_obj.cluster_key())

    # Classify BEFORE ticketing (DEMO-3 / #55): RA-002 does not depend on the
    # ticket id, and the ServiceNow incident's ``category`` + description
    # block are only useful if classification is available at create time.
    classification = classify(ClassificationInput(alert=alert_obj, triage_verdict=verdict))
    classification_id = state_repo.save_classification(classification, verdict_id=verdict_id)

    # alert_name (DEMO-8 / #60): the Prometheus alert rule name, used by
    # auto_ticket to look up the matching Grafana panel and attach a
    # screenshot to the ServiceNow incident.
    ticket_record = auto_ticket(
        verdict,
        classification=classification,
        alert_name=alert_obj.metric,
    )

    notifications: dict[str, Any] | None = None
    deliveries: dict[str, Any] | None = None
    notification_id: int | None = None
    try:
        # #84: route() returns a RoutingOutcome bundling the decision with
        # per-adapter DeliveryResults. Persistence + the existing response
        # shape want the flat decision; deliveries surface as a sibling key.
        outcome = route_notification(verdict)
        decision = outcome.decision
        # CHAT-2 (#82): persist the structured row alongside the existing
        # JSONL audit log. Persistence failure must not break the pipeline —
        # the JSONL adapter (the source of truth) already wrote.
        try:
            notification_id = state_repo.save_notification(decision, verdict_id=verdict_id)
        except Exception:
            logger.exception(
                "RA-005: persist save_notification failed for verdict %s on %s "
                "(JSONL audit log still written)",
                verdict_id,
                verdict.affected_service,
            )
        notifications = decision.model_dump(mode="json")
        deliveries = {name: r.model_dump(mode="json") for name, r in outcome.deliveries.items()}
    except Exception:
        logger.exception("RA-005: routing failed for verdict on %s", verdict.affected_service)

    return {
        "verdict": verdict.model_dump(mode="json"),
        "ticket": ticket_record.model_dump(mode="json"),
        "classification": classification.model_dump(mode="json"),
        "notifications": notifications,
        "deliveries": deliveries,
        "persisted": {
            "verdict_id": verdict_id,
            "classification_id": classification_id,
            "notification_id": notification_id,
        },
    }


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


@app.get("/api/live-alerts")
def live_alerts() -> dict[str, Any]:
    """Pull currently-firing alerts from Prometheus via the registry."""
    registry = get_registry()
    try:
        res = registry.call("observability.metrics.alerts")
    except KeyError:
        raise HTTPException(  # noqa: B904
            status_code=503, detail="observability.metrics.alerts not registered"
        )
    if not res.ok:
        raise HTTPException(status_code=502, detail=f"prometheus error: {res.error}")
    alerts = (res.data or {}).get("alerts", [])
    # Render each Prometheus alert as a candidate Alert payload the UI can post back.
    candidates = [to_canonical_alert(a) for a in alerts]
    return {"count": len(candidates), "alerts": candidates, "raw_count": len(alerts)}


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


@app.post("/api/rca", response_model=None)
async def rca_endpoint(req: RcaRequest) -> dict[str, Any]:
    """Run the RCA Agent (PRS-008) against a prior triage verdict.

    Body: ``{"triage_verdict": {<TriageVerdict dict>}, "scenario_id"?: str}``.
    Returns an ``RCAVerdict`` with ``root_cause``, ``ranked_fix_steps`` (each
    with ``blast_radius`` + ``rollback``), and ``confidence_score``. Every
    fix step is tagged ``requires_hitl=true``; the platform HITL gate enforces
    approval at the action boundary — this endpoint does NOT execute the fix.

    The agent's LLM call can take 5–15 s (Claude via Foundry); ``rca_analyze``
    is sync + blocking, so we wrap it in ``asyncio.to_thread`` to keep the
    event loop free.
    """
    try:
        verdict = await asyncio.to_thread(
            rca_analyze, req.triage_verdict, scenario_id=req.scenario_id
        )
    except Exception as exc:
        logger.exception(
            "RCA agent raised on payload for %s", req.triage_verdict.get("affected_service")
        )
        raise HTTPException(status_code=500, detail=f"RCA failed: {exc}") from exc
    return verdict.model_dump(mode="json")


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
    namespace: str = Field("otel-demo")
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


def _require_approval_token(request: Request) -> None:
    """Authenticate the web approve/deny endpoints against
    ``AIOPS_HITL_APPROVAL_TOKEN``.

    Phase 1 of HITL-2 (#102): a lightweight shared-secret bearer-token
    check.  When the env var is **unset** we accept every request so the
    current localhost-only demo flow keeps working (the startup hook
    above logs a loud warning).  When **set**, callers must present
    ``Authorization: Bearer <token>`` and we compare it with
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

        agent_dir = REPO_ROOT / "agents" / "incident_classifier"
        run = await asyncio.to_thread(run_agent, agent_dir)

        total = len(run.results)
        passed = sum(1 for r in run.results if r.passed)
        misroute = 0
        per_case: list[dict[str, Any]] = []
        for r in run.results:
            type_check = next(
                (c for c in r.details.get("checks", []) if c["check"] == "incident_type"),
                None,
            )
            type_ok = bool(type_check and type_check["passed"])
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


# ─── scenarios (flagd flip + matching alert rule) ──────────────────────────
#
# Each scenario flips one flagd flag in the otel-demo namespace. The matching
# Prometheus alert rule (inlined under prometheus.serverFiles.alerting_rules.yml
# in demo/otel-demo/values.yaml) fires when the resulting metric anomaly
# crosses its threshold.
#
# This requires `kubectl` on the PATH of the uvicorn process — start.ps1 does
# that automatically. If running uvicorn directly, prepend
# %LOCALAPPDATA%\Programs\kubectl to PATH first.

# Scenario catalog lives in demo/scenarios/*.yaml — one file per scenario.
# Schema and conventions: demo/scenarios/README.md.
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def _load_scenarios() -> dict[str, dict[str, Any]]:
    """Read every UI-descriptor YAML in ``demo/scenarios/`` into a dict keyed by id.

    The dict key is the scenario id (also the filename stem); the value
    is the rest of the YAML record. We pop ``id`` from the value because
    it is already the key — keeping it both places would risk drift.
    Iteration order is alphabetical by filename; the UI then groups by
    ``category`` so the on-screen layout is stable regardless of fs order.

    DEMO-12 (#64): the folder now also holds CLI-runnable scenarios that
    declare a ``mechanism`` field. Those are the responsibility of
    ``demo.failure_injection.inject`` and are *not* part of the dashboard
    catalog. We skip them here so ``/api/scenarios`` only returns the rows
    the React Overview page knows how to render.
    """
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"scenario file {path.name} must be a YAML mapping")
        if "mechanism" in data:
            # CLI-runnable scenario — owned by inject.py, not the UI catalog.
            continue
        sid = data.pop("id", None)
        if sid != path.stem:
            raise RuntimeError(
                f"scenario file {path.name}: 'id' must equal filename stem {path.stem!r}, got {sid!r}"
            )
        out[sid] = data
    return out


SCENARIOS: dict[str, dict[str, Any]] = _load_scenarios()


# Synthetic gauge surfaced at /metrics so Prometheus has a signal that fires
# the moment a scenario is injected — bypasses the upstream OTel-demo gap where
# payment/product-catalog spans stay STATUS_CODE_UNSET even when the failure
# flag is on. Labels match what the existing alert-rule machinery expects so
# the agent chain's CMDB lookup has a usable `service` to route on.
_SCENARIO_ACTIVE = Gauge(
    "aiops_scenario_active",
    "1 when the scenario's flag is in a non-off variant per flagd; 0 otherwise.",
    ["scenario_id", "flag", "service"],
)


def _refresh_scenario_gauge() -> None:
    """Re-derive ``aiops_scenario_active`` from the live flagd configmap.

    Called on every ``/metrics`` scrape so the gauge tracks reality without
    needing the UI to push on every inject/reset. Failure to reach flagd
    leaves the gauge at its last-known state (no exception bubbles up — a
    scrape that 500s is worse than a scrape that's slightly stale)."""
    try:
        res = get_registry().call("feature_flags.list_variants")
    except Exception:
        return
    if not res.ok:
        return
    current: dict[str, str] = (res.data or {}).get("variants", {})
    for sid, s in SCENARIOS.items():
        variant = current.get(s["flag"], "off")
        _SCENARIO_ACTIVE.labels(
            scenario_id=sid,
            flag=s["flag"],
            service=s.get("service", "unknown"),
        ).set(1 if variant != "off" else 0)


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


def _toggle_flagd_flag(flag_name: str, variant: str) -> dict[str, Any]:
    """Set ``flags.<flag_name>.defaultVariant`` to ``variant`` via the ARCH-1
    feature-flags seam. Variant must be one of the variants declared for that
    flag in flagd-config. flagd watches the configmap and reloads ~1 s later."""
    res = get_registry().call("feature_flags.set_variant", flag=flag_name, variant=variant)
    if not res.ok:
        meta = res.metadata or {}
        if "available_flags" in meta:
            raise HTTPException(status_code=404, detail=res.error)
        if "valid_variants" in meta:
            raise HTTPException(status_code=400, detail=res.error)
        raise HTTPException(status_code=502, detail=res.error)
    return {
        "flag": flag_name,
        "variant": variant,
        "applied_at": datetime.now(UTC).isoformat(),
    }


@app.get("/api/scenarios")
def list_scenarios() -> dict[str, Any]:
    """List the available failure scenarios + their current variant in flagd."""
    out: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    try:
        res = get_registry().call("feature_flags.list_variants")
        if res.ok:
            current = (res.data or {}).get("variants", {})
    except Exception:
        pass
    for sid, s in SCENARIOS.items():
        out.append(
            {
                **s,
                "scenario_id": sid,
                "current_variant": current.get(s["flag"], "off"),
            }
        )
    return {"scenarios": out}


def _variant_on(s: dict[str, Any]) -> str:
    return str(s.get("variant_on") or "on")


@app.post("/api/scenarios/{scenario_id}/inject")
def inject_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag to its on-variant. Returns the expected alert
    name + ETA so the UI can poll ``/api/live-alerts`` until it fires."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(
            status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}"
        )
    result = _toggle_flagd_flag(s["flag"], _variant_on(s))
    return {**s, "scenario_id": scenario_id, **result, "expected_alert": s["alert"]}


@app.post("/api/scenarios/{scenario_id}/reset")
def reset_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag back to ``off``."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(
            status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}"
        )
    result = _toggle_flagd_flag(s["flag"], "off")
    # DEMO-AUTO-TRIAGE (#130): drop this scenario's alert ids from the
    # auto-triage seen set so a re-inject of the same scenario fires the
    # chain again. The Prometheus alert_id is stable per (alertname,
    # instance) so the previous id is the same — without this, the loop
    # would silently dedupe the re-inject.
    _AUTO_TRIAGE.forget_all()
    return {**s, "scenario_id": scenario_id, **result}


@app.post("/api/scenarios/reset-all")
def reset_all_scenarios() -> dict[str, Any]:
    """Flip every scenario flag back to ``off`` in a single SSA patch via the
    ARCH-1 feature-flags seam. Atomic — flagd reloads once instead of N times.
    """
    flag_names = [s["flag"] for s in SCENARIOS.values()]
    res = get_registry().call("feature_flags.reset_all", flags=flag_names)
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error or "reset_all failed")
    # DEMO-AUTO-TRIAGE (#130): see comment in reset_scenario above.
    _AUTO_TRIAGE.forget_all()
    data = res.data or {}
    return {
        "reset_count": data.get("reset_count", 0),
        "touched": data.get("touched", []),
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
def get_pods(namespace: str = "otel-demo") -> dict[str, Any]:
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
# result to every connected client. Cheaper than each tab polling directly.


class _AlertHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._broadcast_loop())

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def _broadcast_loop(self) -> None:
        interval = float(os.environ.get("AIOPS_ALERT_BROADCAST_INTERVAL", "5"))
        while True:
            async with self._lock:
                if not self._clients:
                    return  # last client gone; stop the task
                clients = list(self._clients)
            payload = await asyncio.to_thread(_collect_alerts_frame)
            stale: list[WebSocket] = []
            for ws in clients:
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            if stale:
                async with self._lock:
                    for ws in stale:
                        self._clients.discard(ws)
            await asyncio.sleep(interval)


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


_HUB = _AlertHub()


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket) -> None:
    await _HUB.connect(ws)
    try:
        # Send one frame immediately so the UI doesn't sit empty for N seconds.
        await ws.send_json(_collect_alerts_frame())
        while True:
            await ws.receive_text()  # client keepalive pings; ignore content
    except WebSocketDisconnect:
        pass
    finally:
        await _HUB.disconnect(ws)


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
