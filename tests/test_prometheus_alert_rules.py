"""Structural checks on the ecommerce Prometheus alert rules.

Nothing parsed this file before. The two existing alertname checks —
``tests/test_scenarios_yaml.py`` and ``tests/test_ecommerce_truth_files_evaluable.py``
— both do ``f"alert: {name}" in values`` against the raw text, which passes
against a commented-out rule and cannot see a malformed one.

That matters more than it sounds: a single PromQL error makes Prometheus refuse
to load the **entire group**, silently disabling every other alert in it. These
tests parse the YAML properly and, where promtool is available, check the PromQL
itself.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
VALUES_PATH = REPO_ROOT / "infra" / "observability" / "prometheus-values.yaml"
SCENARIOS_DIR = REPO_ROOT / "demo" / "ecommerce" / "scenarios"

REQUIRED_LABELS = {"severity", "service", "alert_type"}
REQUIRED_ANNOTATIONS = {"summary", "description", "scenario"}
VALID_SERVICES = {"user-service", "order-service", "payment-service"}


def _ecommerce_group() -> dict[str, Any]:
    data = yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))
    groups = data["serverFiles"]["alerting_rules.yml"]["groups"]
    for group in groups:
        if group["name"] == "ecommerce":
            return group
    raise AssertionError("no rule group named 'ecommerce' in prometheus-values.yaml")


def _rules() -> list[dict[str, Any]]:
    return list(_ecommerce_group()["rules"])


def _scenario_stems() -> set[str]:
    return {p.stem for p in SCENARIOS_DIR.glob("*.yaml")}


def _failure_keys_as_scenario_ids() -> set[str]:
    """Registered failure keys in scenario-id spelling (dots -> underscores)."""
    from demo.ecommerce.failure_injection import FAILURES

    return {key.replace(".", "_") for key in FAILURES}


def _add_eval_blocks() -> Any:
    """Import the truth-file generator, which lives in a package-less directory."""
    path = REPO_ROOT / "demo" / "ecommerce" / "truth_files" / "_add_eval_blocks.py"
    spec = importlib.util.spec_from_file_location("_add_eval_blocks", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── structure ───────────────────────────────────────────────────────────────


def test_rules_yaml_parses_and_has_an_ecommerce_group() -> None:
    assert _rules(), "the ecommerce group has no rules"


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: r["alert"])
def test_every_rule_has_house_style_fields(rule: dict[str, Any]) -> None:
    assert "expr" in rule and str(rule["expr"]).strip(), f"{rule['alert']}: empty expr"
    assert "for" in rule, f"{rule['alert']}: missing 'for' (fires on a single scrape)"

    labels = rule.get("labels") or {}
    assert REQUIRED_LABELS <= set(labels), (
        f"{rule['alert']}: labels missing {REQUIRED_LABELS - set(labels)}"
    )
    # EcommerceServiceDown templates its service from the firing series, since
    # it is the one rule that is not service-specific.
    service = str(labels["service"])
    assert service in VALID_SERVICES or "{{" in service, (
        f"{rule['alert']}: service={service!r} is neither a known service nor a template"
    )

    annotations = rule.get("annotations") or {}
    assert REQUIRED_ANNOTATIONS <= set(annotations), (
        f"{rule['alert']}: annotations missing {REQUIRED_ANNOTATIONS - set(annotations)}"
    )


def test_alert_names_are_unique() -> None:
    names = [r["alert"] for r in _rules()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate alert names: {dupes}"


@pytest.mark.parametrize("rule", _rules(), ids=lambda r: r["alert"])
def test_rule_scenario_annotation_names_something_real(rule: dict[str, Any]) -> None:
    """``annotations.scenario`` must name a real scenario or a real failure key.

    Both are accepted on purpose. Most rules point at a scenario YAML, but the
    resource rules also cover failures that are deliberately CLI-only — they
    have no scenario file and no truth file, by decision — so those annotations
    name the registered failure key instead. Either way a rename cannot leave
    the annotation pointing at nothing.
    """
    scenario = rule["annotations"]["scenario"]
    known = _scenario_stems() | _failure_keys_as_scenario_ids()
    assert scenario in known, (
        f"{rule['alert']}: annotations.scenario={scenario!r} matches no scenario "
        f"file and no registered failure key"
    )


def test_container_metric_exprs_are_namespace_scoped() -> None:
    """A container-metric rule without a namespace selector alerts on the whole
    cluster, kube-system included."""
    for rule in _rules():
        expr = str(rule["expr"])
        if "container_" in expr:
            assert 'namespace="ecommerce"' in expr, (
                f"{rule['alert']}: uses container metrics without a namespace selector"
            )


def test_container_metric_exprs_select_on_pod_not_container() -> None:
    """This cluster's cAdvisor publishes pod-level rollups with no ``container``
    label, so a ``container="user-service"`` selector matches nothing and the
    rule silently never fires. Verified live: pod-keyed returns 9 series,
    container-keyed returns 0."""
    for rule in _rules():
        expr = str(rule["expr"])
        if "container_" in expr:
            assert 'container="' not in expr, (
                f"{rule['alert']}: selects on a `container` label that does not "
                "exist on these series; select on `pod` instead"
            )


# ─── agreement with the rest of the system ───────────────────────────────────


def test_every_scenario_alertname_resolves_to_a_rule() -> None:
    """The structural replacement for the raw substring check."""
    from demo.ui import scenario_provider

    names = {r["alert"] for r in _rules()}
    for sid, row in scenario_provider.load().items():
        assert row["alert"] in names, (
            f"{sid}: alertname {row['alert']!r} is not defined in the ecommerce rule group"
        )


def test_alertnames_map_agrees_with_add_eval_blocks() -> None:
    """The two copies of the scenario -> alertname mapping must not drift.

    ``demo/ui/scenario_provider.ALERTNAMES`` and
    ``demo/ecommerce/truth_files/_add_eval_blocks.ALERTS`` are duplicated on
    purpose (the generator is a dev tool the request path must not import), and
    nothing checked they agree. A partial remap would ship silently: the
    dashboard would show one alertname and the eval harness would grade against
    another.
    """
    from demo.ui import scenario_provider

    generator_alerts = _add_eval_blocks().ALERTS

    assert set(scenario_provider.ALERTNAMES) == set(generator_alerts), (
        "scenario sets differ between ALERTNAMES and _add_eval_blocks.ALERTS"
    )
    mismatched = {
        sid: (name, generator_alerts[sid][0])
        for sid, name in scenario_provider.ALERTNAMES.items()
        if generator_alerts[sid][0] != name
    }
    assert not mismatched, f"alertname disagreements (provider, generator): {mismatched}"


def test_every_scenario_has_an_explicit_alertnames_entry() -> None:
    """``ALERTNAMES.get(sid, "EcommerceServiceDown")`` silently defaults.

    A scenario missing from the map still renders and still passes the alertname
    checks — it just claims an alert that has nothing to do with its fault.
    Catching that here is better than leaving the fallback to be discovered
    during a demo.
    """
    from demo.ui import scenario_provider

    assert set(scenario_provider.ALERTNAMES) == _scenario_stems()


@pytest.mark.parametrize(
    "path",
    sorted((REPO_ROOT / "demo" / "ecommerce" / "truth_files").glob("*.json")),
    ids=lambda p: p.stem,
)
def test_truth_file_alertname_resolves_to_a_rule(path: Path) -> None:
    names = {r["alert"] for r in _rules()}
    data = json.loads(path.read_text(encoding="utf-8"))
    alertname = data["expected_alert_payload"]["labels"]["alertname"]
    assert alertname in names, f"{path.stem}: alertname {alertname!r} matches no rule"


# ─── PromQL validity ─────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promql_is_syntactically_valid() -> None:
    """A PromQL error makes Prometheus drop the whole group, not just the rule.

    Skipped when promtool is absent so this does not become a required binary;
    the queries are also exercised against the live cluster during verification.
    """
    rules_doc = {"groups": [_ecommerce_group()]}
    with tempfile.TemporaryDirectory() as tmp:
        rules_file = Path(tmp) / "alerting_rules.yml"
        rules_file.write_text(yaml.safe_dump(rules_doc), encoding="utf-8")
        proc = subprocess.run(
            ["promtool", "check", "rules", str(rules_file)],
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 0, f"promtool rejected the rules:\n{proc.stdout}\n{proc.stderr}"
