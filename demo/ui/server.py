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
- ``WS   /ws/chatops``             — push chatops notifications (RA-005 sink) to the dashboard

The agent runs in-process (single uvicorn worker = single dedup store) so the
embedding dedup memory persists across triage calls within one server lifetime.

Vendor neutrality: this module uses ``aiops.tools`` capabilities and
``agents.alert_triage`` only — it does not import Prometheus / Jaeger clients
directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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
from agents.alert_triage import Alert, TriageVerdict, triage  # noqa: E402
from agents.auto_ticketing import ticket as auto_ticket  # noqa: E402
from agents.incident_classifier import ClassificationInput, classify  # noqa: E402
from agents.notification_router import route as route_notification  # noqa: E402
from aiops.state import init_db  # noqa: E402
from aiops.state import repository as state_repo  # noqa: E402
from aiops.tools import get_registry  # noqa: E402
from aiops.tools.alerts.prometheus_adapter import to_canonical_alert  # noqa: E402
from aiops.tools.chatops import get_client as get_chatops_client  # noqa: E402
from aiops.tools.chatops.adapters.jsonfile import JsonFileChatOpsAdapter  # noqa: E402
from demo.ui.chatops_ws import register_routes as _register_chatops_ws_routes  # noqa: E402

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
FIXTURES_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "alert_triage" / "evals" / "golden.json"
)

app = FastAPI(title="Adaptive AIOps — Alert Triage demo", version="0.1.0")


@app.on_event("startup")
def _bootstrap_state() -> None:
    init_db()


@app.on_event("startup")
def _register_chatops_adapters() -> None:
    """JSON audit log (D3). The WebSocket sink (D2) registers itself via
    ``_register_chatops_ws_routes`` below."""
    audit_path = Path(__file__).resolve().parents[2] / "demo" / "audit" / "chatops.jsonl"
    get_chatops_client().register(JsonFileChatOpsAdapter(audit_path))
    logger.info("chatops: registered jsonfile adapter -> %s", audit_path)


_register_chatops_ws_routes(app)


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
    except Exception:
        pass
    try:
        res = registry.call("observability.traces.services")
        jaeger_ok = bool(res.ok)
    except Exception:
        pass

    return {
        "status": "ok",
        "llm_provider": os.environ.get("AIOPS_LLM_PROVIDER"),
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


@app.post("/api/triage", response_model=None)
def triage_alert(req: TriageRequest) -> dict[str, Any]:
    """Triage + auto-ticket + classify + notify chatops for a single alert.

    Body: ``{"alert": {<Alert payload>}}``. Pipeline:
    parse → RA-001 triage → persist verdict → RA-003 auto-ticket →
    RA-002 classify → persist classification → RA-005 chatops notify.
    The chatops fan-out is a side effect: a routing failure is logged but
    the response still returns 200 with verdict/ticket/classification populated.

    Response: ``{"verdict": TriageVerdict, "ticket": TicketRecord,
    "classification": Classification, "persisted": {verdict_id, classification_id}}``.
    """
    try:
        alert_obj = Alert(**req.alert)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid alert: {exc}") from exc

    verdict: TriageVerdict = triage(alert_obj)
    verdict_id = state_repo.save_verdict(verdict, cluster_key=alert_obj.cluster_key())

    ticket_record = auto_ticket(verdict)

    classification = classify(ClassificationInput(alert=alert_obj, triage_verdict=verdict))
    classification_id = state_repo.save_classification(classification, verdict_id=verdict_id)

    try:
        route_notification(verdict)
    except Exception:
        logger.exception("RA-005: routing failed for verdict on %s", verdict.affected_service)

    return {
        "verdict": verdict.model_dump(mode="json"),
        "ticket": ticket_record.model_dump(mode="json"),
        "classification": classification.model_dump(mode="json"),
        "persisted": {
            "verdict_id": verdict_id,
            "classification_id": classification_id,
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
    """Read every ``demo/scenarios/*.yaml`` into a dict keyed by id.

    The dict key is the scenario id (also the filename stem); the value
    is the rest of the YAML record. We pop ``id`` from the value because
    it is already the key — keeping it both places would risk drift.
    Iteration order is alphabetical by filename; the UI then groups by
    ``category`` so the on-screen layout is stable regardless of fs order.
    """
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"scenario file {path.name} must be a YAML mapping")
        sid = data.pop("id", None)
        if sid != path.stem:
            raise RuntimeError(
                f"scenario file {path.name}: 'id' must equal filename stem {path.stem!r}, got {sid!r}"
            )
        out[sid] = data
    return out


SCENARIOS: dict[str, dict[str, Any]] = _load_scenarios()


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
        raise HTTPException(
            status_code=500, detail="flagd-config configmap key 'demo.flagd.json' is empty"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500, detail=f"flagd config is not valid JSON: {exc}"
        ) from exc


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
            [
                "patch",
                "cm",
                "flagd-config",
                "-n",
                "otel-demo",
                "--type=merge",
                "--patch-file",
                patch_file,
            ]
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(patch_file)

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
        cfg = _load_flagd_config()
        for fname, fdef in (cfg.get("flags") or {}).items():
            current[fname] = fdef.get("defaultVariant", "off")
    except HTTPException:
        pass  # fall back to assuming all 'off'
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
        return {"reset_count": 0, "touched": [], "applied_at": datetime.now(UTC).isoformat()}

    patch_body = json.dumps({"data": {"demo.flagd.json": json.dumps(cfg)}})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(patch_body)
        patch_file = f.name
    try:
        _run_kubectl(
            [
                "patch",
                "cm",
                "flagd-config",
                "-n",
                "otel-demo",
                "--type=merge",
                "--patch-file",
                patch_file,
            ]
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(patch_file)
    return {
        "reset_count": len(touched),
        "touched": touched,
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
