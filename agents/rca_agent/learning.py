"""Phase 6 — closing the loop: recording what actually happened to an RCA prediction.

This is the only place memory is written from the live incident path, and it runs *after*
the resolution verifier has spoken. That ordering is the whole design: Phase 3 built a
store whose entries may only influence a ranking once recovery was confirmed, and a store
with no verified writer is a store that stays empty forever.

What "learning" means here, and what it deliberately does not
------------------------------------------------------------
It means exactly one thing: an outcome row, promoted by
:func:`investigation.memory.promote`, which a later recall may weight. That is the entire
mechanism.

It does **not** modify RCA source code, prompts, remediation logic, tool registrations, or
safety rules — not on one incident, not on a hundred. Those are code changes that belong
to a human with a diff and a review, and a system that edits its own prompt on the
strength of a single incident has no way to be rolled back by anyone who did not watch it
happen. ``tests/test_rca_learning.py`` asserts this structurally rather than trusting the
docstring: the module writes through the repository and nowhere else.

Both outcomes are recorded, and only one becomes memory
-------------------------------------------------------
A PASS records a ``resolved`` outcome, which :func:`promote` advances to ``VERIFIED`` and a
recall may use. A FAIL records a ``not_resolved`` outcome, which stays ``UNVERIFIED`` and
can never influence a ranking — but it is still written, because a prediction that was
approved, executed and did not work is the most informative record the system produces.
Discarding it would leave calibration measuring only the successes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _outcome_from_verdict(
    incident_id: str,
    *,
    service: str,
    verdict: dict[str, Any],
    verification_result: str,
    time_to_recovery_seconds: float | None,
) -> Any:
    """Build an ``RCAOutcome`` from a stored verdict. Imported lazily to keep this module
    importable from the verifier without pulling the investigation package on every call."""
    from agents.rca_agent.investigation.models import RCAOutcome, RootCauseStatus

    investigation = verdict.get("investigation") or {}
    matrices = investigation.get("matrices") or []
    selected_id = investigation.get("selected_hypothesis_id")

    def _class_of(entry: dict[str, Any]) -> str:
        return str((entry.get("hypothesis") or {}).get("category") or "")

    # The failure *class* is the cross-incident key; the id is scoped to this incident.
    # Resolving both here rather than in the store keeps the store schema-only.
    selected_class = next(
        (
            _class_of(m)
            for m in matrices
            if (m.get("hypothesis") or {}).get("hypothesis_id") == selected_id
        ),
        _class_of(matrices[0]) if matrices else "",
    )

    steps = [s for s in (verdict.get("ranked_fix_steps") or []) if isinstance(s, dict)]
    action_key = next((str(s.get("flag")) for s in steps if s.get("flag")), None)

    status_raw = str(verdict.get("root_cause_status") or "uncertain")
    try:
        status = RootCauseStatus(status_raw)
    except ValueError:
        status = RootCauseStatus.UNCERTAIN

    return RCAOutcome(
        incident_id=incident_id,
        affected_service=service or str(verdict.get("affected_service") or ""),
        recorded_at=datetime.now(UTC),
        predicted_root_cause=str(verdict.get("root_cause") or "")[:2000],
        predicted_status=status,
        confidence=float(verdict.get("confidence_score") or 0.0),
        selected_hypothesis_id=selected_id,
        selected_hypothesis_class=selected_class or None,
        supporting_evidence_ids=tuple(
            str(item.get("evidence_id"))
            for m in matrices[:1]
            for item in (m.get("supporting") or [])
            if isinstance(item, dict) and item.get("evidence_id")
        ),
        recommended_action=steps[0].get("description") if steps else None,
        action_key=action_key,
        # The gate already ran: an executable step only reaches the executor through the
        # HITL approval, so a verified recovery implies it was approved.
        human_decision="approved" if action_key else "not_requested",
        executed_action=action_key,
        verification_result=verification_result,  # type: ignore[arg-type]
        time_to_recovery_seconds=time_to_recovery_seconds,
        extra={"signatures": _signatures_from_investigation(investigation)},
    )


def _signatures_from_investigation(investigation: dict[str, Any]) -> list[str]:
    """Symptom identifiers for a later recall, taken from the evidence that was observed.

    Symptoms only — the alert name and the statements of the supporting evidence. Never the
    concluded cause: a memory keyed on the answer would let a recall retrieve the priors
    that agree with a conclusion already reached and call it corroboration.
    """
    out: list[str] = []
    scope = investigation.get("scope") or {}
    if scope.get("alert_name"):
        out.append(str(scope["alert_name"]))
    for matrix in (investigation.get("matrices") or [])[:1]:
        for item in (matrix.get("supporting") or [])[:6]:
            if isinstance(item, dict) and item.get("signature"):
                out.append(str(item["signature"]))
            elif isinstance(item, dict) and item.get("statement"):
                # Truncated: statements carry live readings ("gauge=0"), and an exact
                # numeric reading never recurs, so the whole string would match nothing.
                out.append(str(item["statement"]).split(":")[0][:80])
    seen: set[str] = set()
    unique: list[str] = []
    for sig in out:
        key = sig.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(sig.strip())
    return unique


def _verified_recurrences(service: str, hypothesis_class: str | None) -> int:
    """How many times this ``(service, class)`` pair has already been verified.

    Feeds ``promote``'s trust threshold. Counted from the store rather than tracked
    incrementally so it cannot drift from what is actually recorded.
    """
    if not hypothesis_class:
        return 0
    try:
        from aiops.state.repository import RECALLABLE_MEMORY_STATUSES, list_rca_outcomes

        rows = list_rca_outcomes(service=service, statuses=RECALLABLE_MEMORY_STATUSES, limit=200)
    except Exception:  # pragma: no cover - defensive
        return 0
    return sum(1 for r in rows if r.get("selected_hypothesis_class") == hypothesis_class)


def record_verified_outcome(
    incident_id: str,
    *,
    service: str = "",
    verification_result: str,
    time_to_recovery_seconds: float | None = None,
) -> int | None:
    """Record what happened to the RCA prediction for ``incident_id``. Never raises.

    Called from the resolution verifier once it has a verdict. Returns the row id, or
    ``None`` when there is nothing to record (no stored RCA verdict for this incident) or
    the write failed — recording is bookkeeping, and it must never cost the incident
    response that produced it.

    ``verification_result`` is passed straight through to
    :func:`investigation.memory.promote`, which decides whether the row is recallable.
    This function does not make that decision and deliberately has no way to override it.
    """
    try:
        from aiops.state.repository import get_rca_result

        stored = get_rca_result(incident_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("rca learning: could not read the stored verdict (%s)", exc)
        return None

    if not stored or not stored.get("verdict"):
        # Common and not an error: RCA is persisted at fix-apply time, so an incident that
        # never reached a proposed fix has no verdict to close the loop on.
        logger.debug("rca learning: no stored RCA verdict for %s", incident_id)
        return None

    verdict = dict(stored["verdict"])
    resolved_service = service or str(stored.get("affected_service") or "")

    try:
        from agents.rca_agent.investigation import memory

        outcome = _outcome_from_verdict(
            incident_id,
            service=resolved_service,
            verdict=verdict,
            verification_result=verification_result,
            time_to_recovery_seconds=time_to_recovery_seconds,
        )
        recurrences = (
            _verified_recurrences(resolved_service, outcome.selected_hypothesis_class)
            if verification_result == "resolved"
            else 0
        )
        row_id = memory.record_outcome(outcome, verified_recurrences=recurrences)
        logger.info(
            "rca learning: recorded %s outcome for %s as %s (row=%s)",
            verification_result,
            incident_id,
            memory.promote(outcome, verified_recurrences=recurrences).value,
            row_id,
        )
        return row_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("rca learning: outcome not recorded (%s)", exc)
        return None


def apply_human_correction(row_id: int, corrected_root_cause: str) -> dict[str, Any] | None:
    """Record that a human said the RCA was wrong, and what the real cause was.

    The highest-value feedback the system can receive, and the reason
    ``predicted_root_cause`` and ``human_corrected_root_cause`` are separate columns:
    overwriting the prediction would destroy the only evidence that the agent was wrong.

    **No automatic caller.** A correction needs a human, and there is no UI for it yet, so
    this is an API with a test and no production trigger — which is the honest state, not
    an oversight. Wiring it to an approval screen is UI work.
    """
    try:
        from sqlmodel import Session

        from agents.rca_agent.investigation.models import MemoryStatus
        from aiops.state import get_engine
        from aiops.state.models import RCAOutcomeRow

        with Session(get_engine()) as session:
            row = session.get(RCAOutcomeRow, row_id)
            if row is None:
                return None
            row.human_corrected_root_cause = corrected_root_cause
            # A correction is verified knowledge in its own right — see
            # ``RCAOutcome.eligible_for_memory`` — so the row becomes recallable. What it
            # teaches is that the *predicted* class was wrong here, which is why
            # ``memory._reliability_for`` counts a correction as a rejection.
            row.memory_status = MemoryStatus.VERIFIED.value
            session.add(row)
            session.commit()
            session.refresh(row)
        from aiops.state.repository import get_rca_outcome

        return get_rca_outcome(row_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("rca learning: correction not applied (%s)", exc)
        return None


def invalidate_outcome(row_id: int, *, reason: str = "") -> dict[str, Any] | None:
    """Retract one memory entry. Retained, never deleted.

    Deleting bad knowledge destroys the evidence that it was ever used to reach a
    conclusion, which is exactly what an audit of a wrong verdict needs.
    """
    try:
        from agents.rca_agent.investigation.models import MemoryStatus
        from aiops.state.repository import update_rca_outcome_memory_status

        return update_rca_outcome_memory_status(
            row_id, MemoryStatus.INVALIDATED.value, superseded_by=reason or None
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("rca learning: invalidation failed (%s)", exc)
        return None


__all__ = ["apply_human_correction", "invalidate_outcome", "record_verified_outcome"]
