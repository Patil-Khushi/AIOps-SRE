"""Tests for shadow-mode comparison recording — the rollout's evidence base.

One asymmetry shapes almost every test here: a false "they differed" is cheap and a
false "they matched" is expensive. ``mismatches == 0`` over a rehearsal is the gate
for flipping ``AIOPS_CONTEXT_LAYER`` to ``on``, so a comparison that *could not tell*
whether two answers agree must never be counted as agreement. That is why several
tests assert the **absence** of a ``matches`` key rather than the presence of a
particular description: the wording of a diff is allowed to improve, but "we could
not tell" turning into "they agree" would let a broken adapter ship on the strength
of a comparison that never ran.

The other half of the file is about the descriptions being *actionable*. A log-line
ordering difference is a shrug; a whole missing evidence category is a blocker. If
both render as the same wall of JSON nobody triages either, so the tests pin that the
two are distinguishable and that a nested difference is located by path.

Process-global state is reset by ``conftest.py``'s ``_hermetic_resilience`` autouse
fixture, which calls ``shadow.reset_for_tests()`` at both ends of every test. Every
assertion below counts from zero and relies on that.

Two ``xfail(strict=True)`` tests at the bottom document real defects found while
writing this file.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from aiops.context.models import SectionStatus
from aiops.context.shadow import (
    MAX_DIFFS_PER_CONSUMER,
    describe_difference,
    diffs,
    record_diff,
    reset_for_tests,
    stats,
)

WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
TS_EPOCH = WINDOW_START.timestamp()
TS_MICROS = int(TS_EPOCH * 1_000_000)
TS_NANOS = int(TS_EPOCH * 1_000_000_000)


class _Uncomparable:
    """An answer that cannot be compared at all.

    Not contrived: an adapter that hands back a model object, an array or a frame can
    raise from ``__eq__`` or return something that is not a bool. What matters is that
    the recorder learns nothing from it and must not conclude agreement.
    """

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("this comparison blows up")


# --- builders (real provider payload shapes) -----------------------------


def _metrics_payload(*values: str) -> dict[str, Any]:
    """Prometheus' shape, sample values kept as the strings it actually returns."""
    return {
        "query": 'sum(rate(orders_failed_total{service="payment-service"}[5m]))',
        "result_type": "vector",
        "results": [
            {
                "metric": {"__name__": "orders_failed_total", "pod": f"payment-{i}"},
                "value": [TS_EPOCH, value],
            }
            for i, value in enumerate(values)
        ],
    }


def _logs_payload(*lines: str, level: str = "error") -> dict[str, Any]:
    """Loki's shape: streams of ``[nanosecond_string, line]`` pairs."""
    return {
        "streams": [
            {
                "stream": {"level": level, "service_name": "payment-service"},
                "values": [[str(TS_NANOS + i), line] for i, line in enumerate(lines)],
            }
        ]
    }


def _traces_payload(duration_us: int) -> dict[str, Any]:
    return {
        "service": "payment-service",
        "lookback": "15m",
        "trace_count": 1,
        "traces": [
            {
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "span_count": 12,
                "root_operation": "POST /api/checkout",
                "duration_us": duration_us,
                "start_time_us": TS_MICROS,
            }
        ],
    }


def _answer(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """One consumer's answer, shaped like a ``ContextSection.raw`` mapping.

    ``{query_id: provider_payload}`` — one section can hold several queries, so a
    comparison has to descend through a query id before it reaches a payload, and the
    description has to say which query it landed in.
    """
    base: dict[str, Any] = {
        "metrics.errors": _metrics_payload("0.4213"),
        "logs.recent": _logs_payload("connection refused to mysql", "retrying in 2s"),
        "traces.slow": _traces_payload(2_500_000),
    }
    return {**base, **(overrides or {})}


def _counters(consumer: str) -> dict[str, int]:
    """``stats()`` for one consumer, or ``{}`` when it was never recorded."""
    return stats().get(consumer, {})


# --- counters -------------------------------------------------------------


def test_identical_answers_match_and_leave_nothing_to_read():
    """The two payloads are distinct objects, so agreeing requires a real structural
    walk rather than an identity check."""
    payload = _logs_payload("connection refused to mysql", "retrying in 2s")

    assert record_diff("rca", legacy=payload, from_context=copy.deepcopy(payload)) is True
    assert _counters("rca") == {"comparisons": 1, "matches": 1}
    assert diffs("rca") == ()


def test_a_consumer_that_never_disagreed_has_no_mismatches_key_at_all():
    """Read these with ``.get(key, 0)``, never by indexing.

    ``resilience.stats()`` carries the same lazy-key convention and its docstring
    records what happens when the convention is documented wrongly: a dashboard
    indexed a key that only exists after the first increment and raised ``KeyError``
    in front of an operator. ``_record`` creates keys on demand, so their absence is
    part of the contract rather than an artefact of this test's ordering.
    """
    record_diff("rca", legacy=_answer(), from_context=_answer())
    counters = stats()["rca"]

    assert counters.get("mismatches", 0) == 0
    assert counters.get("errors", 0) == 0
    assert "mismatches" not in counters
    assert "errors" not in counters


def test_a_consumer_that_never_agreed_has_no_matches_key_at_all():
    record_diff("rca", legacy={"logs.recent": 1}, from_context={"logs.recent": 2})
    counters = stats()["rca"]

    assert counters == {"comparisons": 1, "mismatches": 1}
    assert "matches" not in counters


def test_a_consumer_that_was_never_recorded_is_absent_from_stats_entirely():
    """So "this agent has not run in shadow mode yet" cannot be read as "this agent
    has run and never disagreed" — the second would satisfy the gate."""
    record_diff("rca", legacy=1, from_context=1)
    assert "log_correlation" not in stats()


def test_differing_answers_record_exactly_one_description():
    """One comparison, one line in the report. Recording the same disagreement twice
    would halve the 20-slot ring and make two consumers' reports incomparable."""
    legacy = _logs_payload("connection refused to mysql")
    context = _logs_payload("connection reset by peer")

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    assert _counters("rca") == {"comparisons": 1, "mismatches": 1}
    assert len(diffs("rca")) == 1


def test_comparisons_accounts_for_every_outcome_exactly_once():
    """``comparisons == matches + mismatches + errors``.

    "Zero mismatches" means nothing without the number of comparisons behind it, and
    an error that never landed in the total would let a run of broken comparisons read
    as a run of clean ones.
    """
    record_diff("rca", legacy=_answer(), from_context=_answer())
    record_diff("rca", legacy={"a": 1}, from_context={"a": 2})
    record_diff("rca", legacy=_Uncomparable(), from_context=_Uncomparable())

    counters = _counters("rca")
    assert counters["comparisons"] == 3
    assert counters["matches"] + counters["mismatches"] + counters["errors"] == 3


def test_stats_returns_a_copy_at_both_levels():
    """A caller assembling a rollout report iterates these dicts; handing back the
    live ones would let its own bookkeeping edit the numbers the gate reads."""
    record_diff("rca", legacy=1, from_context=1)

    snapshot = stats()
    snapshot["rca"]["matches"] = 9999
    snapshot["rca"]["invented"] = 1
    snapshot["fabricated"] = {"comparisons": 5}

    assert stats() == {"rca": {"comparisons": 1, "matches": 1}}


def test_a_stats_snapshot_does_not_track_later_recordings():
    """The mirror of the copy test, in the read direction: a report rendered from a
    live view would mix one incident's numbers with the next one's."""
    record_diff("rca", legacy=1, from_context=1)
    snapshot = stats()
    record_diff("rca", legacy=1, from_context=1)

    assert snapshot["rca"]["comparisons"] == 1


# --- structural diffs ----------------------------------------------------


def test_a_missing_evidence_category_is_distinguishable_from_a_changed_log_line():
    """The distinction the whole structural-diff machinery exists for.

    Both are "the two answers differ". One is a shrug — Loki handed back the same
    lines in a different order — and one is a blocker: the context path produced no
    logs at all. If both rendered as two dumps of the payload, whoever reads the
    rollout report cannot triage them, and the cheap wrong move (block the gate on
    ordering noise, or wave through a missing category) becomes the likely one.
    """
    legacy = _answer()
    reordered = _answer({"logs.recent": _logs_payload("retrying in 2s", "connection refused")})
    absent = {name: payload for name, payload in _answer().items() if name != "logs.recent"}

    ordering = describe_difference(legacy, reordered)
    category = describe_difference(legacy, absent)

    assert ordering is not None
    assert category is not None
    assert ordering != category

    # The lost category names the key that vanished, and says it is missing.
    assert "missing from context" in category
    assert "logs.recent" in category
    # The ordering difference points *inside* a category that is still there.
    assert ordering.startswith("value.logs.recent")
    assert "missing" not in ordering


def test_which_side_lost_a_category_is_visible_in_the_description():
    """Legacy holding a category the context path missed is a hole in the new path;
    the reverse is at worst extra work. A symmetric description would make the
    blocker and the shrug read the same."""
    both = {"metrics.errors": {}, "logs.recent": {}}
    only_metrics = {"metrics.errors": {}}

    context_lost_it = describe_difference(both, only_metrics)
    context_gained_it = describe_difference(only_metrics, both)

    assert context_lost_it is not None
    assert context_gained_it is not None
    assert "logs.recent" in context_lost_it
    assert "logs.recent" in context_gained_it
    assert context_lost_it != context_gained_it


def test_a_renamed_query_id_reports_both_directions_at_once():
    """Reporting only the loss reads as a deletion and sends the reader hunting for a
    category that is right there under a new name."""
    description = describe_difference({"logs.recent": 1}, {"logs.window": 1})

    assert description is not None
    assert "logs.recent" in description
    assert "logs.window" in description


def test_a_length_difference_is_reported_as_a_length():
    """Loki returning nine lines where the legacy path saw ten is the likeliest real
    disagreement, and "3 vs 2" is the whole answer. Descending to the first index
    instead would report the *content* of a line as wrong when only the count is."""
    description = describe_difference(_logs_payload("a", "b", "c"), _logs_payload("a", "b"))

    assert description is not None
    assert description.startswith("value.streams[0].values")
    assert "length" in description
    assert "3" in description
    assert "2" in description


def test_equal_length_lists_report_the_first_differing_index_only():
    """ "First" is load-bearing: a stream that starts one line late differs at every
    index, and a description that reported the last one — or all of them — would bury
    the single fact that explains the rest."""
    description = describe_difference(_logs_payload("a", "b", "c"), _logs_payload("a", "B", "C"))

    assert description is not None
    assert "values[1]" in description
    assert "values[2]" not in description


def test_a_nested_difference_is_located_by_path_rather_than_dumped():
    """dict → list → dict → list → list: a Loki answer nests five levels before it
    reaches a log line. Naming the path is the difference between a report an operator
    reads and one they scroll past."""
    legacy = _answer()
    context = _answer(
        {"logs.recent": _logs_payload("connection refused to mysql", "retried in 2s")}
    )

    description = describe_difference(legacy, context)

    assert description is not None
    # Every level between the answer and the changed line is named, in order.
    assert description.startswith("value.logs.recent.streams[0].values[1][1]")
    assert "retried in 2s" in description
    # ...and the levels that agree are not dragged along.
    assert "metrics.errors" not in description
    assert "connection refused" not in description


def test_the_caller_supplied_path_prefixes_the_description():
    """A report threads the section or query id in. A hard-coded ``"value"`` root
    would give eleven sections eleven identically-rooted lines."""
    top = describe_difference(1, 2, path="logs.recent")
    nested = describe_difference({"streams": [1]}, {"streams": [2]}, path="logs.recent")

    assert top is not None
    assert nested is not None
    assert top.startswith("logs.recent")
    assert nested.startswith("logs.recent.streams[0]")
    assert "value" not in nested


@pytest.mark.parametrize(
    ("legacy", "from_context"),
    [
        ("0.4213", 0.4213),
        (0.4213, "0.4213"),
        (None, "payment-service"),
        ({"events": []}, None),
        (True, "true"),
    ],
    ids=["str-vs-float", "float-vs-str", "none-vs-str", "dict-vs-none", "bool-vs-str"],
)
def test_a_type_mismatch_is_reported_as_a_type_difference(legacy: Any, from_context: Any):
    """Naming both types is more useful than dumping both values, and it is the case
    where dumping is least informative — ``0.4213`` and ``"0.4213"`` print almost
    identically, so a value dump would look like a report about nothing."""
    description = describe_difference(legacy, from_context)

    assert description is not None
    assert type(legacy).__name__ in description
    assert type(from_context).__name__ in description


def test_prometheus_string_samples_are_not_silently_coerced():
    """Prometheus returns sample values as strings — ``value: [epoch, "0.4213"]``. An
    adapter that parses them and one that does not are not producing the same answer,
    and a comparison that coerced would hide exactly the class of divergence shadow
    mode is deployed to find."""
    legacy = _metrics_payload("0.4213")
    context = copy.deepcopy(legacy)
    context["results"][0]["value"][1] = 0.4213

    assert record_diff("rca", legacy=legacy, from_context=context) is False


def test_a_string_is_compared_whole_rather_than_character_by_character():
    """``str`` is a sequence, so a descent written against ``Sequence`` instead of
    ``list | tuple`` would report ``value[16]: 'f' != 's'`` for two log lines — a
    character offset in place of the line that changed."""
    description = describe_difference("connection refused", "connection reset")

    assert description is not None
    assert "[" not in description
    assert "connection refused" in description
    assert "connection reset" in description


def test_a_list_and_a_tuple_of_the_same_items_are_not_a_difference():
    """Every collection in the pack is a ``tuple`` while a legacy answer holds
    ``list``s, and a JSON round-trip turns tuples back into lists. Flagging that would
    fill the report with container-type noise and bury the disagreements that are
    actually about evidence."""
    assert describe_difference(["payment", "cart"], ("payment", "cart")) is None
    assert describe_difference({"dependencies": ["mysql"]}, {"dependencies": ("mysql",)}) is None


# --- "asked and found nothing" is never "could not ask" ------------------


def test_an_empty_result_never_compares_equal_to_an_absent_one():
    """This package's load-bearing distinction, seen from shadow mode.

    ``{"events": []}`` is "we asked Kubernetes and there were none" — a claim about
    the world that RCA renders as "NONE — this signal was checked and was absent" and
    instructs the model to treat as evidence *against* a cause. A missing key or a
    ``None`` is "we could not look". A recorder that called those a match would gate
    the rollout on a comparison blind to the difference, and the flip to ``on`` would
    ship an adapter that turns one into the other.
    """
    empty = {"namespace": "otel-demo", "events": [], "configmaps": []}
    could_not_look = (
        {"namespace": "otel-demo", "configmaps": []},
        {"namespace": "otel-demo", "events": None, "configmaps": []},
    )

    for absent in could_not_look:
        assert describe_difference(empty, absent) is not None
        assert record_diff("rca", legacy=empty, from_context=absent) is False

    assert "matches" not in _counters("rca")


def test_an_empty_status_is_never_reported_as_agreeing_with_unavailable():
    """``SectionStatus`` reaches an answer as a bare string (it is a ``StrEnum`` so it
    serialises without a cast), and ``empty`` against ``unavailable`` is the one
    disagreement that must never be smoothed over."""
    assert (
        record_diff(
            "rca",
            legacy={"logs": SectionStatus.EMPTY.value},
            from_context={"logs": SectionStatus.UNAVAILABLE.value},
        )
        is False
    )
    assert _counters("rca")["mismatches"] == 1


def test_a_stray_enum_is_reported_rather_than_read_as_its_string_value():
    """``SectionStatus.EMPTY == "empty"`` is true, and the two are still reported as
    differing, because types are checked before values. That is the right direction
    for a gate that must not pass on a comparison it could not make — the cost is that
    an adapter serialising the enum on one path only shows up as report noise."""
    description = describe_difference(SectionStatus.EMPTY, "empty")

    assert description is not None
    assert "SectionStatus" in description


# --- errors are never matches -------------------------------------------


def test_a_comparison_that_raises_is_an_error_and_never_a_match():
    """ "We could not tell whether these agree" recorded as "they agree" is the one
    failure that would let the gate pass on a broken comparison. The error lands in
    its own counter, reports a non-match, and does not reach the caller — this runs on
    the incident path, where a diagnostic bug must not change an agent's verdict.
    """
    assert record_diff("rca", legacy=_Uncomparable(), from_context=_Uncomparable()) is False

    counters = _counters("rca")
    assert counters == {"comparisons": 1, "errors": 1}
    assert "matches" not in counters
    assert "mismatches" not in counters
    assert diffs("rca") == (), "an error is not a described difference"


def test_a_raising_comparison_nested_inside_a_payload_is_still_caught():
    """The guard has to wrap the whole descent, not just the outermost call: an object
    that only blows up five levels down is exactly what an adapter leaks."""
    nested = {"streams": [{"stream": {}, "values": [[str(TS_NANOS), _Uncomparable()]]}]}
    legacy = _answer({"logs.recent": copy.deepcopy(nested)})
    context = _answer({"logs.recent": copy.deepcopy(nested)})

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    assert _counters("rca").get("errors") == 1


def test_a_self_referential_payload_is_an_error_rather_than_a_crash():
    """The recursion has no depth guard — the docstring bounds it by "the structures
    themselves" — so a payload pointing at itself has to be caught by the same net as
    any other exploding comparison. A ``RecursionError`` escaping would take down the
    agent whose answer was being sanity-checked."""
    legacy: dict[str, Any] = {"namespace": "otel-demo"}
    legacy["self"] = legacy
    context: dict[str, Any] = {"namespace": "otel-demo"}
    context["self"] = context

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    assert _counters("rca").get("errors") == 1


def test_a_payload_with_mixed_type_keys_is_never_recorded_as_agreement():
    """``sorted()`` over a key set holding both a ``str`` and an ``int`` raises, so
    this pair cannot be *described*. It still must not be called a match: the gate
    then sees a comparison that failed rather than one that passed."""
    assert record_diff("rca", legacy={"1": "a", 1: "b"}, from_context={}) is False

    counters = _counters("rca")
    assert counters["comparisons"] == 1
    assert "matches" not in counters


def test_nan_is_reported_as_a_difference_rather_than_as_agreement():
    """Prometheus returns the literal string ``"NaN"`` for an aggregation over no
    samples. Left as a string it compares equal to itself; parsed to a float it never
    does, so two paths that both produced NaN are reported as differing. Noise, which
    is the survivable direction — the alternative is a comparison that calls two
    unequal values equal. What it must not be is an ``error``: nothing raised."""
    as_string = _metrics_payload("NaN")
    assert record_diff("rca", legacy=as_string, from_context=copy.deepcopy(as_string)) is True

    parsed = _metrics_payload("NaN")
    parsed["results"][0]["value"][1] = float("nan")
    assert record_diff("nan", legacy=parsed, from_context=copy.deepcopy(parsed)) is False
    assert _counters("nan").get("errors", 0) == 0


# --- malformed payloads --------------------------------------------------
#
# Real shapes with a real backend's real omission: these payloads cross version
# boundaries, and nothing in this package may raise on the incident path.

_MALFORMED: list[tuple[str, Any, Any]] = [
    ("logs-streams-none", {"streams": None}, {"streams": []}),
    ("logs-streams-is-a-string", {"streams": "error"}, {"streams": []}),
    ("logs-no-streams-key", {}, {"streams": []}),
    (
        "metrics-value-pair-truncated",
        {"results": [{"metric": {}, "value": [TS_EPOCH]}]},
        {"results": [{"metric": {}, "value": [TS_EPOCH, "1"]}]},
    ),
    ("metrics-results-is-a-dict", {"results": {}}, {"results": []}),
    (
        "traces-count-none",
        {"service": "s", "trace_count": None, "traces": []},
        {"service": "s", "trace_count": 0, "traces": []},
    ),
    (
        "k8s-involved-object-missing",
        {"namespace": "otel-demo", "events": [{"reason": "BackOff"}]},
        {
            "namespace": "otel-demo",
            "events": [{"reason": "BackOff", "involved_object": {"kind": "Pod", "name": "p"}}],
        },
    ),
    (
        "deployments-records-none",
        {"records": None, "sources_collected": [], "sources_unavailable": []},
        {"records": [], "sources_collected": [], "sources_unavailable": []},
    ),
    ("oncall-answer-is-a-bare-string", "ada@example.com", {"team": "Payments"}),
    ("whole-answer-none", None, _answer()),
    ("nested-none-where-a-payload-belongs", {"logs.recent": None}, {"logs.recent": {}}),
]


@pytest.mark.parametrize(
    ("legacy", "from_context"),
    [case[1:] for case in _MALFORMED],
    ids=[case[0] for case in _MALFORMED],
)
def test_no_malformed_payload_is_ever_recorded_as_agreement(legacy: Any, from_context: Any):
    assert describe_difference(legacy, from_context) is not None, (
        "precondition: none of these pairs are equal"
    )

    assert record_diff("rca", legacy=legacy, from_context=from_context) is False
    counters = _counters("rca")
    assert counters["comparisons"] == 1
    assert "matches" not in counters


def test_a_payload_neither_path_could_parse_still_counts_as_agreement():
    """Shadow mode compares the two answers; it does not validate them. Two adapters
    that both faithfully passed through a broken Loki response *do* agree, and
    reporting that as a mismatch would block the gate on the backend's bug instead of
    on a divergence between the two paths."""
    broken = {"status": "success", "streams": None}

    assert record_diff("rca", legacy=broken, from_context=copy.deepcopy(broken)) is True
    assert _counters("rca") == {"comparisons": 1, "matches": 1}


# --- no mutation ---------------------------------------------------------


def test_record_diff_never_mutates_either_answer():
    """Both arguments are live objects on the incident path: the legacy answer is what
    the agent is about to return, and ``ContextSection.raw`` is a plain dict shared
    with every other consumer of the same context. A recorder that sorted a list in
    place or filled in a missing key would corrupt an answer while measuring it.
    """
    legacy = _answer()
    context = _answer({"logs.recent": _logs_payload("connection refused to mysql", "retried")})
    legacy_before = copy.deepcopy(legacy)
    context_before = copy.deepcopy(context)
    inner = legacy["logs.recent"]["streams"][0]["values"]

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    record_diff("rca", legacy=legacy, from_context=copy.deepcopy(legacy))

    assert legacy == legacy_before
    assert context == context_before
    # The same containers, not merely equal ones — a rebuilt sub-dict compares equal.
    assert legacy["logs.recent"]["streams"][0]["values"] is inner


# --- redaction --------------------------------------------------------------
#
# The legacy side is live telemetry that has never been through the redactor; the
# context side has. Comparing them unscrubbed would route raw evidence into
# logger.warning and the diffs() buffer on every mismatch — during shadow mode,
# whose entire point is to run against a real cluster. These tests pin that
# record_diff redacts both sides, and the description, before either reaches a log
# or the buffer.


def test_a_mismatched_email_never_reaches_the_diff_buffer():
    """A real disagreement whose text happens to carry a secret must not surface that
    secret raw. The two lines differ in more than the email — "the outage" vs "a
    different outage" — so this stays a genuine mismatch after both sides redact the
    address to the same placeholder."""
    legacy = _logs_payload("paged oncall@example.com about the outage")
    context = _logs_payload("paged oncall@example.com about a different outage")

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    assert _counters("rca").get("mismatches") == 1

    (description,) = diffs("rca")
    assert "oncall@example.com" not in description
    assert "[REDACTED_EMAIL]" in description


def test_a_matching_secret_on_both_sides_still_reports_no_mismatch():
    """The same secret in the same position redacts to the same placeholder on both
    sides, so the comparison must still see agreement — redacting is not the same as
    corrupting."""
    legacy = _logs_payload("charged card 4111111111111111 for order 9")
    context = _logs_payload("charged card 4111111111111111 for order 9")

    assert record_diff("rca", legacy=legacy, from_context=context) is True
    assert _counters("rca").get("matches") == 1
    assert diffs("rca") == ()


def test_a_card_number_that_only_the_legacy_path_saw_is_not_logged(caplog):
    """The asymmetric case the bug report was about: legacy carries a secret the
    context side never had reason to. record_diff must still not let it through,
    on the object handed to diffs() or on the log line."""
    legacy = _logs_payload("refund failed for card 4111111111111111")
    context = _logs_payload("refund failed for card [absent from context]")

    with caplog.at_level("WARNING"):
        assert record_diff("rca", legacy=legacy, from_context=context) is False

    (description,) = diffs("rca")
    assert "4111111111111111" not in description
    assert "4111111111111111" not in caplog.text


def test_redaction_does_not_change_which_side_is_reported_missing():
    """Scrubbing operates on string leaves only, so it must not disturb the
    key-set comparison a missing evidence category depends on."""
    legacy = _answer({"logs.recent": _logs_payload("paged oncall@example.com")})
    context = {k: v for k, v in legacy.items() if k != "logs.recent"}

    assert record_diff("rca", legacy=legacy, from_context=context) is False
    (description,) = diffs("rca")
    assert "missing from context: ['logs.recent']" in description
    assert "oncall@example.com" not in description


def test_a_non_string_secret_bearing_leaf_is_still_withheld_from_the_description():
    """The leaf-level walk only scrubs ``str`` values, so a secret arriving as
    ``repr()`` text of some other object — a model, a custom type — is not caught by
    that walk. The final ``redact_text`` pass over the assembled description is what
    catches it; this pins that the second pass is load-bearing, not redundant."""

    class _CardLike:
        def __eq__(self, other: object) -> bool:
            return False

        def __repr__(self) -> str:
            return "Token(card=4111111111111111)"

    assert record_diff("rca", legacy=_CardLike(), from_context=_CardLike()) is False
    (description,) = diffs("rca")
    assert "4111111111111111" not in description
    assert "[REDACTED_CARD]" in description


def test_record_diff_still_does_not_mutate_the_caller_answers_when_redacting():
    """Redaction builds new containers; it must not, in the process, reintroduce the
    in-place mutation record_diff otherwise guarantees against."""
    legacy = _answer({"logs.recent": _logs_payload("paged oncall@example.com")})
    context = _answer({"logs.recent": _logs_payload("paged oncall@example.com, retrying")})
    legacy_before = copy.deepcopy(legacy)
    context_before = copy.deepcopy(context)

    assert record_diff("rca", legacy=legacy, from_context=context) is False

    assert legacy == legacy_before
    assert context == context_before


# --- determinism ---------------------------------------------------------


def test_a_key_set_difference_is_reported_in_a_stable_order():
    """Which query id lands in a ``raw`` mapping first is decided by which collector
    finished first — the builder fans out concurrently — so the same disagreement must
    not describe itself differently between two runs. A fixed reversal stands in for
    that shuffle; ``random`` is banned in this package and would not be reproducible
    here anyway."""
    keys = ["metrics.errors", "logs.recent", "traces.slow", "oncall.current"]

    forward = describe_difference(dict.fromkeys(keys, 1), {})
    backward = describe_difference(dict.fromkeys(reversed(keys), 1), {})

    assert forward is not None
    assert forward == backward
    for key in keys:
        assert key in forward


def test_the_same_pair_describes_itself_identically_every_time():
    """A rehearsal's report is diffed against the previous one, so a description that
    varied between calls would show as churn nobody can attribute."""
    legacy = _answer()
    context = _answer({"logs.recent": _logs_payload("connection refused to mysql")})

    first = describe_difference(legacy, context)

    assert first is not None
    assert first == describe_difference(legacy, context)
    assert first == describe_difference(copy.deepcopy(legacy), copy.deepcopy(context))


# --- the storage bound --------------------------------------------------


def test_the_diff_ring_keeps_the_most_recent_while_the_counter_stays_exact():
    """Only the descriptions are capped; the counter is what the rollout is gated on.
    A cap that also stopped counting would make a consumer that disagreed 500 times
    indistinguishable from one that disagreed 20 — and 20 is nowhere near enough
    comparisons to mean anything."""
    overflow = MAX_DIFFS_PER_CONSUMER + 5
    for i in range(overflow):
        record_diff("rca", legacy={"logs.recent": f"line-{i}"}, from_context={"logs.recent": "x"})

    assert _counters("rca") == {"comparisons": overflow, "mismatches": overflow}

    recorded = diffs("rca")
    assert len(recorded) == MAX_DIFFS_PER_CONSUMER
    assert f"line-{overflow - MAX_DIFFS_PER_CONSUMER}" in recorded[0]
    assert f"line-{overflow - 1}" in recorded[-1]
    assert "line-0" not in " ".join(recorded), "the oldest descriptions are the ones evicted"


def test_the_cap_is_per_consumer_not_global():
    """Four agents run in shadow mode over one incident; a shared ring would let the
    chattiest one evict every other consumer's evidence."""
    for i in range(MAX_DIFFS_PER_CONSUMER + 5):
        record_diff("rca", legacy=i, from_context=-1)
    record_diff("log_correlation", legacy=1, from_context=2)

    assert len(diffs("rca")) == MAX_DIFFS_PER_CONSUMER
    assert len(diffs("log_correlation")) == 1


# --- reading the record -------------------------------------------------


def test_diffs_returns_every_consumer_in_name_order_newest_last():
    """Stable enough to diff between runs, per the docstring: two rehearsals that
    disagreed in the same places must produce the same report text, and the order
    consumers first appear in is whatever the orchestrator happened to schedule."""
    record_diff("rca", legacy=1, from_context=2)
    record_diff("alert_triage", legacy=3, from_context=4)
    record_diff("rca", legacy=5, from_context=6)
    record_diff("log_correlation", legacy=7, from_context=8)

    everything = diffs()

    assert len(everything) == 4
    assert everything == diffs("alert_triage") + diffs("log_correlation") + diffs("rca")
    # alert_triage sorts first despite being recorded second.
    assert "3" in everything[0]
    # Within a consumer, oldest first.
    assert "1" in diffs("rca")[0]
    assert "5" in diffs("rca")[1]


def test_diffs_for_a_consumer_that_never_disagreed_is_empty_not_an_error():
    record_diff("rca", legacy=1, from_context=1)

    assert diffs("rca") == ()
    assert diffs("never-ran") == ()


def test_a_diffs_snapshot_neither_edits_nor_watches_the_record():
    """A tuple so a caller cannot append to the evidence, and a copy so it cannot
    watch it either: a live view of the ring would render half of one incident's
    report and half of the next."""
    record_diff("rca", legacy=1, from_context=2)
    snapshot = diffs("rca")
    record_diff("rca", legacy=3, from_context=4)

    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1
    assert len(diffs("rca")) == 2


def test_reset_clears_counters_and_descriptions_together():
    """Half a reset is worse than none: counters cleared with descriptions left behind
    would attribute one test's diff to the next test's zero mismatches."""
    record_diff("rca", legacy=1, from_context=2)
    record_diff("rca", legacy=_Uncomparable(), from_context=_Uncomparable())
    assert stats()
    assert diffs()

    reset_for_tests()

    assert stats() == {}
    assert diffs() == ()
    assert diffs("rca") == ()


# --- concurrency ---------------------------------------------------------


def test_concurrent_recording_loses_no_increments():
    """``record_diff`` is called from the demo server's request threads and from the
    builder's collector fan-out, so these counters are genuinely contended. An
    unlocked read-modify-write drops increments under exactly this load, and a gate
    reading a total that lost updates passes for the wrong reason.
    """
    workers = 8
    rounds = 40
    outcomes = ("match", "mismatch", "error")

    def hammer(worker: int) -> None:
        for i in range(rounds):
            kind = outcomes[i % len(outcomes)]
            if kind == "match":
                record_diff("rca", legacy={"w": worker}, from_context={"w": worker})
            elif kind == "mismatch":
                record_diff("rca", legacy={"w": worker}, from_context={"w": -worker - 1})
            else:
                record_diff("rca", legacy=_Uncomparable(), from_context=_Uncomparable())
            # A fresh consumer per worker as well, so concurrent *key creation* is
            # exercised and not only concurrent increments of an existing key.
            record_diff(f"worker-{worker}", legacy=1, from_context=1)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(hammer, range(workers)))

    per_kind = {
        kind: sum(1 for i in range(rounds) if outcomes[i % len(outcomes)] == kind)
        for kind in outcomes
    }
    counters = _counters("rca")
    assert counters["comparisons"] == workers * rounds
    assert counters["matches"] == workers * per_kind["match"]
    assert counters["mismatches"] == workers * per_kind["mismatch"]
    assert counters["errors"] == workers * per_kind["error"]
    assert len(diffs("rca")) == MAX_DIFFS_PER_CONSUMER

    for worker in range(workers):
        assert _counters(f"worker-{worker}") == {"comparisons": rounds, "matches": rounds}


# --- shape divergence and ordering determinism --------------------------
#
# Both of these were found as real defects while writing this file and were then
# fixed in ``aiops/context/shadow.py``; see the comments in ``describe_difference``.
# They are kept as ordinary tests rather than deleted because each guards a subtle
# regression that would otherwise reappear as "the diff output got less useful",
# which nobody files a bug for.


def test_a_dict_against_a_list_is_reported_as_a_type_difference():
    """The whole-shape divergence the structural descent exists for.

    The type-mismatch guard exempts list-vs-tuple, because the layer returns tuples
    where the legacy paths return lists and that is an artefact of frozen models. An
    earlier version widened that exemption to any dict/list/tuple pair, which made
    dict-vs-sequence — legacy returning ``{query_id: payload}`` against a context path
    returning a list of rows — the one type mismatch never reported as one. It fell
    through to value inequality and printed both payloads in full.
    """
    description = describe_difference({"logs.recent": {}}, [{"logs.recent": {}}])

    assert description is not None
    assert "dict" in description
    assert "list" in description


def test_list_against_tuple_is_not_reported_as_a_type_difference():
    """The exemption that has to survive the fix above.

    ``ContextSection.observations`` is a tuple and every legacy path returns a list;
    reporting that as a disagreement about evidence would make every shadow comparison
    a mismatch and the rollout gate permanently red.
    """
    assert describe_difference(["a", "b"], ("a", "b")) is None


def test_which_difference_is_reported_first_does_not_depend_on_insertion_order():
    context = {"logs.recent": "b", "metrics.errors": "b"}
    logs_first = {"logs.recent": "a", "metrics.errors": "a"}
    metrics_first = {"metrics.errors": "a", "logs.recent": "a"}
    assert logs_first == metrics_first, "precondition: one value, two construction orders"

    assert describe_difference(logs_first, context) == describe_difference(metrics_first, context)
