"""Tests for the YAML scenario files (D4).

D4 only writes the YAML files; D5 swaps the in-memory ``SCENARIOS`` for
this loader. These tests are the contract D5 must satisfy — and the
schema reviewers can rely on without reading server.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"

REQUIRED_FIELDS = {
    "id",
    "category",
    "flag",
    "alert",
    "service",
    "title",
    "description",
    "eta_seconds",
}
OPTIONAL_FIELDS = {"variant_on"}
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS

VALID_CATEGORIES = {"errors", "latency", "capacity", "infra"}


def _yaml_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


def test_scenarios_dir_has_at_least_one_file():
    assert _yaml_paths(), f"no YAML scenario files found in {SCENARIOS_DIR}"


@pytest.mark.parametrize("path", _yaml_paths(), ids=lambda p: p.name)
def test_scenario_file_passes_schema(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: top-level must be a mapping"

    missing = REQUIRED_FIELDS - data.keys()
    assert not missing, f"{path.name}: missing required fields {missing}"

    extra = data.keys() - ALLOWED_FIELDS
    assert not extra, f"{path.name}: unknown fields {extra}"

    assert data["id"] == path.stem, f"{path.name}: id {data['id']!r} != filename stem {path.stem!r}"
    assert data["category"] in VALID_CATEGORIES, (
        f"{path.name}: category {data['category']!r} not in {sorted(VALID_CATEGORIES)}"
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


def test_yaml_dict_matches_in_memory_scenarios():
    """The YAML files must be byte-faithful to the dict in server.py. Once
    D5 deletes the dict, this assertion is the safety net protecting the
    cut-over: if anyone hand-edits the YAML, the dict comparison breaks."""
    from demo.ui.server import SCENARIOS

    loaded: dict[str, dict] = {}
    for p in _yaml_paths():
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        sid = data.pop("id")
        loaded[sid] = data

    assert set(loaded) == set(SCENARIOS), (
        f"YAML / dict mismatch — in YAML only: {set(loaded) - set(SCENARIOS)}; "
        f"in dict only: {set(SCENARIOS) - set(loaded)}"
    )
    for sid, dict_body in SCENARIOS.items():
        assert loaded[sid] == dict_body, (
            f"scenario {sid!r} differs:\n  dict: {dict_body}\n  yaml: {loaded[sid]}"
        )
