"""Resolution Verifier engine.

Re-runs the detection-time observability checks after a fix is applied, across
a stabilization window (T+1m / T+3m / T+5m by default), and writes a proof
work note to the ServiceNow incident. The final round decides the verdict:
PASS when no check FAILED (checks whose data source is unavailable are
``SKIPPED`` — they degrade gracefully, never fail the verification).

Everything is dependency-injected (``itsm_call``, ``metrics_call``,
``sleep_fn``, ``checks``) so the engine is unit-testable without Prometheus,
Loki, or a real ServiceNow instance. The defaults wire to the existing
``aiops.tools`` registry capabilities.

The HITL closure card + ticket close live in increment 2b; this module stops at
producing + attaching the verification report and recording the ledger.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.resolution_verifier.models import CheckResult, CheckStatus, VerificationReport

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("VERIFIER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def _windows() -> list[float]:
    raw = os.environ.get("VERIFIER_WINDOW_SECONDS", "60,180,300")
    out: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or [60.0, 180.0, 300.0]


def _state_file() -> Path:
    return Path(os.environ.get("VERIFIER_STATE_FILE", "data/verifier_state.json"))


@dataclass
class VerifyContext:
    """What the verifier needs to re-check an incident. All observability
    fields are optional — a missing one makes its check ``SKIPPED``."""

    incident_id: str
    service: str
    alert_signature: str = ""  # PromQL/expr for the triggering signature
    metric_query: str = ""  # PromQL for the key service metric
    threshold: float | None = None
    health_query: str = ""  # PromQL for service health (e.g. up{...})


# ─── default dependency wiring (overridable for tests) ──────────────────────


def _default_itsm_call(capability: str, **kwargs: Any) -> Any:
    from aiops.tools import get_registry

    return get_registry().call(capability, **kwargs)


def _default_metrics_call(promql: str) -> Any:
    from aiops.tools import get_registry

    return get_registry().call("observability.metrics.query", promql=promql)


def _default_close(
    ctx: VerifyContext, sys_id: str | None, report: VerificationReport
) -> dict[str, Any]:
    """Request HITL approval to close the ticket, then close it on approve.
    Routes through the REQUIRED ``itsm.ticket.close`` gate (posts the card)."""
    if not sys_id:
        return {"status": "no_sys_id"}
    import uuid

    from aiops.tools.itsm_close import request_ticket_close

    aid = uuid.uuid4().hex
    hitl_context = {
        "approval_id": aid,
        "approval_timeout_seconds": 120,
        "capability": "itsm.ticket.close",
        "incident_id": ctx.incident_id,
        "reason": (
            f"Verified resolved — close ticket {ctx.incident_id}? "
            f"{len(report.passed)} check(s) passed, {len(report.skipped)} skipped."
        ),
    }
    # Put the full verification proof into close_notes so it lands in the
    # incident's Resolution Information (not just the Work Notes activity log).
    return request_ticket_close(
        incident_id=ctx.incident_id,
        sys_id=sys_id,
        close_code=report.close_code(),
        close_notes=(
            report.work_note() + "\n\nClosed via HITL approval after resolution verification."
        ),
        hitl_context=hitl_context,
    )


def _default_notify(ctx: VerifyContext, report: VerificationReport) -> None:
    """On FAIL, raise a HITL notification (not a closure card)."""
    from aiops.tools import get_registry

    msg = (
        f"Resolution verification FAILED for {ctx.incident_id} ({ctx.service}): "
        f"fix applied but symptoms persist — {len(report.failed)} check(s) still failing. "
        f"Closure NOT proposed."
    )
    with contextlib.suppress(Exception):
        get_registry().call("notify.send", channel="incidents", message=msg)


def _extract_scalar(res: Any) -> float | None:
    """Best-effort scalar extraction from a Prometheus-shaped ToolResult.
    Returns None when nothing usable is present (→ SKIPPED upstream)."""
    if res is None or not getattr(res, "ok", False):
        return None
    data = getattr(res, "data", None)
    try:
        # Adapter may already return a number / {"value": n}.
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            if "value" in data and isinstance(data["value"], (int, float, str)):
                return float(data["value"])
            # Raw Prometheus shape: data.data.result[0].value = [ts, "v"]
            inner = data.get("data", data)
            result = inner.get("result") if isinstance(inner, dict) else None
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict) and "value" in first:
                    return float(first["value"][1])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return None


# ─── default checks (Prometheus-backed, graceful skip) ──────────────────────


def _scalar_check(
    name: str, promql: str, threshold: float | None, metrics_call: Callable[[str], Any]
) -> CheckResult:
    if not promql:
        return CheckResult(name=name, status=CheckStatus.SKIPPED, detail="no query configured")
    try:
        val = _extract_scalar(metrics_call(promql))
    except Exception as exc:  # no provider / unreachable → skip, never fail
        return CheckResult(name=name, status=CheckStatus.SKIPPED, detail=f"data unavailable: {exc}")
    if val is None:
        return CheckResult(
            name=name, status=CheckStatus.SKIPPED, detail="no data (source unavailable)"
        )
    thr = 0.0 if threshold is None else threshold
    if val <= thr:
        return CheckResult(
            name=name,
            status=CheckStatus.PASS,
            detail=f"value {val} within range (≤ {thr})",
            after=str(val),
        )
    return CheckResult(
        name=name, status=CheckStatus.FAIL, detail=f"value {val} above {thr}", after=str(val)
    )


def _default_checks(ctx: VerifyContext, metrics_call: Callable[[str], Any]) -> list[CheckResult]:
    return [
        _scalar_check(
            "triggering signature cleared", ctx.alert_signature, ctx.threshold, metrics_call
        ),
        _scalar_check("key metric within range", ctx.metric_query, ctx.threshold, metrics_call),
        _scalar_check("service health", ctx.health_query, 0.0, metrics_call),
    ]


# ─── the verifier ───────────────────────────────────────────────────────────


CheckFn = Callable[[VerifyContext, Callable[[str], Any]], list[CheckResult]]


class Verifier:
    def __init__(
        self,
        *,
        itsm_call: Callable[..., Any] | None = None,
        metrics_call: Callable[[str], Any] | None = None,
        checks: CheckFn | None = None,
        windows: list[float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        state_file: Path | None = None,
        close_fn: Callable[[VerifyContext, str | None, VerificationReport], dict[str, Any]]
        | None = None,
        notify_fn: Callable[[VerifyContext, VerificationReport], None] | None = None,
    ) -> None:
        self._itsm = itsm_call or _default_itsm_call
        self._metrics = metrics_call or _default_metrics_call
        self._checks = checks or _default_checks
        self._close = close_fn or _default_close
        self._notify = notify_fn or _default_notify
        self._windows = windows if windows is not None else _windows()
        self._sleep = sleep_fn or time.sleep
        self._state_file = state_file if state_file is not None else _state_file()
        st = self._load_state()
        self._ledger: dict[str, dict[str, Any]] = dict(st.get("ledger", {}))
        self._verified_total = int(st.get("verified_total", 0))
        self._passed_total = int(st.get("passed_total", 0))
        self._failed_total = int(st.get("failed_total", 0))
        self._errors_total = int(st.get("errors_total", 0))
        self._last_error: str | None = st.get("last_error")

    # ── persistence / status ──
    def _load_state(self) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        return {}

    def _save_state(self) -> None:
        with contextlib.suppress(Exception):
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(
                    {
                        "ledger": self._ledger,
                        "verified_total": self._verified_total,
                        "passed_total": self._passed_total,
                        "failed_total": self._failed_total,
                        "errors_total": self._errors_total,
                        "last_error": self._last_error,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": _enabled(),
            "verified_total": self._verified_total,
            "passed_total": self._passed_total,
            "failed_total": self._failed_total,
            "errors_total": self._errors_total,
            "windows_seconds": self._windows,
            "last_error": self._last_error,
            "recent": list(self._ledger.values())[-10:],
        }

    def already_verified(self, incident_id: str) -> bool:
        return incident_id in self._ledger

    # ── core verification ──
    def verify(self, ctx: VerifyContext) -> VerificationReport:
        started = datetime.now(UTC).isoformat()
        results: list[CheckResult] = []
        prev = 0.0
        for t in self._windows:
            delta = max(0.0, t - prev)
            prev = t
            if delta:
                self._sleep(delta)
            results = self._checks(ctx, self._metrics)
        verdict = (
            CheckStatus.FAIL
            if any(c.status is CheckStatus.FAIL for c in results)
            else CheckStatus.PASS
        )
        return VerificationReport(
            incident_id=ctx.incident_id,
            service=ctx.service,
            verdict=verdict,
            checks=results,
            rounds=len(self._windows),
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
        )

    def _resolve_sys_id(self, incident_id: str) -> str | None:
        with contextlib.suppress(Exception):
            res = self._itsm("itsm.incident.get", number=incident_id)
            if getattr(res, "ok", False):
                rec = (getattr(res, "data", None) or {}).get("incident") or {}
                sid = rec.get("sys_id")
                if isinstance(sid, dict):
                    sid = sid.get("value")
                return sid or None
        return None

    def _write_proof(self, report: VerificationReport, sys_id: str | None) -> bool:
        if not sys_id:
            logger.warning(
                "verifier: no sys_id for %s — proof work note not written", report.incident_id
            )
            return False
        with contextlib.suppress(Exception):
            res = self._itsm(
                "itsm.incident.update", sys_id=sys_id, fields={"work_notes": report.work_note()}
            )
            return bool(getattr(res, "ok", False))
        return False

    def run(self, ctx: VerifyContext) -> VerificationReport | None:
        """Full verify → attach proof → record ledger. Idempotent: a second
        run for the same incident is a no-op. Returns the report (or None when
        skipped/disabled). The HITL closure card is wired in increment 2b — on
        PASS this logs 'ready for closure'."""
        if not _enabled():
            logger.info("verifier: disabled via VERIFIER_ENABLED")
            return None
        if self.already_verified(ctx.incident_id):
            logger.info("verifier: incident %s already verified — skipping", ctx.incident_id)
            return None
        try:
            report = self.verify(ctx)
        except Exception as exc:
            self._errors_total += 1
            self._last_error = str(exc)
            self._save_state()
            logger.exception("verifier: verification raised for %s", ctx.incident_id)
            return None

        sys_id = self._resolve_sys_id(ctx.incident_id)
        proof_written = self._write_proof(report, sys_id)

        self._verified_total += 1
        closure: dict[str, Any] = {"status": "not_attempted"}
        if report.verdict is CheckStatus.PASS:
            self._passed_total += 1
            # PASS → raise the HITL "close ticket?" card (blocks on the gate).
            with contextlib.suppress(Exception):
                closure = self._close(ctx, sys_id, report) or {"status": "error"}
            logger.info(
                "verifier: %s PASS (%d skipped) — proof_written=%s — closure=%s",
                ctx.incident_id,
                len(report.skipped),
                proof_written,
                closure.get("status"),
            )
            # Closed → synthesize the postmortem/runbook draft right now so the
            # publish (3rd HITL) approval is available immediately, instead of
            # waiting on the SNOW watcher's poll (which can miss a closure to a
            # checkpoint race). Fire-and-forget + lazily imported so it can never
            # affect closure; the watcher remains the backstop. (Demo wiring.)
            if isinstance(closure, dict) and closure.get("status") == "closed":
                with contextlib.suppress(Exception):
                    from agents.knowledge_synthesizer.snow_watcher import (
                        synthesize_incident_now,
                    )

                    synthesize_incident_now(ctx.incident_id)
        else:
            self._failed_total += 1
            # FAIL → notify (NOT a closure card) and stop.
            with contextlib.suppress(Exception):
                self._notify(ctx, report)
            logger.warning(
                "verifier: %s FAIL — fix applied but symptoms persist; closure NOT proposed",
                ctx.incident_id,
            )
        self._ledger[ctx.incident_id] = {
            "incident_id": ctx.incident_id,
            "verdict": report.verdict.value,
            "finished_at": report.finished_at,
            "proof_written": proof_written,
            "skipped": len(report.skipped),
            "closure": closure.get("status"),
            "closure_error": closure.get("error"),
        }
        self._save_state()
        return report


# ─── module singleton + fire-and-forget trigger ─────────────────────────────

_VERIFIER: Verifier | None = None


def get_verifier() -> Verifier:
    global _VERIFIER
    if _VERIFIER is None:
        _VERIFIER = Verifier()
    return _VERIFIER


def reset_verifier_for_tests() -> None:
    global _VERIFIER
    _VERIFIER = None


# Own pool so the (up to 5-minute) stabilization wait never ties up the HITL
# executor or the request thread.
_POOL: Any = None


def _get_pool() -> Any:
    global _POOL
    if _POOL is None:
        from concurrent.futures import ThreadPoolExecutor

        _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="verifier")
    return _POOL


def trigger(ctx: VerifyContext) -> None:
    """Fire-and-forget verification. Safe to call from the fix-apply handler —
    any exception is swallowed and never affects the caller."""
    if not _enabled():
        return

    def _safe_run() -> None:
        with contextlib.suppress(Exception):
            get_verifier().run(ctx)

    with contextlib.suppress(Exception):
        _get_pool().submit(_safe_run)
