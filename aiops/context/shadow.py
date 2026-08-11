"""Shadow-mode comparison recording — how the rollout earns the right to flip on.

The Context Engineering Layer ships behind ``AIOPS_CONTEXT_LAYER=off|shadow|on``
(see ``config.py``). In ``shadow``, an agent returns its **legacy** answer and the
context-derived answer is computed alongside and compared. This module records those
comparisons.

Why this exists rather than "just read the tests"
-------------------------------------------------
The eval harness forces synthetic evidence — ``log_correlation.run()`` passes
``force_synthetic=True`` specifically so goldens are reproducible with zero I/O — so
CI never exercises the live fan-out at all. A parity test proves the adapters agree
on *fixture* data; only shadow mode against a real cluster proves they agree on what
Prometheus, Loki and Jaeger actually return. Flipping the default without that
evidence would be trusting a proof that structurally cannot cover the case it is
being cited for.

Structural diffs, not string equality
-------------------------------------
A boolean "these differed" is useless for the one decision this module supports. If
the legacy and context paths disagree, the question is immediately *how*: a log-line
ordering difference is a shrug, a whole missing evidence category is a blocker. So
dicts report which **keys** differ and lists report length plus the **first**
differing index.

Never affects the caller
------------------------
``record_diff`` catches everything and returns ``False`` on error. It is diagnostic
instrumentation on the incident path: a bug in a comparison must not change an
agent's verdict, its decision trace, or anything it persists. This is the one module
in the package that is allowed process-global state, and consequently the one that
must expose ``reset_for_tests``.

Both answers are redacted before they are compared
--------------------------------------------------
The ``from_context`` side has already been through stage 5 of the pipeline; the
``legacy`` side is whatever the agent's own live retrieval returned, unscrubbed. That
asymmetry breaks this module twice over, so ``record_diff`` scrubs both sides with the
same ``redactor.redact_text`` before diffing, and scrubs the resulting description
again before it is logged or buffered.

*Security.* A mismatch description embeds the differing values verbatim, and
``record_diff`` writes it to ``logger.warning`` and keeps it in a process buffer that
``diffs()`` hands back. Comparing a raw payload against a scrubbed one therefore routed
unredacted evidence into a log stream on every disagreement — during the one mode whose
whole purpose is to run against a real cluster. Stage 5 exists so that never happens;
skipping it here was a hole in the same control.

*Correctness.* Raw-versus-scrubbed also makes redaction itself look like a divergence:
an incident containing one email would report ``user@x.com != [REDACTED_EMAIL]`` as a
mismatch, and the rollout gate is ``mismatches == 0``. Left alone it would block the
flip on the layer working exactly as designed.

Scrubbing both sides is the right comparison, not a compromise: in ``on`` mode the
agent consumes the *redacted* context, so "would this agent have behaved the same" is
a question about post-redaction evidence. Two distinct secrets in the same position do
collapse to one placeholder and read as agreement — that is a real if narrow loss, and
it is the correct trade, because the alternative is a gate that can never go green.

The description is scrubbed as well as the payloads because it is the only text that
escapes this module, and it is built with ``!r`` over arbitrary objects: a payload the
structural walk below cannot see into (a custom ``__repr__``, a non-``str`` leaf
carrying a token) would otherwise reach the log unscrubbed. Redaction is idempotent, so
the second pass costs nothing on already-clean text.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from aiops.context.redactor import redact_text

logger = logging.getLogger(__name__)

MAX_DIFFS_PER_CONSUMER = 20
"""How many diff descriptions are retained per consumer.

Bounded because the demo server is long-lived and shadow mode runs on every
incident; an unbounded list would grow for the length of a rehearsal. Twenty is
enough to see a pattern, and the counters — which are what you actually gate the
rollout on — are exact regardless of this cap.
"""

# One lock over all shared state, following ``aiops/tools/resilience.py``. The
# collectors fan out concurrently and the demo server is multi-threaded, so these
# dicts are genuinely contended; a lock costs nothing at these call rates.
_lock = threading.RLock()
_stats: dict[str, dict[str, int]] = {}
_diffs: dict[str, deque[str]] = {}


def _record(consumer: str, key: str) -> None:
    with _lock:
        _stats.setdefault(consumer, {}).setdefault(key, 0)
        _stats[consumer][key] += 1


def stats() -> dict[str, dict[str, int]]:
    """Per-consumer counters, keyed exactly as ``_record`` writes them:

    ``comparisons`` · ``matches`` · ``mismatches`` · ``errors``

    Keys are created lazily on first increment, so read with ``.get(key, 0)`` rather
    than indexing — a consumer that has never disagreed has no ``mismatches`` key at
    all. Same convention as ``resilience.stats()``, and enumerated in full here for
    the same reason: a dashboard built from a docstring that named a key wrong got a
    ``KeyError``.

    The gate for flipping ``AIOPS_CONTEXT_LAYER`` to ``on`` is
    ``mismatches == 0`` across a full rehearsal, with ``comparisons`` high enough to
    mean something — a zero mismatch count over three comparisons proves nothing.
    """
    with _lock:
        return {consumer: dict(counters) for consumer, counters in _stats.items()}


def diffs(consumer: str | None = None) -> tuple[str, ...]:
    """Recorded mismatch descriptions, newest last. All consumers when ``consumer`` is
    ``None``, in name order so the output is stable enough to diff between runs."""
    with _lock:
        if consumer is not None:
            return tuple(_diffs.get(consumer, ()))
        return tuple(description for name in sorted(_diffs) for description in _diffs[name])


def reset_for_tests() -> None:
    """Clear all recorded state.

    Required, not a convenience: this module holds process-global counters, and this
    repo's ``tests/conftest.py`` carries ten autouse fixtures that exist precisely
    because process-global state leaked between tests and produced order-dependent
    failures. Wire this into a fixture alongside ``resilience.reset_for_tests()``
    before anything starts calling ``record_diff``.
    """
    with _lock:
        _stats.clear()
        _diffs.clear()


def describe_difference(legacy: Any, from_context: Any, *, path: str = "") -> str | None:
    """A short account of the first meaningful difference, or ``None`` if equal.

    Recurses into dicts and sequences so the description names *where* the two
    disagree rather than dumping both values. Depth is bounded by the structures
    themselves — these are small evidence payloads, not arbitrary graphs — but a type
    mismatch stops the descent, because "a dict here, a list there" is already the
    most useful thing that can be said.
    """
    here = path or "value"

    # A type mismatch is worth reporting as such, with one exemption: ``list`` and
    # ``tuple`` are interchangeable here, because the layer returns tuples where the
    # legacy paths return lists and that difference is an artefact of frozen models,
    # not a disagreement about evidence.
    #
    # The exemption is deliberately narrow. An earlier version exempted any pair where
    # both sides were dict/list/tuple, which meant the one case this function's whole
    # structural descent exists for — a dict on one side, a sequence on the other,
    # i.e. a *whole-shape* divergence — was the single type mismatch never reported as
    # one. It fell through to the value-inequality branch and printed both payloads in
    # full, which is precisely the unreadable output the descent is here to avoid.
    both_sequences = isinstance(legacy, list | tuple) and isinstance(from_context, list | tuple)
    both_mappings = isinstance(legacy, dict) and isinstance(from_context, dict)
    if type(legacy) is not type(from_context) and not (both_sequences or both_mappings):
        return (
            f"{here}: legacy is {type(legacy).__name__}, context is {type(from_context).__name__}"
        )

    if both_mappings:
        # Report the key *sets* first: a missing evidence category is a different
        # class of problem from a category whose contents shifted, and conflating
        # them is what makes a diff unactionable.
        only_legacy = sorted(set(legacy) - set(from_context))
        only_context = sorted(set(from_context) - set(legacy))
        if only_legacy or only_context:
            parts = []
            if only_legacy:
                parts.append(f"missing from context: {only_legacy}")
            if only_context:
                parts.append(f"extra in context: {only_context}")
            return f"{here}: " + "; ".join(parts)
        # Sorted, not insertion order. ``ContextSection.raw`` is ``{query_id: payload}``
        # filled by the builder's concurrent collector fan-out, so insertion order
        # varies between runs over the same incident. Descending in that order would
        # make "the first meaningful difference" depend on which collector happened to
        # finish first, and two identical runs would report the same disagreement under
        # different query ids — while ``diffs()`` promises output stable enough to diff
        # between runs. The key-set branch above already sorts; this now matches it.
        for key in sorted(legacy, key=str):
            nested = describe_difference(legacy[key], from_context[key], path=f"{here}.{key}")
            if nested:
                return nested
        return None

    if both_sequences:
        if len(legacy) != len(from_context):
            return f"{here}: length {len(legacy)} vs {len(from_context)}"
        for index, (left, right) in enumerate(zip(legacy, from_context, strict=True)):
            nested = describe_difference(left, right, path=f"{here}[{index}]")
            if nested:
                # Return on the FIRST difference, not the last. A stream that starts
                # one line late differs at every subsequent index, so reporting the
                # last one buries the single fact that explains all the rest. It is
                # also the cheaper walk on the common case.
                return nested
        return None

    if legacy != from_context:
        return f"{here}: {legacy!r} != {from_context!r}"
    return None


def _redact_deep(value: Any) -> Any:
    """Scrub every string leaf in an arbitrary structure.

    Mirrors ``describe_difference``'s own descent (dict / list-or-tuple / leaf), so
    anything the diff can see into, this can redact into. A non-string leaf — a
    number, a bool, an enum, an ``_Uncomparable``-style object — passes through
    unchanged: ``redact_text`` only knows how to scan text, and nothing else was ever
    going to carry a secret. Always builds new containers, never mutates the input,
    matching this module's "never touches the caller's data" contract.

    No depth guard, deliberately matching ``describe_difference``: a self-referential
    payload recurses here exactly as it does there, and ``RecursionError`` is caught
    by ``record_diff``'s outer ``except`` the same way a raising comparison is.
    """
    if isinstance(value, dict):
        return {key: _redact_deep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_deep(item) for item in value)
    if isinstance(value, str):
        scrubbed, _counts = redact_text(value)
        return scrubbed
    return value


def record_diff(consumer: str, *, legacy: Any, from_context: Any) -> bool:
    """Compare the two answers and record the outcome. Returns ``True`` when equal.

    Never raises and never touches the caller's data — see the module docstring. A
    comparison that blows up is counted as an ``error`` and reported as a non-match,
    because "we could not tell whether these agree" must not be recorded as "they
    agree": that would let the rollout gate pass on the strength of a broken
    comparison.

    Both answers are redacted before they are diffed or described — see "Both answers
    are redacted before they are compared" above. A redaction failure is one more way
    the comparison can blow up, so it is folded into the same outer ``except`` rather
    than given its own path: either way, nothing unscrubbed was compared and nothing
    unscrubbed reaches the log.
    """
    try:
        _record(consumer, "comparisons")
        safe_legacy = _redact_deep(legacy)
        safe_from_context = _redact_deep(from_context)
        description = describe_difference(safe_legacy, safe_from_context)
        if description is None:
            _record(consumer, "matches")
            return True
        _record(consumer, "mismatches")
        # Scrubbed again, not just inherited from the already-redacted payloads above:
        # this is built with `!r` over arbitrary objects (a custom `__repr__`, a
        # non-str leaf holding a token) that the leaf-level walk above could not see
        # into. Redaction is idempotent, so re-scrubbing already-clean text is free.
        description, _counts = redact_text(description)
        with _lock:
            _diffs.setdefault(consumer, deque(maxlen=MAX_DIFFS_PER_CONSUMER)).append(description)
        logger.warning("context shadow mismatch [%s] %s", consumer, description)
        return False
    except Exception:
        _record(consumer, "errors")
        logger.debug("context shadow comparison failed for %s", consumer, exc_info=True)
        return False
