"""Scoring helpers for the eval harness.

Each ``expected`` block in ``golden.json`` is a flat dict whose keys encode
both the target field and the check type via a small suffix grammar. Keep
the grammar tight — every assertion fits on one line, no nesting.

Grammar:

    <field>                    — exact equality on actual[<field>]
    <field>_in: [list]         — list membership: actual[<field>] in [...]
    <field>_contains: value    — substring (str) or element (list) containment
                                 in actual[<field>]
    <field>_contains_any: [..] — ANY of the values is contained in actual[<field>]
    min_<field>: number        — numeric >= on actual[<field>]
    max_<field>: number        — numeric <= on actual[<field>]

Suffix matching is ordered: prefix checks (``min_``, ``max_``) win over
suffix checks (``_in``, ``_contains_any``, ``_contains``) which win over plain
equality. ``_contains_any`` is tested before ``_contains`` because the shorter
suffix is a suffix of the longer one, and checking the wrong way round would read
``root_cause_contains_any`` as a ``_contains`` check on a field literally named
``root_cause_contains_any``.

``_contains_any`` exists for root-cause grading. The ecommerce truth files express
the expected answer as ``grading.match_any_keyword`` — a *disjunction*, because a
correct verdict may say "PostgreSQL", "database" or "connection" and all three are
right. ``_contains`` can only express a conjunction of one, so grading a root cause
with it would fail a correct answer that happened to pick a different synonym.
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
    if not isinstance(actual, dict):
        return (False, f"actual not a dict (got {type(actual).__name__})")

    if key.startswith("min_"):
        return _check_numeric(key[4:], want, actual, op="gte")
    if key.startswith("max_"):
        return _check_numeric(key[4:], want, actual, op="lte")
    if key.endswith("_in"):
        field = key[:-3]
        got = actual.get(field)
        if not isinstance(want, (list, tuple, set)):
            return (False, f"_in expects a list, got {type(want).__name__}")
        return (got in want, f"actual[{field!r}]={_short(got)} expected_in={_short(want)}")
    if key.endswith("_contains_any"):
        field = key[: -len("_contains_any")]
        got = actual.get(field)
        if not isinstance(want, (list, tuple, set)):
            return (False, f"_contains_any expects a list, got {type(want).__name__}")
        if got is None:
            return (False, f"actual[{field!r}] is None")
        # Case-insensitive for strings: the values are keywords matched against
        # prose an LLM wrote, and "PostgreSQL" must satisfy the keyword "postgres".
        if isinstance(got, str):
            haystack = got.lower()
            hits = [w for w in want if str(w).lower() in haystack]
            return (
                bool(hits),
                f"actual[{field!r}]={_short(got)} matched={_short(hits)} "
                f"of want_any={_short(list(want))}",
            )
        if isinstance(got, (list, tuple, set)):
            hits = [w for w in want if w in got]
            return (bool(hits), f"actual[{field!r}]={_short(got)} matched={_short(hits)}")
        return (False, f"actual[{field!r}] is {type(got).__name__}; cannot 'contains_any'")

    if key.endswith("_contains"):
        field = key[:-9]
        got = actual.get(field)
        if got is None:
            return (False, f"actual[{field!r}] is None")
        if isinstance(got, str):
            return (str(want) in got, f"actual[{field!r}]={_short(got)} want_substr={want!r}")
        if isinstance(got, (list, tuple, set)):
            return (want in got, f"actual[{field!r}]={_short(got)} want_elem={want!r}")
        return (False, f"actual[{field!r}] is {type(got).__name__}; cannot 'contains'")

    got = actual.get(key)
    return (got == want, f"actual[{key!r}]={_short(got)} expected={_short(want)}")


def _check_numeric(field: str, want: Any, actual: dict[str, Any], *, op: str) -> tuple[bool, str]:
    got = actual.get(field)
    try:
        gotv = float(got)
        wantv = float(want)
    except (TypeError, ValueError):
        return (False, f"actual[{field!r}]={_short(got)} not numeric")
    ok = (gotv >= wantv) if op == "gte" else (gotv <= wantv)
    return (ok, f"actual[{field!r}]={gotv} {op} {wantv}")


def _short(v: Any, n: int = 80) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."
