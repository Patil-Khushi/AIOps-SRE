"""Perf & Code Reliability agent (UC3) — recommend-only code/runtime optimizer.

Entry point: ``analyze(perf_input) -> PerfVerdict``.

Pipeline:
    1. Validate input (pydantic on PerfInput).
    2. Rank assets by measured runtime.
    3. LLM reasoning pass over the notebook source + runtimes (JSON-mode-ish;
       parsed defensively because the gateway is provider-agnostic).
    4. Deterministic heuristic fallback when the LLM is the stub (CI), returns
       an unparseable response, or errors — so the agent always produces a
       useful, testable verdict offline.

Vendor-neutrality: imports only from ``aiops.llm``. No SDK imports. The data
source (sample files today, Databricks live later) is fetched *outside* this
agent through the tool registry and passed in — the agent stays a pure
input→verdict function so it is trivially testable.

Recommend-only: no HITL gate, no execution. UC3 only asks us to *identify*
opportunities.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from agents.perf_reliability.models import (
    Complexity,
    NotebookAsset,
    OptimizationFinding,
    PerfAuditMetadata,
    PerfInput,
    PerfVerdict,
)
from agents.perf_reliability.prompts import SYSTEM_PROMPT_V1, USER_PROMPT_V1
from aiops.llm import Message
from aiops.llm import complete as llm_complete

logger = logging.getLogger(__name__)

# Per-agent LLM choice. Defaults to the platform provider (Azure OpenAI today).
# Override via env if a given deployment mis-handles code-heavy prompts.
_LLM_PROVIDER = os.environ.get("AIOPS_UC3_LLM_PROVIDER") or None  # None = platform default
_LLM_MODEL = os.environ.get("AIOPS_UC3_LLM_MODEL") or None

# Token budget for the analysis call. Must be generous: the platform default
# model is a *reasoning* model (Azure gpt-5), whose budget covers reasoning +
# output, and reasoning-token usage VARIES per call — so a fixed budget
# occasionally leaves nothing for output (empty/truncated). We try a normal
# budget, then retry once at a larger one before falling back to the heuristic.
# NOTE: the gateway also clamps to AIOPS_LLM_MAX_TOKENS_PER_CALL (.env pins
# 4096) — the CLI raises that ceiling; any other entry point running this over
# multi-notebook jobs must raise it too, or these requests are silently capped.
_MAX_TOKENS = int(os.environ.get("AIOPS_UC3_MAX_TOKENS", "16000"))
_RETRY_TOKENS = int(os.environ.get("AIOPS_UC3_RETRY_TOKENS", "28000"))
# Reasoning effort for gpt-5/o-series: "minimal"|"low"|"medium"|"high". This is
# structured code review, not deep multi-step reasoning, so "low" cuts the
# (billed) reasoning tokens — cheaper + faster — with little quality loss. Set
# AIOPS_UC3_REASONING_EFFORT="" to disable the hint (provider default). Ignored
# by non-reasoning models/providers.
_REASONING_EFFORT = os.environ.get("AIOPS_UC3_REASONING_EFFORT", "low") or None


# ─── deterministic heuristic fallback ───────────────────────────────────────
#
# A small, honest anti-pattern scanner. It is NOT meant to replace the LLM —
# it is the offline/CI baseline and a safety net. Person A extends the LLM
# prompt; extend this table too so the deterministic evals keep pace.

_ANTIPATTERNS: list[tuple[re.Pattern[str], str, str, Complexity]] = [
    (
        re.compile(r"\.coalesce\s*\(\s*1\s*\)"),
        "coalesce(1) collapses the write to a single task, serializing output — "
        "a classic single-partition write bottleneck.",
        "Replace .coalesce(1) with .repartition(<n>) sized to the data, or write "
        "with .partitionBy(<column>) so the write parallelizes across executors.",
        Complexity.LOW,
    ),
    (
        re.compile(r"\.coalesce\s*\("),
        "coalesce() reduces partitions without a shuffle and can starve "
        "parallelism on wide writes.",
        "Prefer .repartition(<n>) / .partitionBy(<col>) unless you have measured "
        "that coalesce is safe for this data size.",
        Complexity.LOW,
    ),
    (
        re.compile(r"\.(collect|toPandas)\s*\(\s*\)"),
        "collect()/toPandas() pulls the whole dataset to the driver — a common "
        "out-of-memory and slowdown source at scale.",
        "Keep the computation distributed; aggregate/limit on the cluster and "
        "write with Spark instead of materializing on the driver.",
        Complexity.MEDIUM,
    ),
    (
        re.compile(r"for\s+\w+\s+in\s+.*\.collect\s*\(\s*\)"),
        "Row-by-row Python loop over collected Spark data defeats parallelism.",
        "Express the logic as a DataFrame transformation (select/withColumn/join) "
        "rather than a driver-side loop.",
        Complexity.MEDIUM,
    ),
    (
        re.compile(r"\.count\s*\(\s*\).*\n(?:.*\n){0,3}?.*\.(count|collect)\s*\(", re.MULTILINE),
        "Repeated actions (count/collect) recompute the DataFrame each time.",
        "Cache/persist the DataFrame once, or restructure so the action runs a single time.",
        Complexity.MEDIUM,
    ),
]


def _rank_assets(notebooks: list[NotebookAsset]) -> list[NotebookAsset]:
    """Slowest-first. Assets with no runtime sort last (unknown ≠ fast)."""
    return sorted(
        notebooks,
        key=lambda n: n.runtime_minutes if n.runtime_minutes is not None else -1.0,
        reverse=True,
    )


def _heuristic_findings(notebooks: list[NotebookAsset]) -> list[OptimizationFinding]:
    findings: list[OptimizationFinding] = []
    for nb in notebooks:
        if not nb.source:
            continue
        lines = nb.source.splitlines()
        for i, line in enumerate(lines, start=1):
            for pat, issue, rec, complexity in _ANTIPATTERNS:
                if pat.search(line):
                    findings.append(
                        OptimizationFinding(
                            notebook=nb.path,
                            line=i,
                            snippet=line.strip()[:200],
                            issue=issue,
                            recommendation=rec,
                            estimated_saving="(estimate pending profiling)",
                            implementation_complexity=complexity,
                        )
                    )
                    break  # one finding per line is enough
    return findings


def _fallback_verdict(
    parsed: PerfInput, ranked: list[NotebookAsset], *, decision_trace: list[str]
) -> PerfVerdict:
    findings = _heuristic_findings(ranked)
    decision_trace.append(
        f"deterministic heuristic scan: {len(findings)} finding(s) across {len(ranked)} asset(s)"
    )
    bottlenecks = list(dict.fromkeys(f.notebook for f in findings)) or [
        nb.path for nb in ranked[:1]
    ]
    if findings:
        summary = (
            f"Found {len(findings)} optimization opportunit(y/ies) in "
            f"{', '.join(bottlenecks)}; top fix: {findings[0].recommendation}"
        )
        confidence = 0.6
    else:
        summary = (
            "No known anti-patterns detected by the offline scanner. Run the "
            "LLM analysis (configure a live provider) for a deeper review."
        )
        confidence = 0.3
    return PerfVerdict(
        job_name=parsed.job_name,
        summary=summary,
        analyzed_assets=len(parsed.notebooks),
        bottleneck_assets=bottlenecks,
        findings=findings,
        primary_recommendation=findings[0].recommendation if findings else "",
        total_runtime_minutes=parsed.total_runtime_minutes,
        confidence_score=confidence,
        audit_metadata=PerfAuditMetadata(
            created_at=datetime.now(UTC),
            decision_trace=decision_trace,
        ),
    )


# ─── LLM response parsing ────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction (strip code fences, then find first balanced
    object). Mirrors the RCA agent's parser so behavior is consistent."""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_finding(raw: dict[str, Any]) -> OptimizationFinding | None:
    try:
        comp = str(raw.get("implementation_complexity", "medium")).lower()
        if comp not in {"low", "medium", "high"}:
            comp = "medium"
        line = raw.get("line")
        line_val = int(line) if isinstance(line, (int, float)) else None
        return OptimizationFinding(
            notebook=str(raw.get("notebook", "")).strip() or "unknown",
            line=line_val,
            snippet=str(raw.get("snippet", "")).strip()[:200],
            issue=str(raw.get("issue", "")).strip() or "unspecified",
            recommendation=str(raw.get("recommendation", "")).strip() or "unspecified",
            estimated_saving=str(raw.get("estimated_saving", "")).strip(),
            implementation_complexity=Complexity(comp),
        )
    except Exception as exc:  # defensive: one bad finding shouldn't sink the verdict
        logger.warning("perf_reliability: dropped malformed finding: %s", exc)
        return None


def _coerce_verdict(
    raw: dict[str, Any], parsed: PerfInput, *, decision_trace: list[str]
) -> PerfVerdict | None:
    try:
        raw_findings = raw.get("findings") or []
        if not isinstance(raw_findings, list):
            return None
        findings = [
            f for f in (_coerce_finding(x) for x in raw_findings if isinstance(x, dict)) if f
        ]
        summary = str(raw.get("summary", "")).strip()
        if not summary:
            summary = f"{len(findings)} optimization opportunit(y/ies) identified."
        bottlenecks = [str(b) for b in (raw.get("bottleneck_assets") or []) if b]
        if not bottlenecks and findings:
            bottlenecks = list(dict.fromkeys(f.notebook for f in findings))
        return PerfVerdict(
            job_name=parsed.job_name,
            summary=summary,
            analyzed_assets=len(parsed.notebooks),
            bottleneck_assets=bottlenecks,
            findings=findings,
            primary_recommendation=findings[0].recommendation if findings else "",
            total_runtime_minutes=parsed.total_runtime_minutes,
            confidence_score=float(raw.get("confidence_score", 0.5)),
            audit_metadata=PerfAuditMetadata(
                created_at=datetime.now(UTC),
                signal_source="databricks",
                decision_trace=decision_trace,
            ),
        )
    except Exception as exc:
        logger.warning("perf_reliability: verdict coercion failed: %s", exc)
        return None


# ─── prompt rendering ────────────────────────────────────────────────────────


def _render_assets_block(ranked: list[NotebookAsset]) -> str:
    blocks: list[str] = []
    for nb in ranked:
        rt = f"{nb.runtime_minutes} min" if nb.runtime_minutes is not None else "unknown"
        child = " (child)" if nb.is_child else ""
        blocks.append(
            f"--- {nb.path}{child} | runtime: {rt} ---\n{nb.source or '(no source provided)'}"
        )
    return "\n\n".join(blocks) if blocks else "(no assets provided)"


# ─── entry point ─────────────────────────────────────────────────────────────


def analyze(perf_input: dict[str, Any] | PerfInput) -> PerfVerdict:
    """Produce an optimization verdict for one pipeline/job."""
    parsed = perf_input if isinstance(perf_input, PerfInput) else PerfInput(**perf_input)
    ranked = _rank_assets(parsed.notebooks)
    decision_trace: list[str] = [
        f"received job={parsed.job_name!r} with {len(parsed.notebooks)} asset(s), "
        f"total_runtime={parsed.total_runtime_minutes}"
    ]

    user_prompt = USER_PROMPT_V1.format(
        job_name=parsed.job_name,
        total_runtime=(
            f"{parsed.total_runtime_minutes} min"
            if parsed.total_runtime_minutes is not None
            else "unknown"
        ),
        assets_block=_render_assets_block(ranked),
    )
    # Try a normal budget, then retry once at a larger one. A reasoning model's
    # per-call reasoning cost varies, so a single fixed budget is flaky.
    budgets = [_MAX_TOKENS] + ([_RETRY_TOKENS] if _RETRY_TOKENS > _MAX_TOKENS else [])
    for attempt, budget in enumerate(budgets, start=1):
        try:
            resp = llm_complete(
                messages=[
                    Message(role="system", content=SYSTEM_PROMPT_V1),
                    Message(role="user", content=user_prompt),
                ],
                provider=_LLM_PROVIDER,
                model=_LLM_MODEL,
                temperature=0.2,
                max_tokens=budget,
                reasoning_effort=_REASONING_EFFORT,
            )
        except Exception as exc:
            logger.warning("perf_reliability: LLM call failed (%s); using heuristic", exc)
            decision_trace.append(f"LLM call raised {type(exc).__name__}; falling back")
            return _fallback_verdict(parsed, ranked, decision_trace=decision_trace)

        text = (resp.text or "").strip()
        if text.startswith("[stub]"):
            decision_trace.append("LLM provider is the offline stub; using deterministic heuristic")
            return _fallback_verdict(parsed, ranked, decision_trace=decision_trace)

        verdict = None
        if text:
            raw = _extract_json_object(text)
            if raw is not None:
                verdict = _coerce_verdict(raw, parsed, decision_trace=decision_trace)
        if verdict is not None:
            decision_trace.append(
                f"LLM produced {len(verdict.findings)} finding(s) at budget={budget}, "
                f"confidence={verdict.confidence_score:.2f}"
            )
            return verdict

        # Empty/truncated output (reasoning model out of budget) or unparseable
        # JSON — record and let the loop retry at a larger budget if one is left.
        reason = "empty response" if not text else "unparseable/invalid JSON"
        decision_trace.append(f"attempt {attempt} (budget={budget}) -> {reason}")

    decision_trace.append(
        "LLM did not return usable JSON after retry (reasoning model likely out of "
        "token budget); using deterministic heuristic"
    )
    return _fallback_verdict(parsed, ranked, decision_trace=decision_trace)


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out shim around ``analyze``."""
    return analyze(PerfInput(**input)).to_dict()


def reset_state() -> None:
    """Eval-harness hook. This agent is stateless — no-op."""
    return None
