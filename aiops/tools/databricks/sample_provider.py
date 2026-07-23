"""Sample (file-backed) provider for ``code.assets.fetch`` — UC3 demo default.

Reads a bundled sample job from ``demo/uc3_sample/`` and returns it in the shape
the ``perf_reliability`` agent's ``PerfInput`` expects. No Azure needed — this is
what lets the first demo run offline.

The live Databricks provider (Person B) registers the SAME capability
(``code.assets.fetch``, provider ``databricks``) and is selected with
``get_registry().select_provider("code.assets.fetch", "databricks.code.assets.fetch")``.
The agent never changes — only the provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops.tools import ToolResult, tool

# demo/uc3_sample/ relative to the repo root (aiops/tools/databricks/ → up 3).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE_DIR = _REPO_ROOT / "demo" / "uc3_sample"


def _load_sample(sample_dir: Path) -> dict[str, Any]:
    manifest_path = sample_dir / "run_history.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    notebooks: list[dict[str, Any]] = []
    for asset in manifest.get("assets", []):
        src_file = sample_dir / asset["file"]
        source = src_file.read_text(encoding="utf-8") if src_file.exists() else ""
        notebooks.append(
            {
                "path": asset.get("path") or src_file.stem,
                "source": source,
                "runtime_minutes": asset.get("runtime_minutes"),
                "is_child": asset.get("is_child", False),
                "called_by": asset.get("called_by"),
            }
        )
    return {
        "job_name": manifest.get("job_name", "sample-job"),
        "total_runtime_minutes": manifest.get("total_runtime_minutes"),
        "notebooks": notebooks,
    }


@tool(
    name="sample.code.assets.fetch",
    capability="code.assets.fetch",
    provider="sample",
    description="Fetch a sample job's notebook source + per-asset runtimes from demo/uc3_sample/.",
)
def fetch(job_name: str | None = None, sample_dir: str | None = None) -> ToolResult:
    """Return the bundled sample job (or another dir via ``sample_dir``).

    ``job_name`` is accepted for interface parity with the future live provider
    (which will look the job up by name); the sample provider ignores it beyond
    echoing what the manifest declares.
    """
    base = Path(sample_dir) if sample_dir else _SAMPLE_DIR
    if not (base / "run_history.json").exists():
        return ToolResult(
            ok=False,
            error=f"sample job not found under {base} (expected run_history.json)",
            metadata={"provider": "sample"},
        )
    try:
        data = _load_sample(base)
    except Exception as exc:  # malformed manifest / unreadable files
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    return ToolResult(ok=True, data=data, metadata={"provider": "sample", "source_dir": str(base)})
