"""The RCA blindness boundary: truth files in, observable-only agent input out.

Why this file exists
--------------------
A truth file states what is wrong, why, and how to fix it. That makes it the
grading key *and* a loaded gun: any of it that reaches the RCA agent turns the
evaluation into a lookup and the accuracy number into a fiction. The leak does not
have to be deliberate — ``expected_alert_payload`` sits in the same JSON object as
``root_cause``, and passing "the truth file" to an agent is one keystroke away from
passing the answer.

So the split is made once, here, and enforced by assertion rather than by care:

    truth file
       ├── observable  -> rca_input_from_truth()   -> the agent
       └── truth       -> expected_from_truth()    -> the harness, never the agent

``assert_blind`` then re-checks the produced payload against the source file and
raises on leakage. It is called by ``rca_input_from_truth`` itself, so the guard
runs on the real path and not only in tests — a blindness check that only tests
call is a blindness check that a future caller bypasses.

Why the check is field-based and value-based, never substring-based
------------------------------------------------------------------
The obvious guard — "no root-cause keyword may appear in the input" — is wrong in
both directions. ``root_cause_keywords`` for the Postgres scenario includes
``"postgres"``, and the alert label ``alertname="EcommercePostgresDown"`` legitimately
contains it: that alert is exactly what a production SRE is paged with, and
stripping it would make the eval *harder than reality* rather than honest. Meanwhile
a substring check passes happily on ``fault_category="dependency_unavailable"``,
which is a taxonomy label no telemetry emits.

So this module forbids **named fields** and the **exact values** of the fields that
constitute the answer, and deliberately permits observable text that happens to
share vocabulary with them.

``scenario_id`` is withheld too
-------------------------------
``RCAInput.scenario_id`` is documented as a hint the failure-injection runner sets.
For the ecommerce suite its value (``order_service_postgres_down``) is the failure
key with the dot swapped for an underscore, and ``agent._fallback_verdict`` branches
on it directly. Passing it would let the agent short-circuit to a hand-written
verdict for the very scenario under test. The evaluation therefore never sets it,
which is also what a production alert webhook does — it has no such field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TRUTH_FIELDS: frozenset[str] = frozenset(
    {
        "root_cause",
        "root_cause_keywords",
        "fault_category",
        "failure_key",
        "remediation",
        "grading",
        "expected_signals",
        "exercises",
        "scenario",
        "scenario_id",
        "real_cause",
        "expected_rca",
        "expected_fix",
        "known_wrong_fixes",
        "ranked_hypotheses",
        "l1",
        "l2",
        "rca",
        "injection",
        "injection_method",
        "env_var",
    }
)
"""Keys that may never appear anywhere in an RCA input, at any nesting depth.

Covers both truth-file schema families: ``root_cause``/``fault_category``/
``failure_key`` (ecommerce JSON), ``real_cause``/``expected_rca``/``expected_fix``/
``known_wrong_fixes``/``ranked_hypotheses`` (OTel YAML), and the ``l1``/``l2``/
``rca`` truth fields plus injection-mechanism keys named in the brief.

Listed as data rather than filtered by prefix because a prefix rule cannot express
"``expected_alert_payload`` is fine but ``expected_signals`` is not" — the two differ
by intent, not by shape.
"""

_ANSWER_VALUE_FIELDS: tuple[str, ...] = (
    "root_cause",
    "failure_key",
    "fault_category",
    "remediation",
)
"""Fields whose *values* are the answer, and so must not appear as text anywhere in
the input either. Guards against a future adapter helpfully copying the cause into
an alert description — a field-name check alone would not catch that."""

_MIN_DISTINCTIVE_LENGTH = 12
_SEPARATORS = ("_", ".", " ", "-")


def _is_distinctive(value: str) -> bool:
    """Whether a truth value is specific enough that finding it is proof of a leak.

    This guard has to tell an *answer* from a *word*. ``fault_category`` for the
    latency scenario is literally ``"latency"``, and the observable metric
    ``order_latency_seconds`` contains it — so a naive value check rejects a
    perfectly legitimate metric name and the blindness guard becomes something
    people switch off. Meanwhile ``"dependency_unavailable"`` is a taxonomy label no
    telemetry emits, and ``"order_service.postgres_down"`` is a failure key, and
    either appearing in an agent input is unambiguously a leak.

    So a value is checked only when it is long or structured — twelve-plus
    characters, or containing a separator. Single short words are treated as shared
    vocabulary rather than as the answer. The residual risk is a one-word cause
    slipping through the value check; the field-name check still covers the field
    itself, which is the realistic leak path.
    """
    return len(value) >= _MIN_DISTINCTIVE_LENGTH or any(s in value for s in _SEPARATORS)


class TruthLeakError(AssertionError):
    """Raised when truth-file content reaches an RCA input.

    An ``AssertionError`` subclass so it reads as the invariant violation it is, and
    a named type so a test can assert on it specifically rather than on any
    assertion failing for any reason.
    """


def load_truth(path: Path) -> dict[str, Any]:
    """Parse one truth file. JSON (ecommerce) or YAML (OTel demo)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def discover_ecommerce_truth_files() -> list[Path]:
    """The 12 ecommerce scenarios — the RCA evaluation set.

    Only this family: the OTel YAML files describe the Astronomy Shop, which is no
    longer deployed, and scoring RCA against a system that does not exist is how
    the stale ``productCatalogFailure`` golden came about in the first place.
    """
    directory = REPO_ROOT / "demo" / "ecommerce" / "truth_files"
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.json") if not p.stem.startswith("_"))


def _walk_keys(value: Any) -> list[str]:
    """Every mapping key anywhere in a nested structure."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(str(key))
            found.extend(_walk_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_walk_keys(child))
    return found


def assert_blind(payload: dict[str, Any], truth: dict[str, Any] | None = None) -> None:
    """Raise :class:`TruthLeakError` if ``payload`` carries truth-file content.

    Two independent checks, because either alone has a hole:

    1. **No forbidden key**, at any depth — catches a whole truth block being
       passed through.
    2. **No answer value as text** — catches the cause being copied into an
       otherwise innocent field. Only run when ``truth`` is supplied, since it needs
       to know what this scenario's answer actually is.

    The value check is case-insensitive and only considers values ``_is_distinctive``
    judges specific enough to be the answer rather than shared vocabulary — see
    there for the concrete false positive that rule exists to avoid.
    """
    offending = sorted(set(_walk_keys(payload)) & FORBIDDEN_TRUTH_FIELDS)
    if offending:
        raise TruthLeakError(
            "RCA input carries truth-file field(s) the agent must never see: "
            f"{', '.join(offending)}. Truth belongs to the harness, not the agent."
        )
    if not truth:
        return

    haystack = json.dumps(payload, default=str).lower()
    for field_name in _ANSWER_VALUE_FIELDS:
        raw = truth.get(field_name)
        if not isinstance(raw, str):
            continue
        needle = raw.strip().lower()
        if _is_distinctive(needle) and needle in haystack:
            raise TruthLeakError(
                f"RCA input contains the value of truth field {field_name!r} "
                f"({raw!r}) as text. That is the answer, not an observation."
            )


def _severity_label(hint: str) -> str:
    """Map a truth file's ``severity_hint`` onto RA-001's Sev-N vocabulary.

    The alert payload speaks Prometheus severities (``critical``/``warning``) and
    the triage verdict speaks ``Sev-1``..``Sev-4``. Observable either way — this is a
    vocabulary translation, not a hint about the cause.
    """
    return {"critical": "Sev-1", "warning": "Sev-2", "info": "Sev-3"}.get(
        hint.strip().lower(), "Sev-2"
    )


def triage_verdict_from_truth(truth: dict[str, Any]) -> dict[str, Any]:
    """Synthesise the RA-001 verdict RCA would have received, from observables only.

    Built from ``expected_alert_payload`` — the alert that fires — and nothing else.
    Every field here is something the monitoring stack produces: an alert id, the
    service the alert is labelled with, the metric and its value against its
    threshold, and a summary phrased the way RA-001 phrases one.

    Deliberately synthesised rather than obtained by running ``alert_triage`` first:
    chaining agents would make an RCA score depend on triage's behaviour, so an
    RA-001 regression would show up as an RCA accuracy drop. Chaining is the more
    end-to-end measurement and belongs in a separate flow eval.
    """
    payload = truth.get("expected_alert_payload") or {}
    labels = payload.get("labels") or {}
    service = str(payload.get("service") or truth.get("service") or "unknown")
    metric = str(payload.get("metric") or "unknown_metric")
    value = payload.get("value")
    threshold = payload.get("threshold")
    alert_id = str(payload.get("alert_id") or f"ALT-{service}")
    alertname = str(labels.get("alertname") or "")

    summary = f"{service} {metric} at {value} against threshold {threshold} (source: Prometheus)."
    if alertname:
        summary = f"{alertname} firing: {summary}"

    return {
        "affected_service": service,
        "severity": _severity_label(
            str(payload.get("severity_hint") or labels.get("severity") or "")
        ),
        "confidence_score": 0.75,
        "alert_summary": summary,
        "assigned_team": "Platform On-Call",
        "duplicate_alert_count": 1,
        "status": "Active",
        "audit_metadata": {
            "created_at": str(payload.get("timestamp") or "2026-08-03T10:00:00Z"),
            "created_by": "RA-001",
            "source_alerts": [alert_id],
            "decision_trace": [
                f"received alert_id={alert_id} service={service} source=Prometheus",
                "new alert cluster",
                f"fetched metric bundle: {metric}={value} (threshold {threshold})",
                "severity from rule-based mapping",
                "assigned to Platform On-Call via CMDB lookup",
                "generated incident summary",
            ],
        },
    }


def rca_input_from_truth(truth: dict[str, Any]) -> dict[str, Any]:
    """The complete, blind ``RCAInput`` payload for one scenario.

    No ``scenario_id`` (see the module docstring) and no ``context``: the harness
    path deliberately gives RCA nothing but the verdict, which is what makes the
    zero-evidence contract assertions meaningful. ``evals/rca_synthetic.py`` supplies
    a context for the accuracy tier.

    Runs ``assert_blind`` before returning, so the guard is on the production path
    of this module rather than only in a test.
    """
    payload = {"triage_verdict": triage_verdict_from_truth(truth)}
    assert_blind(payload, truth)
    return payload


def expected_from_truth(truth: dict[str, Any]) -> dict[str, Any]:
    """The grading key — **harness only, never passed to an agent.**

    Reads the truth fields ``rca_input_from_truth`` refuses to touch:
    ``grading.match_any_keyword`` (or ``root_cause_keywords``), the failure key the
    correct remediation would clear, and the service and category that must be
    identified.
    """
    grading = truth.get("grading") or {}
    keywords = grading.get("match_any_keyword") or truth.get("root_cause_keywords") or []
    return {
        "scenario_id": str(truth.get("id") or ""),
        "service": str(grading.get("must_identify_service") or truth.get("service") or ""),
        "category": str(grading.get("must_identify_category") or truth.get("fault_category") or ""),
        "keywords": [str(k).strip().lower() for k in keywords if str(k).strip()],
        "failure_key": str(truth.get("failure_key") or ""),
        "root_cause": str(truth.get("root_cause") or ""),
        "signals": truth.get("expected_signals") or {},
    }


__all__ = [
    "FORBIDDEN_TRUTH_FIELDS",
    "TruthLeakError",
    "assert_blind",
    "discover_ecommerce_truth_files",
    "expected_from_truth",
    "load_truth",
    "rca_input_from_truth",
    "triage_verdict_from_truth",
]
