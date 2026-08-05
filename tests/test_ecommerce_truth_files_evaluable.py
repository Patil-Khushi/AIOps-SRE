"""Guards for the ecommerce truth-file evals.

The failure mode these exist to catch is silent, not loud: ``run_truth_file()``
returns an empty ``TruthFileRun`` when a truth file lacks ``expected_alert_payload``
or ``exercises``, and an empty run scores ``pass_rate == 1.0``. So a truth file
that is never actually exercised is indistinguishable in the summary from one
that passes — and a discovery bug that drops the whole ecommerce suite reports
a perfect score while measuring nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.harness import discover_truth_files, load_truth_file

REPO_ROOT = Path(__file__).resolve().parent.parent
ECOMMERCE_TRUTH = REPO_ROOT / "demo" / "ecommerce" / "truth_files"

EXPECTED_SCENARIO_COUNT = 12


def _ecommerce_truth_files() -> list[Path]:
    return sorted(p for p in ECOMMERCE_TRUTH.glob("*.json"))


def test_ecommerce_truth_files_exist() -> None:
    found = _ecommerce_truth_files()
    assert len(found) == EXPECTED_SCENARIO_COUNT, (
        f"expected {EXPECTED_SCENARIO_COUNT} ecommerce truth files, found {len(found)}: "
        f"{[p.name for p in found]}"
    )


def test_discovery_includes_ecommerce_truth_files() -> None:
    """The harness must actually pick the JSON suite up, not just the legacy YAML."""
    discovered = {p.resolve() for p in discover_truth_files()}
    for path in _ecommerce_truth_files():
        assert path.resolve() in discovered, (
            f"{path.name} is not discovered by evals.harness.discover_truth_files(); "
            "the ecommerce suite would be skipped and the run would still report 1.0"
        )


@pytest.mark.parametrize("path", _ecommerce_truth_files(), ids=lambda p: p.stem)
def test_truth_file_is_actually_evaluable(path: Path) -> None:
    """Both blocks must be present, or the scenario is scored without being run."""
    data = load_truth_file(path)

    assert data.get("scenario_id"), f"{path.name}: no scenario_id (or id) to key results on"

    payload = data.get("expected_alert_payload")
    assert payload, (
        f"{path.name}: missing expected_alert_payload — the harness will return an "
        "empty run that silently scores 1.0"
    )
    exercises = data.get("exercises")
    assert exercises, f"{path.name}: missing exercises — same silent-pass problem"

    # severity_hint must be top-level. alert_triage reads alert.severity_hint;
    # the adapters normally map it from labels.severity, but the harness feeds
    # the payload straight to run() with no adapter in the path. Without it the
    # classifier falls through to a value/threshold ratio that assumes
    # higher-is-worse, and scores a database outage (value=0) as Sev-4.
    assert payload.get("severity_hint"), (
        f"{path.name}: expected_alert_payload.severity_hint is missing. Putting "
        "severity only inside labels does not reach the rule-based classifier."
    )

    assert payload.get("service") == data.get("service"), (
        f"{path.name}: payload service {payload.get('service')!r} disagrees with "
        f"truth-file service {data.get('service')!r}"
    )


@pytest.mark.parametrize("path", _ecommerce_truth_files(), ids=lambda p: p.stem)
def test_alertname_matches_a_real_prometheus_rule(path: Path) -> None:
    """Every payload must name an alert the rule group actually defines.

    A typo here produces a truth file that passes its eval while describing an
    alert that can never fire against the live stack.
    """
    # Rules moved here when the OTel Demo umbrella chart was removed: it owned
    # Prometheus as a subchart, so its values.yaml was where the ecommerce
    # alert rules had to live. They now belong to the standalone stack.
    values_path = REPO_ROOT / "infra" / "observability" / "prometheus-values.yaml"
    values = values_path.read_text(encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    alertname = data["expected_alert_payload"]["labels"]["alertname"]
    assert f"alert: {alertname}" in values, (
        f"{path.name}: alertname {alertname!r} is not defined in "
        f"{values_path.relative_to(REPO_ROOT).as_posix()} — the scenario's truth "
        "file references an alert rule that does not exist"
    )
