"""Simulation detail + simulation-vs-execution comparison for RA-004 (issue #213).

The dry-run preview (``automation.runbook.simulate``) predicts what a step *would*
do; the real execution (``automation.runbook.apply`` / ``.execute``) reports what
it *did*. This module gives both a typed shape and computes the structured diff
between them, so the comparison is part of the audit trail rather than derived
ad-hoc later.

Scope (this pass): the diff covers **predicted vs. actual side effects** and
**estimated vs. actual duration** — the two dimensions the execution result can
supply. ``predicted_actions`` / ``warnings`` / ``summary`` are captured in full
on :class:`SimulationDetail` but not diffed (no "actual" counterpart is emitted
by the executor yet; predicted-actions diffing is a possible follow-up).

Everything here is pure and deterministic — no I/O, no gate, no LLM.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SimulationDetail(BaseModel):
    """The full prediction captured from a step's dry-run preview."""

    predicted_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimated_duration_ms: int | None = None
    predicted_side_effects: list[str] = Field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_provider(cls, data: dict[str, Any] | None) -> SimulationDetail:
        """Tolerant parse of a ``simulate`` ToolResult's ``data`` payload.

        Missing fields degrade to empty defaults so an un-enriched provider (or
        a failed simulation whose ``data`` is ``{"error": ...}``) still yields a
        valid object. ``summary`` falls back to the provider's ``preview`` line
        when no explicit summary is present.
        """
        data = data or {}
        return cls(
            predicted_actions=list(data.get("predicted_actions", []) or []),
            warnings=list(data.get("warnings", []) or []),
            estimated_duration_ms=data.get("estimated_duration_ms"),
            predicted_side_effects=list(data.get("predicted_side_effects", []) or []),
            summary=str(data.get("summary", data.get("preview", "")) or ""),
        )


class SimulationComparison(BaseModel):
    """Structured diff of what the simulation predicted vs. what execution did.

    ``matched`` is side-effect parity only: ``True`` when no side effect was
    unexpected and none predicted went missing. Duration is reported as a delta
    but does not affect ``matched`` (timing drift is expected, not a divergence).
    """

    matched: bool
    divergences: list[str] = Field(default_factory=list)
    predicted_side_effects: list[str] = Field(default_factory=list)
    actual_side_effects: list[str] = Field(default_factory=list)
    unexpected_side_effects: list[str] = Field(default_factory=list)  # actual − predicted
    missing_side_effects: list[str] = Field(default_factory=list)  # predicted − actual
    estimated_duration_ms: int | None = None
    actual_duration_ms: int | None = None
    duration_delta_ms: int | None = None


def compare_simulation(
    sim: SimulationDetail | None, executed: dict[str, Any] | None
) -> SimulationComparison:
    """Diff a step's prediction against its actual execution result.

    ``executed`` is the ``ToolResult.data`` from the apply/execute call (or the
    ``{"error": ...}`` dict on failure). Reads ``actual_side_effects`` and
    ``duration_ms`` from it; both absent → empty/None, and the step simply
    reports side-effect parity against whatever was predicted.
    """
    sim = sim or SimulationDetail()
    executed = executed or {}

    predicted_se = list(sim.predicted_side_effects)
    actual_se = list(executed.get("actual_side_effects", []) or [])
    unexpected = [s for s in actual_se if s not in predicted_se]
    missing = [s for s in predicted_se if s not in actual_se]

    actual_dur = executed.get("duration_ms")
    est_dur = sim.estimated_duration_ms
    delta = actual_dur - est_dur if actual_dur is not None and est_dur is not None else None

    divergences: list[str] = []
    if unexpected:
        divergences.append(f"unexpected side effects not predicted: {unexpected}")
    if missing:
        divergences.append(f"predicted side effects did not occur: {missing}")

    return SimulationComparison(
        matched=not unexpected and not missing,
        divergences=divergences,
        predicted_side_effects=predicted_se,
        actual_side_effects=actual_se,
        unexpected_side_effects=unexpected,
        missing_side_effects=missing,
        estimated_duration_ms=est_dur,
        actual_duration_ms=actual_dur,
        duration_delta_ms=delta,
    )
