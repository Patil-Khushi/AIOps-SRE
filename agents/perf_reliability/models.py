"""Input/output models for the Perf & Code Reliability agent (UC3).

Client track: Azure Databricks code/performance optimization.

Given one or more notebook assets (parent + nested children) with their source
and runtimes, the agent names the slowest/most wasteful assets and emits ranked,
line-level optimization recommendations — each with an estimated saving and an
implementation-complexity rating.

Recommend-only (UC3 requires only "identify optimization opportunities"): this
agent NEVER changes code or reruns a job. It advises; a human decides. There is
deliberately no HITL gate here because nothing is executed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Complexity(StrEnum):
    """How hard the recommended change is to implement."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NotebookAsset(BaseModel):
    """One notebook (parent or nested child) handed to the analyzer.

    ``source`` is the raw notebook code; ``runtime_minutes`` is that asset's
    measured wall-clock. ``called_by`` records the parent path so the agent can
    reason about the nested call tree (parent → ``dbutils.notebook.run`` child).
    """

    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1)
    source: str = ""
    runtime_minutes: float | None = None
    is_child: bool = False
    called_by: str | None = None


class OptimizationFinding(BaseModel):
    """One line-level optimization opportunity."""

    model_config = ConfigDict(extra="forbid")

    notebook: str
    line: int | None = None
    snippet: str = ""
    issue: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    # Freeform because it is an estimate (e.g. "~40% write time", "~$120/run").
    estimated_saving: str = ""
    implementation_complexity: Complexity = Complexity.MEDIUM


class PerfAuditMetadata(BaseModel):
    """Provenance carried on every verdict (mirrors the other agents' shape so
    the dashboard's decision-trace renderer works unchanged)."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    created_by: str = "perf_reliability (UC3)"
    # "sample" for the demo file-backed provider, "databricks" once the live
    # provider is wired. Surfaced so a reviewer always knows if numbers are real.
    signal_source: str = "sample"
    decision_trace: list[str] = Field(default_factory=list)


class PerfInput(BaseModel):
    """Eval-harness contract for ``run(input)``.

    A job is one or more notebook assets. ``total_runtime_minutes`` is the
    whole-job wall-clock; per-asset runtimes live on each ``NotebookAsset``.
    """

    model_config = ConfigDict(extra="allow")

    job_name: str = "unknown-job"
    total_runtime_minutes: float | None = None
    notebooks: list[NotebookAsset] = Field(default_factory=list)


class PerfVerdict(BaseModel):
    """Structured output of the Perf & Code Reliability agent.

    ``findings`` is ordered worst-first (index 0 is the highest-impact
    opportunity). ``primary_recommendation`` mirrors ``findings[0].recommendation``
    as a top-level scalar so the eval harness and quick UI views can read it
    without walking the nested list.
    """

    model_config = ConfigDict(extra="forbid")

    job_name: str
    summary: str = Field(min_length=1)
    total_runtime_minutes: float | None = None
    analyzed_assets: int = 0
    bottleneck_assets: list[str] = Field(default_factory=list)
    findings: list[OptimizationFinding] = Field(default_factory=list)
    primary_recommendation: str = ""
    confidence_score: float = Field(ge=0.0, le=1.0)
    audit_metadata: PerfAuditMetadata

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
