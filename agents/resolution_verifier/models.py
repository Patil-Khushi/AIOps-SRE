"""Models for the Resolution Verifier."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"  # data source unavailable — not a failure


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str = ""
    before: str | None = None
    after: str | None = None


class VerificationReport(BaseModel):
    """Outcome of verifying that an incident is actually resolved."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    service: str
    verdict: CheckStatus  # PASS (no failed checks) or FAIL (≥1 failed)
    checks: list[CheckResult] = Field(default_factory=list)
    rounds: int = 0
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def passed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.PASS]

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    @property
    def skipped(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.SKIPPED]

    def close_code(self) -> str:
        return "Solved (Permanently)" if self.verdict is CheckStatus.PASS else ""

    def work_note(self) -> str:
        """Human-readable proof note written onto the ServiceNow incident.

        Lists every check with before/after numbers, and — importantly for the
        HITL reviewer — clearly calls out which checks were SKIPPED (no data),
        so they can see what was and was not actually verified.
        """
        head = (
            f"[Resolution Verifier] {self.service} — verdict: {self.verdict.value.upper()} "
            f"({len(self.passed)} passed, {len(self.failed)} failed, "
            f"{len(self.skipped)} skipped over {self.rounds} stabilization round(s))."
        )
        lines = [head, ""]
        for c in self.checks:
            mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIPPED"}[c.status.value]
            ba = ""
            if c.before is not None or c.after is not None:
                ba = f" (before={c.before}, after={c.after})"
            lines.append(f"- [{mark}] {c.name}: {c.detail}{ba}")
        if self.skipped:
            lines.append("")
            lines.append(
                "NOTE: skipped checks had no available data source (e.g. Loki/Prometheus "
                "not reachable) and were NOT verified — review before closing."
            )
        if self.verdict is CheckStatus.FAIL:
            lines.append("")
            lines.append("Fix applied but symptoms persist — closure NOT proposed.")
        return "\n".join(lines)


__all__ = ["CheckResult", "CheckStatus", "VerificationReport"]
