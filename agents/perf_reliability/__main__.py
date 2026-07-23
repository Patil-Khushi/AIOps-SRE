"""CLI entry: fetch the sample job through the tool seam and analyze it.

    uv run python -m agents.perf_reliability

Demonstrates the full path the demo uses: fetch data via the registry
(``code.assets.fetch``, sample provider) → ``analyze`` → print the verdict.
Swap the provider to ``databricks`` later and this same command runs live.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiops.tools.databricks  # noqa: F401  — registers the sample provider
from agents.perf_reliability.agent import analyze
from aiops._dotenv import load_dotenv
from aiops.tools import get_registry


def main() -> int:
    # Load .env so the LLM path uses the configured (Azure) provider when
    # available; without it the agent falls back to the offline heuristic.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    res = get_registry().call("code.assets.fetch", job_name="sample-incremental-load")
    if not res.ok:
        print(f"fetch failed: {res.error}")
        return 1
    verdict = analyze(res.data)
    print(json.dumps(verdict.to_dict(), indent=2))
    print()
    print(f"Job: {verdict.job_name}  |  confidence: {verdict.confidence_score:.2f}")
    print(f"Bottlenecks: {', '.join(verdict.bottleneck_assets) or '(none)'}")
    for i, f in enumerate(verdict.findings, start=1):
        loc = f"{f.notebook}:{f.line}" if f.line is not None else f.notebook
        print(f"  {i}. [{f.implementation_complexity.value}] {loc} — {f.recommendation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
