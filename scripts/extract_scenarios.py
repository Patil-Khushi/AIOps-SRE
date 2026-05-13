"""One-shot extractor for D4: dump demo/ui/server.py::SCENARIOS to YAML.

Reads the in-memory ``SCENARIOS`` dict that ``server.py`` already exposes,
and writes one ``demo/scenarios/<scenario_id>.yaml`` file per entry. This
is the byte-faithful source of truth for D5's loader to read.

Idempotent: rerunning overwrites any existing files. Safe to delete after
D5 lands — kept in scripts/ as the record of how the YAML was first
generated.

Run:
    uv run python -m scripts.extract_scenarios
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Importing server.py runs its module-level code (init, tool registration,
# etc.). That's fine for a one-shot script.
from demo.ui.server import SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "demo" / "scenarios"


# Field order in each emitted YAML — explicit so reviews see a stable layout.
FIELD_ORDER = (
    "id",
    "category",
    "flag",
    "variant_on",
    "alert",
    "service",
    "title",
    "description",
    "eta_seconds",
)


def _to_ordered(sid: str, body: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"id": sid}
    for key in FIELD_ORDER:
        if key == "id":
            continue
        if key in body:
            record[key] = body[key]
    # Preserve any fields the FIELD_ORDER list does not yet know about.
    for k, v in body.items():
        if k not in record:
            record[k] = v
    return record


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sid, body in SCENARIOS.items():
        record = _to_ordered(sid, body)
        out = OUT_DIR / f"{sid}.yaml"
        out.write_text(
            yaml.safe_dump(
                record,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=100,
            ),
            encoding="utf-8",
        )
        written.append(out)
    print(f"wrote {len(written)} scenario file(s) to {OUT_DIR}")


if __name__ == "__main__":
    main()
