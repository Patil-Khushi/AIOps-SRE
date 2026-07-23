"""Perf & Code Reliability agent (UC3) — recommend-only code/runtime optimizer.

Client track: Azure Databricks. Given notebook source + runtimes, it names the
slowest assets and emits ranked, line-level optimization recommendations with an
estimated saving and an implementation-complexity rating. It only advises.

Public surface::

    from agents.perf_reliability import (
        Complexity, NotebookAsset, OptimizationFinding,
        PerfInput, PerfVerdict, analyze, run, reset_state,
    )
"""

from agents.perf_reliability.agent import analyze, reset_state, run
from agents.perf_reliability.models import (
    Complexity,
    NotebookAsset,
    OptimizationFinding,
    PerfAuditMetadata,
    PerfInput,
    PerfVerdict,
)

__all__ = [
    "Complexity",
    "NotebookAsset",
    "OptimizationFinding",
    "PerfAuditMetadata",
    "PerfInput",
    "PerfVerdict",
    "analyze",
    "reset_state",
    "run",
]
