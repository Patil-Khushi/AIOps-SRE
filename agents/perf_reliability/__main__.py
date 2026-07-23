"""CLI entry: fetch a job through the tool seam, analyze it, print/emit results.

    uv run python -m agents.perf_reliability
    uv run python -m agents.perf_reliability --html report.html   # open in a browser
    uv run python -m agents.perf_reliability --json               # full verdict JSON

Demonstrates the path the demo uses: fetch data via the registry
(``code.assets.fetch``, sample provider) → ``analyze`` → render. Swap the
provider to ``databricks`` later and this same command runs live.
"""

from __future__ import annotations

import argparse
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
_UC3_CAP = 32000  # must be >= AIOPS_UC3_RETRY_TOKENS or the retry is clamped
_existing_cap = int(os.environ.get("AIOPS_LLM_MAX_TOKENS_PER_CALL", "0") or "0")
if _existing_cap < _UC3_CAP:
    os.environ["AIOPS_LLM_MAX_TOKENS_PER_CALL"] = str(_UC3_CAP)

import aiops.tools.databricks  # noqa: E402,F401  — import registers the sample provider
from agents.perf_reliability.agent import analyze  # noqa: E402
from agents.perf_reliability.report import render_html  # noqa: E402
from aiops.tools import get_registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m agents.perf_reliability",
        description="UC3 code/runtime optimizer — analyze a job and print recommendations.",
    )
    ap.add_argument("--job", default="orders_incremental_load", help="job name to fetch")
    ap.add_argument("--html", metavar="PATH", help="write a self-contained HTML report to PATH")
    ap.add_argument("--json", action="store_true", help="also print the full verdict JSON")
    args = ap.parse_args()

    res = get_registry().call("code.assets.fetch", job_name=args.job)
    if not res.ok:
        print(f"fetch failed: {res.error}")
        return 1
    verdict = analyze(res.data)

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))

    print(
        f"Job: {verdict.job_name}  |  confidence: {verdict.confidence_score:.2f}  |  "
        f"{len(verdict.findings)} finding(s) across {verdict.analyzed_assets} asset(s)"
    )
    print(f"Bottlenecks: {', '.join(verdict.bottleneck_assets) or '(none)'}")
    for i, f in enumerate(verdict.findings, start=1):
        loc = f"{f.notebook}:{f.line}" if f.line is not None else f.notebook
        print(f"  {i}. [{f.implementation_complexity.value}] {loc} — {f.recommendation}")

    if args.html:
        out = Path(args.html)
        out.write_text(render_html(verdict), encoding="utf-8")
        print(f"\nHTML report written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
