"""ServiceNow resolved-ticket watcher → triggers the Knowledge Synthesizer.

A background poller that, every ``SNOW_WATCHER_POLL_SECONDS`` (default 45),
asks ServiceNow (through the existing ``aiops.tools`` registry — capability
``itsm.incident.query``) for incidents in state Resolved (6) or Closed (7)
updated since the last checkpoint, and synthesizes knowledge for each newly
resolved one.

Design constraints (additive + decoupled):
- Imports only ``agents`` / ``aiops`` — never the demo server. Started from the
  server's lifespan alongside the auto-triage loop.
- Calls the SAME synthesis entry point the manual ``POST /api/synthesize`` uses
  (``agents.knowledge_synthesizer.agent.run``); no logic duplicated.
- Fire-and-forget: any exception in polling or synthesis is logged and
  swallowed. A 5-consecutive-failure circuit breaker backs the poll interval
  off to 5 minutes until ServiceNow recovers.
- Idempotent: the existing KB ledger (``find_kb_by_incident_id``) skips tickets
  already synthesized; the persisted checkpoint stops a restart re-scanning.

Config:
- ``SNOW_WATCHER_ENABLED``       — "true"/"false" (default true).
- ``SNOW_WATCHER_POLL_SECONDS``  — base poll interval (default 45).
- ``SNOW_WATCHER_STATE_FILE``    — checkpoint+counters JSON (default
                                   ``data/snow_watcher_state.json``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops.state import repository as repo

logger = logging.getLogger(__name__)

# ServiceNow incident states we treat as "resolved".
_RESOLVED_STATES = "6,7"  # 6=Resolved, 7=Closed
_FIELDS = (
    "number,sys_id,state,sys_updated_on,resolved_at,closed_at,opened_at,"
    "short_description,description,cmdb_ci"
)
_CIRCUIT_THRESHOLD = 5
_BACKOFF_SECONDS = 300.0


def _enabled() -> bool:
    return os.environ.get("SNOW_WATCHER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def _base_interval() -> float:
    try:
        return float(os.environ.get("SNOW_WATCHER_POLL_SECONDS", "45"))
    except ValueError:
        return 45.0


def _state_file() -> Path:
    return Path(os.environ.get("SNOW_WATCHER_STATE_FILE", "data/snow_watcher_state.json"))


# ─── default dependency wiring (overridable for tests) ──────────────────────


def _default_itsm_call(query: str, fields: str, limit: int) -> Any:
    from aiops.tools import get_registry

    return get_registry().call("itsm.incident.query", query=query, fields=fields, limit=limit)


def _default_synthesize(bundle: dict[str, Any]) -> dict[str, Any]:
    from agents.knowledge_synthesizer.agent import run

    return run(bundle)


# ─── bundle reconstruction ──────────────────────────────────────────────────


def _derive_service(incident: dict[str, Any]) -> str:
    ci = incident.get("cmdb_ci")
    if isinstance(ci, dict):
        ci = ci.get("display_value")
    if isinstance(ci, str) and ci.strip():
        return ci.strip()
    sd = str(incident.get("short_description") or "")
    return sd.strip() or "unknown"


def _minimal_rca(service: str, resolved_at: str | None) -> dict[str, Any]:
    return {
        "affected_service": service,
        "root_cause": "Incident resolved outside the automated pipeline; no RCA on record.",
        "ranked_fix_steps": [],
        "confidence_score": 0.0,
        "audit_metadata": {"created_at": resolved_at, "created_by": "snow_watcher"},
    }


def _build_bundle(incident: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return (synthesis bundle, source) for a resolved incident.

    ``source`` is ``pipeline`` when a stored RCA result exists, else
    ``ticket_only`` (degraded, ticket-derived bundle)."""
    number = str(incident.get("number") or "")
    sys_id = incident.get("sys_id")
    resolved_at = (
        incident.get("resolved_at") or incident.get("closed_at") or incident.get("sys_updated_on")
    )
    short_desc = str(incident.get("short_description") or "") or f"Incident {number}"
    service = _derive_service(incident)

    stored = repo.get_rca_result(number)
    if stored and stored.get("verdict"):
        rca_verdict = stored["verdict"]
        service = rca_verdict.get("affected_service") or service
        source = "pipeline"
    else:
        rca_verdict = _minimal_rca(service, resolved_at)
        source = "ticket_only"

    bundle = {
        "incident_id": number,
        "resolved_at": resolved_at,
        "triage_verdict": {
            "affected_service": service,
            "severity": "Sev-3",
            "alert_summary": short_desc,
            "audit_metadata": {"created_at": incident.get("opened_at") or resolved_at},
        },
        "rca_verdict": rca_verdict,
        "ticket": {"external_id": number, "sys_id": sys_id, "system": "servicenow"},
        "change_records": [],
    }
    return bundle, source


def synthesize_incident_now(number: str) -> dict[str, Any] | None:
    """Synthesize knowledge for ONE resolved incident on demand.

    Lets the closure path (resolution verifier) trigger synthesis the instant a
    ticket is closed — so the KB draft appears immediately for the publish
    (3rd HITL) approval, instead of waiting on the poll loop (which can miss a
    closure to a checkpoint/ordering race when several tickets resolve at once).

    Idempotent via the KB ledger; fire-and-forget safe (returns ``None`` on any
    failure, never raises). Reuses the same bundle reconstruction + synthesis
    entry point as the poller, so there is no duplicated logic and the watcher
    stays the backstop for anything this misses.
    """
    try:
        if not number or repo.find_kb_by_incident_id(number) is not None:
            return None
        res = _default_itsm_call(f"number={number}", _FIELDS, 1)
        if not getattr(res, "ok", False):
            return None
        rows = (getattr(res, "data", None) or {}).get("incidents", []) or []
        if not rows:
            return None
        # Defensive: only synthesize a genuinely closed ticket. Callers (the
        # verifier's close branch) already guarantee this, but a stray call must
        # not draft for an open incident and skip the close-ticket approval.
        state = str(rows[0].get("state") or "")
        if state not in {"6", "7"}:
            logger.info(
                "synthesize_incident_now: %s not Resolved/Closed (state=%r) — skipping",
                number,
                state,
            )
            return None
        bundle, source = _build_bundle(rows[0])
        logger.info("synthesize_incident_now: %s (source=%s)", number, source)
        return _default_synthesize(bundle)
    except Exception:
        logger.exception("synthesize_incident_now failed for %s", number)
        return None


# ─── the watcher ────────────────────────────────────────────────────────────


class _SnowWatcher:
    """Resolved-ticket poller. ``poll_once`` is sync + side-effect-complete so
    it can be unit-tested without an event loop; ``start``/``stop`` drive it on
    a background asyncio task."""

    def __init__(
        self,
        *,
        interval_seconds: float | None = None,
        itsm_call: Callable[[str, str, int], Any] | None = None,
        synthesize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        state_file: Path | None = None,
    ) -> None:
        self._base = interval_seconds if interval_seconds is not None else _base_interval()
        self._current_interval = self._base
        self._itsm_call = itsm_call or _default_itsm_call
        self._synthesize = synthesize or _default_synthesize
        self._state_file = state_file if state_file is not None else _state_file()
        self._task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0
        loaded = self._load_state()
        self._checkpoint: str | None = loaded.get("checkpoint")
        self._processed_total: int = int(loaded.get("processed_total", 0))
        self._errors_total: int = int(loaded.get("errors_total", 0))
        self._last_poll: str | None = loaded.get("last_poll")
        self._last_error: str | None = loaded.get("last_error")

    # ── persistence ──
    def _load_state(self) -> dict[str, Any]:
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:  # corrupt file shouldn't break startup
            logger.warning("snow_watcher: could not read state file: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(
                    {
                        "checkpoint": self._checkpoint,
                        "processed_total": self._processed_total,
                        "errors_total": self._errors_total,
                        "last_poll": self._last_poll,
                        "last_error": self._last_error,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("snow_watcher: could not persist state file: %s", exc)

    # ── status ──
    def status(self) -> dict[str, Any]:
        return {
            "enabled": _enabled(),
            "checkpoint": self._checkpoint,
            "processed_total": self._processed_total,
            "errors_total": self._errors_total,
            "consecutive_failures": self._consecutive_failures,
            "backed_off": self._current_interval > self._base,
            "poll_interval_seconds": self._current_interval,
            "last_poll": self._last_poll,
            "last_error": self._last_error,
            "running": self._task is not None and not self._task.done(),
        }

    # ── failure / circuit breaker ──
    def _record_failure(self, msg: str) -> None:
        self._consecutive_failures += 1
        self._errors_total += 1
        self._last_error = msg
        logger.warning(
            "snow_watcher: poll failed (%d in a row): %s", self._consecutive_failures, msg
        )
        if (
            self._consecutive_failures >= _CIRCUIT_THRESHOLD
            and self._current_interval < _BACKOFF_SECONDS
        ):
            self._current_interval = _BACKOFF_SECONDS
            logger.warning(
                "snow_watcher: circuit open — backing off to %.0fs polls until ServiceNow recovers",
                _BACKOFF_SECONDS,
            )

    def _record_success(self) -> None:
        if self._consecutive_failures or self._current_interval != self._base:
            logger.info("snow_watcher: ServiceNow recovered; resuming %.0fs polls", self._base)
        self._consecutive_failures = 0
        self._current_interval = self._base
        self._last_error = None

    def _query(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        res = self._itsm_call(query, _FIELDS, limit)
        ok = getattr(res, "ok", False)
        if not ok:
            raise RuntimeError(f"itsm.incident.query failed: {getattr(res, 'error', 'unknown')}")
        data = getattr(res, "data", None) or {}
        return list(data.get("incidents", []) or [])

    # ── one poll cycle (sync, fully testable) ──
    def poll_once(self) -> dict[str, Any]:
        self._last_poll = datetime.now(UTC).isoformat()
        try:
            # First run: anchor the checkpoint to the newest resolved ticket so
            # we don't synthesize the entire historical backlog. Process nothing.
            if self._checkpoint is None:
                newest = self._query(
                    f"stateIN{_RESOLVED_STATES}^ORDERBYDESCsys_updated_on", limit=1
                )
                self._checkpoint = (newest[0].get("sys_updated_on") if newest else "") or ""
                self._record_success()
                self._save_state()
                logger.info("snow_watcher: initialized checkpoint=%r", self._checkpoint)
                return {"initialized": True, "checkpoint": self._checkpoint, "processed": 0}

            cp = self._checkpoint
            query = f"stateIN{_RESOLVED_STATES}"
            if cp:
                query += f"^sys_updated_on>{cp}"
            query += "^ORDERBYsys_updated_on"
            rows = self._query(query)
            self._record_success()
        except Exception as exc:
            self._record_failure(str(exc))
            self._save_state()
            return {"error": str(exc)}

        processed = 0
        for incident in rows:
            try:
                if self._process(incident):
                    processed += 1
            except Exception as exc:  # one bad ticket must not stop the rest
                self._errors_total += 1
                self._last_error = str(exc)
                logger.exception(
                    "snow_watcher: failed to synthesize incident %s", incident.get("number")
                )
            # Advance checkpoint regardless so a poison ticket isn't retried forever.
            updated = incident.get("sys_updated_on")
            if isinstance(updated, str) and (
                self._checkpoint is None or updated > self._checkpoint
            ):
                self._checkpoint = updated

        self._processed_total += processed
        self._save_state()
        return {"processed": processed, "scanned": len(rows), "checkpoint": self._checkpoint}

    def _process(self, incident: dict[str, Any]) -> bool:
        number = str(incident.get("number") or "")
        if not number:
            return False
        if repo.find_kb_by_incident_id(number) is not None:
            logger.info("snow_watcher: incident %s already synthesized — skipping", number)
            return False
        bundle, source = _build_bundle(incident)
        logger.info("snow_watcher: ticket %s resolved → synthesizing (source=%s)", number, source)
        result = self._synthesize(bundle)
        kb_id = result.get("kb_article_id") if isinstance(result, dict) else None
        if kb_id and source == "ticket_only":
            with contextlib.suppress(Exception):
                repo.tag_kb_article_source(int(kb_id), "ticket_only")
        return True

    # ── async loop ──
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="snow-watcher")

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        logger.info("snow_watcher: started (interval=%.0fs)", self._current_interval)
        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.poll_once)
            try:
                await asyncio.sleep(self._current_interval)
            except asyncio.CancelledError:
                logger.info("snow_watcher: stopped")
                return


# ─── module singleton + server hooks ────────────────────────────────────────

_WATCHER: _SnowWatcher | None = None


def get_watcher() -> _SnowWatcher:
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = _SnowWatcher()
    return _WATCHER


async def start_watcher() -> None:
    """Called from the demo server's lifespan. No-op when disabled."""
    if not _enabled():
        logger.info("snow_watcher: disabled via SNOW_WATCHER_ENABLED")
        return
    get_watcher().start()


async def stop_watcher() -> None:
    if _WATCHER is not None:
        await _WATCHER.stop()


def reset_watcher_for_tests() -> None:
    global _WATCHER
    _WATCHER = None
