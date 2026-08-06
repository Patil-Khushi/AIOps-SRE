"""Schema tests for the ecommerce scenario YAMLs.

Replaces the OTel Demo version, which validated two flavours of descriptor
(UI-only vs CLI-runnable, discriminated by a ``mechanism`` block) against
``demo/scenarios/``. That directory and its schema went with the demo app.

The guarantees worth keeping are unchanged, only re-aimed:

  * every scenario parses and carries the fields the tooling reads,
  * ids are unique and match their filename,
  * every scenario has a truth file (CLAUDE.md non-negotiable #8),
  * the dashboard catalog the server builds agrees with the files on disk.

That last one is the one that actually catches drift: the UI reads a derived
catalog, so a renamed field shows up as a blank column rather than an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "demo" / "ecommerce" / "scenarios"
TRUTH_DIR = REPO_ROOT / "demo" / "ecommerce" / "truth_files"

REQUIRED = {
    "id",
    "title",
    "service",
    "failure_key",
    "severity",
    "inject",
    "recover",
    "detection",
    "expected_rca",
    "truth_file",
}

VALID_SERVICES = {"user-service", "order-service", "payment-service"}
VALID_SEVERITIES = {"critical", "high", "warning"}


def _scenario_files() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: must be a YAML mapping"
    return data


def test_scenarios_dir_has_at_least_one_file() -> None:
    assert _scenario_files(), f"no scenario YAMLs under {SCENARIOS_DIR}"


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_scenario_schema(path: Path) -> None:
    data = _load(path)
    missing = REQUIRED - set(data)
    assert not missing, f"{path.name}: missing required field(s) {sorted(missing)}"
    assert data["id"] == path.stem, (
        f"{path.name}: 'id' must equal the filename stem {path.stem!r}, got {data['id']!r}"
    )
    assert data["service"] in VALID_SERVICES, (
        f"{path.name}: service {data['service']!r} not in {sorted(VALID_SERVICES)}"
    )
    # severity drives the injected alert's severity_hint, which the rule-based
    # classifier maps to Sev-1/Sev-2. A typo here silently downgrades an
    # incident rather than failing.
    assert data["severity"] in VALID_SEVERITIES, (
        f"{path.name}: severity {data['severity']!r} not in {sorted(VALID_SEVERITIES)}"
    )
    assert "command" in (data["inject"] or {}), f"{path.name}: inject.command missing"
    assert "command" in (data["recover"] or {}), f"{path.name}: recover.command missing"


def test_scenario_ids_are_unique() -> None:
    ids = [_load(p)["id"] for p in _scenario_files()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate scenario ids: {sorted(dupes)}"


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_every_scenario_has_a_truth_file(path: Path) -> None:
    """CLAUDE.md non-negotiable #8 — no scenario without ground truth."""
    assert (TRUTH_DIR / f"{path.stem}.json").exists(), (
        f"{path.name}: no matching truth file at truth_files/{path.stem}.json"
    )


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda p: p.stem)
def test_failure_key_is_registered(path: Path) -> None:
    """The declared failure_key must exist in the injection registry.

    Without this a scenario looks fine on disk and in the dashboard, then fails
    at inject time with 'unknown failure key' — after the operator has already
    clicked the button during a demo.
    """
    from demo.ecommerce.failure_injection import FAILURES

    key = _load(path)["failure_key"]
    assert key in FAILURES, (
        f"{path.name}: failure_key {key!r} is not registered; available: {sorted(FAILURES)}"
    )


def test_yaml_descriptors_match_server_catalog() -> None:
    """The catalog the UI serves must cover exactly the scenarios on disk."""
    from demo.ui import scenario_provider

    catalog = scenario_provider.load()
    on_disk = {p.stem for p in _scenario_files()}
    assert set(catalog) == on_disk, (
        f"catalog/disk mismatch — only in catalog: {sorted(set(catalog) - on_disk)}; "
        f"only on disk: {sorted(on_disk - set(catalog))}"
    )
    for sid, row in catalog.items():
        for field in ("title", "service", "severity", "alert", "flag", "category"):
            assert row.get(field), f"{sid}: catalog row is missing {field!r}"


def test_catalog_alertnames_exist_as_prometheus_rules() -> None:
    """Every scenario's alertname must be a rule the stack actually defines.

    A typo produces a synthetic dashboard alert that can never dedup against a
    real one, so the operator sees the same incident twice.
    """
    from demo.ui import scenario_provider

    values = (REPO_ROOT / "infra" / "observability" / "prometheus-values.yaml").read_text(
        encoding="utf-8"
    )
    for sid, row in scenario_provider.load().items():
        assert f"alert: {row['alert']}" in values, (
            f"{sid}: alertname {row['alert']!r} is not defined in "
            "infra/observability/prometheus-values.yaml"
        )
