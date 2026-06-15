"""Tests for the YAML scenario files (D4 + DEMO-12).

DEMO-12 (#64) introduces two scenario flavours in the same folder:

- **UI descriptor** — minimal fields the dashboard catalog needs.
- **CLI runnable** — adds a ``mechanism`` block so ``inject.py`` can run it.

Schema enforcement here covers both. The ``mechanism`` field is the
discriminator: present → CLI-runnable schema applies; absent → UI-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"

# UI-descriptor schema (flavour 1)
UI_REQUIRED = {
    "id",
    "category",
    "flag",
    "alert",
    "service",
    "title",
    "description",
    "eta_seconds",
    # ``severity`` drives the injected alert's severity_hint so dashboard
    # injects classify across the full Sev-1..Sev-3 range instead of all
    # landing on Sev-2 (see demo/ui/server.py::_synthetic_alert_for_scenario
    # and agents/alert_triage/agent.py::_classify_severity_rule_based).
    # Required so the data and this contract can't silently drift apart.
    "severity",
}
UI_OPTIONAL = {"variant_on"}
UI_ALLOWED = UI_REQUIRED | UI_OPTIONAL

VALID_CATEGORIES = {"errors", "latency", "capacity", "infra"}
# Maps to RA-001's severity_hint buckets: critical→Sev-1 (page),
# high→Sev-2 (notify), warning→Sev-3 (daytime).
VALID_SEVERITIES = {"critical", "high", "warning"}

# CLI-runnable schema (flavour 2) — driven by the presence of ``mechanism``.
CLI_REQUIRED = {"id", "title", "description", "mechanism"}
# CLI scenarios may carry any of the descriptive blocks below; they are
# informational, not validated for shape here.
CLI_OPTIONAL = {
    "flagd",
    "kubectl",
    "chaos_mesh",
    "expected_signals",
    "duration_seconds",
    "clears_on",
}
CLI_ALLOWED = CLI_REQUIRED | CLI_OPTIONAL

VALID_MECHANISMS = {"flagd", "kubectl", "chaos-mesh"}


def _yaml_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


def test_scenarios_dir_has_at_least_one_file():
    assert _yaml_paths(), f"no YAML scenario files found in {SCENARIOS_DIR}"


@pytest.mark.parametrize("path", _yaml_paths(), ids=lambda p: p.name)
def test_scenario_file_passes_schema(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: top-level must be a mapping"
    assert data.get("id") == path.stem, (
        f"{path.name}: id {data.get('id')!r} != filename stem {path.stem!r}"
    )

    if "mechanism" in data:
        # CLI-runnable flavour
        missing = CLI_REQUIRED - data.keys()
        assert not missing, f"{path.name}: CLI scenario missing required fields {missing}"
        extra = data.keys() - CLI_ALLOWED
        assert not extra, f"{path.name}: CLI scenario has unknown fields {extra}"
        assert data["mechanism"] in VALID_MECHANISMS, (
            f"{path.name}: mechanism {data['mechanism']!r} not in {sorted(VALID_MECHANISMS)}"
        )
    else:
        # UI-descriptor flavour
        missing = UI_REQUIRED - data.keys()
        assert not missing, f"{path.name}: UI scenario missing required fields {missing}"
        extra = data.keys() - UI_ALLOWED
        assert not extra, f"{path.name}: UI scenario has unknown fields {extra}"
        assert data["category"] in VALID_CATEGORIES, (
            f"{path.name}: category {data['category']!r} not in {sorted(VALID_CATEGORIES)}"
        )
        assert data["severity"] in VALID_SEVERITIES, (
            f"{path.name}: severity {data['severity']!r} not in {sorted(VALID_SEVERITIES)}"
        )
        assert isinstance(data["eta_seconds"], int) and data["eta_seconds"] > 0, (
            f"{path.name}: eta_seconds must be a positive int"
        )


def test_scenario_ids_are_unique():
    seen: dict[str, Path] = {}
    for p in _yaml_paths():
        sid = yaml.safe_load(p.read_text(encoding="utf-8"))["id"]
        assert sid not in seen, f"duplicate id {sid!r}: {seen[sid].name} and {p.name}"
        seen[sid] = p


def test_yaml_ui_descriptors_match_server_in_memory_scenarios():
    """The UI dashboard's ``/api/scenarios`` payload is built from the
    UI-descriptor YAMLs in this folder. CLI-runnable YAMLs (with a
    ``mechanism`` block) are not part of the UI catalog — the dashboard
    filters them out — so they're excluded here too."""
    from demo.ui.server import SCENARIOS

    loaded: dict[str, dict] = {}
    for p in _yaml_paths():
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if "mechanism" in data:
            continue  # CLI-only, not part of the UI catalog
        sid = data.pop("id")
        loaded[sid] = data

    assert set(loaded) == set(SCENARIOS), (
        f"UI catalog mismatch — in YAML only: {set(loaded) - set(SCENARIOS)}; "
        f"in SCENARIOS only: {set(SCENARIOS) - set(loaded)}"
    )
    for sid, dict_body in SCENARIOS.items():
        assert loaded[sid] == dict_body, (
            f"scenario {sid!r} differs:\n  SCENARIOS: {dict_body}\n  yaml: {loaded[sid]}"
        )
