"""FastAPI service wrapping the Alert Triage agent for the demo UI.

Endpoints:

- ``GET  /``                       — serves ``static/index.html`` (legacy vanilla UI)
- ``GET  /dashboard/*``            — serves the React+Vite dashboard build (if present)
- ``GET  /api/health``             — liveness + agent + port-forward checks
- ``GET  /api/fixtures``           — list golden.json cases for the UI's fixture pane
- ``POST /api/triage``             — run the agent on a posted Alert payload
- ``POST /api/triage/fixture/{id}``— run the agent on a named fixture
- ``GET  /api/live-alerts``        — fetch currently firing Prometheus alerts
- ``POST /api/triage/live``        — triage every firing alert and return all verdicts
- ``GET  /api/topology``           — service nodes + dependency edges from Jaeger
- ``GET  /api/system/pods``        — kubectl-derived pod status for otel-demo
- ``WS   /ws/alerts``              — push live Prometheus alerts to the dashboard

The agent runs in-process (single uvicorn worker = single dedup store) so the
embedding dedup memory persists across triage calls within one server lifetime.

Vendor neutrality: this module uses ``aiops.tools`` capabilities and
``agents.alert_triage`` only — it does not import Prometheus / Jaeger clients
directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Default to the stub LLM so the demo runs without an API key. Override by
# setting AIOPS_LLM_PROVIDER=anthropic before launching uvicorn.
os.environ.setdefault("AIOPS_LLM_PROVIDER", "stub")

# Importing the agent triggers @tool registration for prometheus, jaeger,
# and the mock CMDB / on-call providers.
from agents.alert_triage import Alert, TriageVerdict, triage  # noqa: E402
from aiops.tools import get_registry  # noqa: E402

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
FIXTURES_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "alert_triage" / "evals" / "golden.json"
)

app = FastAPI(title="Adaptive AIOps — Alert Triage demo", version="0.1.0")


# ─── routes ─────────────────────────────────────────────────────────────────


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Quick probe: agent importable, mocks registered, port-forwards reachable."""
    registry = get_registry()
    caps = sorted({t.capability for t in registry.list()})

    prom_ok = False
    jaeger_ok = False
    try:
        res = registry.call("observability.metrics.query", promql="up")
        prom_ok = bool(res.ok)
    except Exception:  # noqa: BLE001
        pass
    try:
        res = registry.call("observability.traces.services")
        jaeger_ok = bool(res.ok)
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": "ok",
        "llm_provider": os.environ.get("AIOPS_LLM_PROVIDER"),
        "registered_capabilities": caps,
        "prometheus_reachable": prom_ok,
        "jaeger_reachable": jaeger_ok,
        "checked_at": datetime.now(timezone.utc).isoformat(),
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


@app.post("/api/triage", response_model=None)
def triage_alert(req: TriageRequest) -> dict[str, Any]:
    """Triage a single alert. Body: ``{"alert": {<Alert payload>}}``."""
    try:
        alert_obj = Alert(**req.alert)
    except Exception as exc:  # noqa: BLE001 — boundary validation
        raise HTTPException(status_code=400, detail=f"invalid alert: {exc}") from exc
    verdict: TriageVerdict = triage(alert_obj)
    return verdict.model_dump(mode="json")


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
    candidates = [_prometheus_alert_to_candidate(a) for a in alerts]
    return {"count": len(candidates), "alerts": candidates, "raw_count": len(alerts)}


@app.post("/api/triage/live", response_model=None)
def triage_live() -> dict[str, Any]:
    """Fetch firing Prometheus alerts and triage every one."""
    payload = live_alerts()
    verdicts: list[dict[str, Any]] = []
    for candidate in payload["alerts"]:
        try:
            verdicts.append(triage_alert(TriageRequest(alert=candidate)))
        except HTTPException as exc:
            verdicts.append({"error": exc.detail, "alert": candidate})
    return {"count": len(verdicts), "verdicts": verdicts}


# ─── helpers ────────────────────────────────────────────────────────────────


def _prometheus_alert_to_candidate(alert: dict[str, Any]) -> dict[str, Any]:
    """Translate a Prometheus /api/v1/alerts entry into the canonical Alert shape.

    This is the v0 source-adapter for Prometheus → canonical Alert (the
    workflow step 3 'Normalize' work that was deferred). Lives here for now;
    move to ``aiops/tools/alerts/prometheus_adapter.py`` when other sources
    show up.
    """
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    service = (
        labels.get("service")
        or labels.get("service_name")
        or labels.get("job")
        or annotations.get("service")
        or "unknown"
    )
    metric = (
        labels.get("alertname")
        or labels.get("__name__")
        or annotations.get("summary")
        or "alert"
    )
    value_str = alert.get("value") or labels.get("value") or "0"
    try:
        value = float(value_str)
    except (TypeError, ValueError):
        value = 0.0
    return {
        "alert_id": f"PROM-{labels.get('alertname','UNKNOWN')}-{labels.get('instance', 'na')}",
        "service": service,
        "metric": metric,
        "value": value,
        "timestamp": alert.get("activeAt") or datetime.now(timezone.utc).isoformat(),
        "source": "Prometheus",
        "severity_hint": labels.get("severity"),
        "labels": {k: str(v) for k, v in labels.items()},
        "annotations": {k: str(v) for k, v in annotations.items()},
    }


# ─── scenarios (flagd flip + matching alert rule) ──────────────────────────
#
# Each scenario flips one flagd flag in the otel-demo namespace. The matching
# Prometheus alert rule (deployed via infra/prometheus-rules.yml) fires when
# the resulting metric anomaly crosses its threshold.
#
# This requires `kubectl` on the PATH of the uvicorn process — start.ps1 does
# that automatically. If running uvicorn directly, prepend
# %LOCALAPPDATA%\Programs\kubectl to PATH first.

# Scenario catalog. Each entry maps a flagd flag to:
#   - alert       : the matching Prometheus alert rule name (rule lives in
#                   infra/prometheus-rules.yml)
#   - service     : OTel demo service whose telemetry the alert reads
#   - variant_on  : variant name to set when the user clicks "Inject". Some flags
#                   have intensity variants (paymentFailure: 100%/90%/.../off,
#                   imageSlowLoad: 10sec/5sec/off, emailMemoryLeak: 1x/.../10000x).
#                   When omitted we use "on" (the default for simple toggles).
#   - category    : grouping for the UI ("errors", "latency", "capacity", "infra")
SCENARIOS: dict[str, dict[str, Any]] = {
    # ── HTTP-level failures (5xx rate signals) ──────────────────────────────
    "payment_failure": {
        "flag": "paymentFailure",
        "variant_on": "100%",
        "alert": "PaymentErrorRateHigh",
        "service": "payment",
        "title": "Payment failure (HTTP 500s)",
        "description": "Payment service rejects every charge with a 5xx error.",
        "category": "errors",
        "eta_seconds": 90,
    },
    "payment_unreachable": {
        "flag": "paymentUnreachable",
        "alert": "PaymentErrorRateHigh",
        "service": "payment",
        "title": "Payment unreachable",
        "description": "Payment endpoint is unreachable — connection refused upstream.",
        "category": "errors",
        "eta_seconds": 90,
    },
    "cart_failure": {
        "flag": "cartFailure",
        "alert": "CartErrorRateHigh",
        "service": "cart",
        "title": "Cart failure (HTTP 500s)",
        "description": "Cart service errors out on every request.",
        "category": "errors",
        "eta_seconds": 90,
    },
    "product_catalog_failure": {
        "flag": "productCatalogFailure",
        "alert": "ProductCatalogErrorRateHigh",
        "service": "product-catalog",
        "title": "Product catalog failure",
        "description": "Product catalog returns errors on a subset of products.",
        "category": "errors",
        "eta_seconds": 90,
    },
    "ad_failure": {
        "flag": "adFailure",
        "alert": "AdErrorRateHigh",
        "service": "ad",
        "title": "Ad service failure",
        "description": "Ad service returns 5xx errors — banners disappear from the homepage.",
        "category": "errors",
        "eta_seconds": 90,
    },

    # ── Latency / cache failures ────────────────────────────────────────────
    "recommendation_cache_failure": {
        "flag": "recommendationCacheFailure",
        "alert": "RecommendationLatencyP95High",
        "service": "recommendation",
        "title": "Recommendation cache miss",
        "description": "Recommendation service slows down — every request bypasses the cache.",
        "category": "latency",
        "eta_seconds": 120,
    },
    "ad_manual_gc": {
        "flag": "adManualGc",
        "alert": "AdLatencyP95High",
        "service": "ad",
        "title": "Ad service GC stall",
        "description": "Ad service triggers manual GC pauses — p95 latency spikes.",
        "category": "latency",
        "eta_seconds": 120,
    },
    "image_slow_load_10s": {
        "flag": "imageSlowLoad",
        "variant_on": "10sec",
        "alert": "FrontendImageLatencyHigh",
        "service": "frontend",
        "title": "Slow image load (10 s)",
        "description": "Frontend image responses delayed 10 s — page-load p95 spikes.",
        "category": "latency",
        "eta_seconds": 120,
    },

    # ── Capacity / queue ────────────────────────────────────────────────────
    "loadgen_homepage_flood": {
        "flag": "loadGeneratorFloodHomepage",
        "alert": "FrontendTrafficSurge",
        "service": "frontend",
        "title": "Homepage traffic surge",
        "description": "Loadgenerator floods the homepage (> 10 req/s).",
        "category": "capacity",
        "eta_seconds": 60,
    },
    "kafka_backpressure": {
        "flag": "kafkaQueueProblems",
        "alert": "CheckoutBackpressureHigh",
        "service": "checkout",
        "title": "Kafka queue backpressure",
        "description": "Kafka consumer falls behind — checkout pipeline starts erroring out.",
        "category": "capacity",
        "eta_seconds": 120,
    },

    # ── Infra (no HTTP signal; the agent picks them up via secondary metrics) ─
    "email_memory_leak": {
        "flag": "emailMemoryLeak",
        "variant_on": "100x",
        "alert": "EmailMemoryHigh",
        "service": "email",
        "title": "Email service memory leak",
        "description": "Email service leaks memory (~100× normal growth rate).",
        "category": "infra",
        "eta_seconds": 180,
    },
    "ad_high_cpu": {
        "flag": "adHighCpu",
        "alert": "AdCpuHigh",
        "service": "ad",
        "title": "Ad service CPU saturation",
        "description": "Ad service pegs CPU above 80%.",
        "category": "infra",
        "eta_seconds": 120,
    },
}


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
        raise HTTPException(status_code=502, detail=f"kubectl error: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _load_flagd_config() -> dict[str, Any]:
    raw = _run_kubectl(
        [
            "get",
            "cm",
            "flagd-config",
            "-n",
            "otel-demo",
            "-o",
            "jsonpath={.data.demo\\.flagd\\.json}",
        ]
    )
    if not raw.strip():
        raise HTTPException(status_code=500, detail="flagd-config configmap key 'demo.flagd.json' is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"flagd config is not valid JSON: {exc}") from exc


def _toggle_flagd_flag(flag_name: str, variant: str) -> dict[str, Any]:
    """Set ``flags.<flag_name>.defaultVariant`` to ``variant``. The variant must
    be one of the variants declared for that flag in flagd-config (e.g. for
    ``paymentFailure`` valid values are ``100%``, ``90%``, ``75%``, ``50%``,
    ``25%``, ``10%``, ``off``). flagd watches the file and reloads ~1 s after
    the configmap is patched."""

    cfg = _load_flagd_config()
    flags = cfg.get("flags") or {}
    if flag_name not in flags:
        raise HTTPException(
            status_code=404,
            detail=f"flag {flag_name!r} not present in flagd config; available: {sorted(flags)}",
        )
    valid_variants = list((flags[flag_name].get("variants") or {}).keys())
    if variant not in valid_variants:
        raise HTTPException(
            status_code=400,
            detail=f"variant {variant!r} not valid for flag {flag_name!r}; "
                   f"choose one of {valid_variants}",
        )
    flags[flag_name]["defaultVariant"] = variant
    cfg["flags"] = flags

    # Patch via temp file (Windows command-line length limit makes inline -p brittle).
    patch_body = json.dumps({"data": {"demo.flagd.json": json.dumps(cfg)}})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(patch_body)
        patch_file = f.name
    try:
        _run_kubectl(
            ["patch", "cm", "flagd-config", "-n", "otel-demo", "--type=merge", "--patch-file", patch_file]
        )
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass

    return {
        "flag": flag_name,
        "variant": variant,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/scenarios")
def list_scenarios() -> dict[str, Any]:
    """List the available failure scenarios + their current variant in flagd."""
    out: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    try:
        cfg = _load_flagd_config()
        for fname, fdef in (cfg.get("flags") or {}).items():
            current[fname] = fdef.get("defaultVariant", "off")
    except HTTPException:
        pass  # fall back to assuming all 'off'
    for sid, s in SCENARIOS.items():
        out.append({
            **s,
            "scenario_id": sid,
            "current_variant": current.get(s["flag"], "off"),
        })
    return {"scenarios": out}


def _variant_on(s: dict[str, Any]) -> str:
    return str(s.get("variant_on") or "on")


@app.post("/api/scenarios/{scenario_id}/inject")
def inject_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag to its on-variant. Returns the expected alert
    name + ETA so the UI can poll ``/api/live-alerts`` until it fires."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}")
    result = _toggle_flagd_flag(s["flag"], _variant_on(s))
    return {**s, "scenario_id": scenario_id, **result, "expected_alert": s["alert"]}


@app.post("/api/scenarios/{scenario_id}/reset")
def reset_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag back to ``off``."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}")
    result = _toggle_flagd_flag(s["flag"], "off")
    return {**s, "scenario_id": scenario_id, **result}


@app.post("/api/scenarios/reset-all")
def reset_all_scenarios() -> dict[str, Any]:
    """Flip every scenario flag back to ``off`` in a single configmap patch.

    Cheaper than calling /reset on each scenario sequentially (one kubectl
    round-trip vs N), and atomic — flagd reloads once instead of N times.
    """
    cfg = _load_flagd_config()
    flags = cfg.get("flags") or {}
    touched: list[dict[str, str]] = []
    for s in SCENARIOS.values():
        fname = s["flag"]
        if fname not in flags:
            continue
        prev = flags[fname].get("defaultVariant", "off")
        if prev != "off":
            flags[fname]["defaultVariant"] = "off"
            touched.append({"flag": fname, "from": prev, "to": "off"})
    cfg["flags"] = flags

    if not touched:
        return {"reset_count": 0, "touched": [], "applied_at": datetime.now(timezone.utc).isoformat()}

    patch_body = json.dumps({"data": {"demo.flagd.json": json.dumps(cfg)}})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(patch_body)
        patch_file = f.name
    try:
        _run_kubectl(
            ["patch", "cm", "flagd-config", "-n", "otel-demo", "--type=merge", "--patch-file", patch_file]
        )
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass
    return {
        "reset_count": len(touched),
        "touched": touched,
        "applied_at": datetime.now(timezone.utc).isoformat(),
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
            for name in (svcs.json().get("data") or []):
                if name and "jaeger" not in name.lower():
                    nodes.append({"id": name, "label": name})

            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            try:
                deps = client.get(
                    f"{_JAEGER_URL}{_JAEGER_PREFIX}/api/dependencies",
                    params={"endTs": str(end_ms), "lookback": str(3600 * 1000)},
                )
                deps.raise_for_status()
                for d in deps.json().get("data") or []:
                    if d.get("parent") and d.get("child"):
                        edges.append({
                            "source": d["parent"],
                            "target": d["child"],
                            "call_count": int(d.get("callCount") or 0),
                        })
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
        rows.append({
            "name": name,
            "ready": ready,
            "status": status,
            "restarts": restarts_int,
            "age": age,
        })
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
                except Exception:  # noqa: BLE001 — client gone mid-send
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
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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


# Mount static files AFTER routes so /api/*, /ws/*, and / aren't overshadowed.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if DASHBOARD_DIST.exists():
    # html=True so client-side routes ('/dashboard/anything') fall back to index.html.
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(DASHBOARD_DIST), html=True),
        name="dashboard",
    )
