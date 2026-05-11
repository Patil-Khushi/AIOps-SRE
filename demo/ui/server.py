"""FastAPI service wrapping the Alert Triage agent for the demo UI.

Endpoints:

- ``GET  /``                       — serves ``static/index.html``
- ``GET  /api/health``             — liveness + agent + port-forward checks
- ``GET  /api/fixtures``           — list golden.json cases for the UI's fixture pane
- ``POST /api/triage``             — run the agent on a posted Alert payload
- ``POST /api/triage/fixture/{id}``— run the agent on a named fixture
- ``GET  /api/live-alerts``        — fetch currently firing Prometheus alerts
- ``POST /api/triage/live``        — triage every firing alert and return all verdicts

The agent runs in-process (single uvicorn worker = single dedup store) so the
embedding dedup memory persists across triage calls within one server lifetime.

Vendor neutrality: this module uses ``aiops.tools`` capabilities and
``agents.alert_triage`` only — it does not import Prometheus / Jaeger clients
directly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
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

SCENARIOS: dict[str, dict[str, Any]] = {
    "payment_failure": {
        "flag": "paymentFailure",
        "alert": "PaymentErrorRateHigh",
        "service": "payment",
        "title": "Payment failure (HTTP 500s)",
        "description": "Payment service starts returning 5xx errors.",
        "eta_seconds": 90,
    },
    "cart_failure": {
        "flag": "cartFailure",
        "alert": "CartErrorRateHigh",
        "service": "cart",
        "title": "Cart failure (HTTP 500s)",
        "description": "Cart service errors out on requests.",
        "eta_seconds": 90,
    },
    "product_catalog_failure": {
        "flag": "productCatalogFailure",
        "alert": "ProductCatalogErrorRateHigh",
        "service": "product-catalog",
        "title": "Product catalog failure",
        "description": "Product catalog returns errors on specific products.",
        "eta_seconds": 90,
    },
    "recommendation_cache_failure": {
        "flag": "recommendationCacheFailure",
        "alert": "RecommendationLatencyP95High",
        "service": "recommendation",
        "title": "Recommendation cache miss",
        "description": "Recommendation service slows down (p95 > 1 s).",
        "eta_seconds": 120,
    },
    "loadgen_homepage_flood": {
        "flag": "loadgeneratorFloodHomepage",
        "alert": "FrontendTrafficSurge",
        "service": "frontend",
        "title": "Homepage traffic surge",
        "description": "Loadgenerator floods the homepage (> 10 req/s).",
        "eta_seconds": 60,
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


def _toggle_flagd_flag(flag_name: str, variant: str) -> dict[str, Any]:
    """Set ``flags.<flag_name>.defaultVariant`` in the otel-demo's flagd-config
    configmap. flagd watches the file and reloads automatically (~1 s)."""
    if variant not in {"on", "off"}:
        raise HTTPException(status_code=400, detail=f"variant must be 'on' or 'off', not {variant!r}")

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
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"flagd config is not valid JSON: {exc}") from exc

    flags = cfg.get("flags") or {}
    if flag_name not in flags:
        raise HTTPException(
            status_code=404,
            detail=f"flag {flag_name!r} not present in flagd config; available: {sorted(flags)}",
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
    # Best-effort: read current variants from flagd-config so the UI can show state.
    current: dict[str, str] = {}
    try:
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
        cfg = json.loads(raw) if raw.strip() else {}
        for fname, fdef in (cfg.get("flags") or {}).items():
            current[fname] = fdef.get("defaultVariant", "off")
    except Exception:  # noqa: BLE001 — fall back to assuming all 'off'
        pass
    for sid, s in SCENARIOS.items():
        out.append({**s, "scenario_id": sid, "current_variant": current.get(s["flag"], "off")})
    return {"scenarios": out}


@app.post("/api/scenarios/{scenario_id}/inject")
def inject_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag to ``on``. Returns the expected alert name +
    ETA so the UI can poll ``/api/live-alerts`` until it fires."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}")
    result = _toggle_flagd_flag(s["flag"], "on")
    return {**s, "scenario_id": scenario_id, **result, "expected_alert": s["alert"]}


@app.post("/api/scenarios/{scenario_id}/reset")
def reset_scenario(scenario_id: str) -> dict[str, Any]:
    """Flip the scenario's flag back to ``off``."""
    s = SCENARIOS.get(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"unknown scenario; available: {list(SCENARIOS)}")
    result = _toggle_flagd_flag(s["flag"], "off")
    return {**s, "scenario_id": scenario_id, **result}


# Mount static files AFTER routes so /api/* and / aren't overshadowed.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
