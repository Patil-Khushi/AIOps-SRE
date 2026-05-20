"""Tests for Stage 1 input validation on the ``Alert`` Pydantic model.

The agent docstring claimed validate + normalize as Stage 1 work, but until
now it relied on Pydantic's default coercion only. These tests pin the
explicit rules:

- alert_id / service / metric: non-empty, surrounding whitespace stripped
- value / threshold: finite (reject NaN, +inf, -inf)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError


def _base_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alert_id": "ALT-V",
        "service": "payment",
        "metric": "cpu",
        "value": 90.0,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return base


# ─── identifier fields: alert_id / service / metric ────────────────────────


@pytest.mark.parametrize("field", ["alert_id", "service", "metric"])
@pytest.mark.parametrize(
    "bad_value",
    ["", "   ", "\t", "\n\n", " \t \n "],
    ids=["empty", "spaces", "tab", "newlines", "mixed-whitespace"],
)
def test_empty_or_whitespace_identifier_rejected(field: str, bad_value: str) -> None:
    from agents.alert_triage import Alert

    payload = _base_input(**{field: bad_value})
    with pytest.raises(ValidationError) as exc_info:
        Alert(**payload)
    # Surface the offending field name in the error so it's debuggable.
    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", ["alert_id", "service", "metric"])
def test_surrounding_whitespace_is_stripped(field: str) -> None:
    """Trim leading/trailing whitespace so downstream PromQL / lookups don't
    silently get a space-prefixed identifier."""
    from agents.alert_triage import Alert

    payload = _base_input(**{field: "  payment-service  "})
    alert = Alert(**payload)
    assert getattr(alert, field) == "payment-service"


# ─── numeric fields: value / threshold ─────────────────────────────────────


@pytest.mark.parametrize("field", ["value", "threshold"])
@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "+inf", "-inf"],
)
def test_non_finite_numeric_rejected(field: str, bad_value: float) -> None:
    """NaN / ±Inf must be rejected. NaN comparisons silently return False,
    so a malformed upstream payload would otherwise propagate through the
    rule-based classifier into the LLM consult with no warning."""
    from agents.alert_triage import Alert

    payload = _base_input(**{field: bad_value})
    with pytest.raises(ValidationError) as exc_info:
        Alert(**payload)
    assert field in str(exc_info.value)


def test_threshold_none_is_allowed() -> None:
    """Threshold is optional — None is the canonical 'no threshold given'
    signal and must not be rejected."""
    from agents.alert_triage import Alert

    alert = Alert(**_base_input(threshold=None))
    assert alert.threshold is None


def test_finite_value_passes() -> None:
    from agents.alert_triage import Alert

    alert = Alert(**_base_input(value=42.5, threshold=80.0))
    assert alert.value == 42.5
    assert alert.threshold == 80.0
