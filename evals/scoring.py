"""Scoring helpers for the eval harness.

Each ``expected`` block in ``golden.json`` is a dict of *checks* the agent's
output must satisfy. Supported checks are intentionally simple — Phase 0 should
not need a regex engine or LLM-judge. Add fancier scorers when a real agent
forces it.

Supported expected-keys:
    equals      — exact equality on the actual output
    contains    — substring/element membership in actual[<field>]
    has_keys    — actual is a dict containing every listed key
    matches     — actual matches a literal string (case-insensitive)
    field       — nested check on actual[<field>] using one of the above
"""

from __future__ import annotations

from typing import Any


def score_case(*, actual: Any, expected: dict[str, Any]) -> dict[str, Any]:
    checks: list[tuple[str, bool, str]] = []
    for key, want in expected.items():
        ok, detail = _check(key, want, actual)
        checks.append((key, ok, detail))
    passed = all(ok for _, ok, _ in checks)
    score = sum(1 for _, ok, _ in checks) / max(1, len(checks))
    return {
        "passed": passed,
        "score": score,
        "details": {"checks": [{"check": k, "passed": ok, "detail": d} for k, ok, d in checks]},
    }


def _check(key: str, want: Any, actual: Any) -> tuple[bool, str]:
    if key == "equals":
        return (actual == want, f"actual={_short(actual)} expected={_short(want)}")
    if key == "contains":
        if isinstance(actual, str):
            return (str(want) in actual, f"want substring {want!r}")
        if isinstance(actual, (list, tuple, set)):
            return (want in actual, f"want element {want!r}")
        return (False, f"actual is {type(actual).__name__}, cannot contain")
    if key == "has_keys":
        if not isinstance(actual, dict):
            return (False, f"actual not a dict (got {type(actual).__name__})")
        missing = [k for k in want if k not in actual]
        return (not missing, "missing=" + ",".join(missing) if missing else "ok")
    if key == "matches":
        return (
            isinstance(actual, str) and actual.casefold() == str(want).casefold(),
            f"actual={_short(actual)}",
        )
    if key == "field":
        if not isinstance(want, dict) or "name" not in want or "check" not in want:
            return (False, "field check requires {name, check, value}")
        if not isinstance(actual, dict):
            return (False, f"actual not a dict (got {type(actual).__name__})")
        sub_actual = actual.get(want["name"])
        sub_expected = {want["check"]: want.get("value")}
        sub = score_case(actual=sub_actual, expected=sub_expected)
        return (bool(sub["passed"]), f"field={want['name']} -> {sub['details']}")
    return (False, f"unknown check {key!r}")


def _short(v: Any, n: int = 80) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."
