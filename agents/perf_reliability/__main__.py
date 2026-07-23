"""CLI entry: fetch the sample job through the tool seam and analyze it.

    uv run python -m agents.perf_reliability

Demonstrates the full path the demo uses: fetch data via the registry
(``code.assets.fetch``, sample provider) → ``analyze`` → print the verdict.
Swap the provider to ``databricks`` later and this same command runs live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Load .env BEFORE importing anything that resolves an LLM provider, so the CLI
# uses the configured (Azure) provider instead of falling back to the stub.
# (The eval harness works for the same reason — it loads .env at module top.)
from aiops._dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# The platform default model (Azure gpt-5) is a reasoning model: its token
# budget covers reasoning + output. The project .env pins the per-call cap at
# 4096, which is fine for small prompts but gets fully consumed by reasoning on
# a multi-notebook analysis, leaving empty content. Raise the ceiling for this
# demo run — take the max so we never LOWER an operator's higher setting.
_UC3_CAP = 16000
_existing_cap = int(os.environ.get("AIOPS_LLM_MAX_TOKENS_PER_CALL", "0") or "0")
if _existing_cap < _UC3_CAP:
    os.environ["AIOPS_LLM_MAX_TOKENS_PER_CALL"] = str(_UC3_CAP)

import aiops.tools.databricks  # noqa: E402,F401  — import registers the sample provider
from agents.perf_reliability.agent import analyze  # noqa: E402
from aiops.tools import get_registry  # noqa: E402


def main() -> int:
    res = get_registry().call("code.assets.fetch", job_name="orders_incremental_load")
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
