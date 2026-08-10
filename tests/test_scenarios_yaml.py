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


# Scenarios whose fault is invisible on an idle SUT, so the UI auto-starts the
# load generator on inject. Two independent reasons a fault lands here:
#   * its Prometheus rule is a rate()/histogram over [2m], so zero requests means
#     zero failures and the counter never moves, or
#   * the fault is applied per-request (maybe_burn_cpu burns ~2s inside the route
#     handler; INJECT_LATENCY_SECONDS sleeps in it), so an idle service never spikes.
# The complement is observable idle: the three *_connection_status gauges are
# refreshed by Prometheus' own scrape (each service pings its datastore inside
# /metrics) and crashloop trips up{} == 0.
TRAFFIC_DEPENDENT = {
    "order_service_http_500",
    "order_service_memory_leak",
    "order_service_payment_timeout",
    "payment_service_gateway_timeout",
    "payment_service_high_cpu",
    "payment_service_http_500",
    "user_service_high_cpu",
    "user_service_high_latency",
}


def test_needs_load_matches_registry_loadhint() -> None:
    """The catalog's ``needs_load`` must agree with the registry's ``Failure.load``.

    The YAMLs are generated from the registry, so these two can drift silently if
    a scenario file is hand-edited or a LoadHint is added without regenerating.
    Drift is invisible at runtime and expensive: the UI would decline to start
    traffic for a fault that needs it, and the operator sees an injected scenario
    that never raises its alert.
    """
    from demo.ecommerce.failure_injection import FAILURES
    from demo.ui import scenario_provider

    for sid, row in scenario_provider.load().items():
        failure = FAILURES.get(str(row["flag"]))
        if failure is None:  # covered by test_failure_key_is_registered
            continue
        assert row["needs_load"] == (failure.load is not None), (
            f"{sid}: catalog needs_load={row['needs_load']} but registry "
            f"Failure.load is {'set' if failure.load else 'None'} — regenerate the "
            f"scenario YAML or fix the LoadHint"
        )


def test_traffic_dependent_scenarios_are_flagged_needs_load() -> None:
    """Pin the traffic-dependent set so a rule/fault change cannot silently unflag one.

    If an alert rule is rewritten from a gauge to a rate(), or a fault moves into
    the request path, the scenario becomes traffic-dependent and must declare a
    ``load:`` block — otherwise it is injected and never observable.
    """
    from demo.ui import scenario_provider

    catalog = scenario_provider.load()
    flagged = {sid for sid, row in catalog.items() if row["needs_load"]}
    assert flagged == TRAFFIC_DEPENDENT, (
        f"needs_load drift — newly flagged: {sorted(flagged - TRAFFIC_DEPENDENT)}; "
        f"no longer flagged: {sorted(TRAFFIC_DEPENDENT - flagged)}"
    )


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
