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
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

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


def record_diff(consumer: str, *, legacy: Any, from_context: Any) -> bool:
    """Compare the two answers and record the outcome. Returns ``True`` when equal.

    Never raises and never touches the caller's data — see the module docstring. A
    comparison that blows up is counted as an ``error`` and reported as a non-match,
    because "we could not tell whether these agree" must not be recorded as "they
    agree": that would let the rollout gate pass on the strength of a broken
    comparison.
    """
    try:
        _record(consumer, "comparisons")
        description = describe_difference(legacy, from_context)
        if description is None:
            _record(consumer, "matches")
            return True
        _record(consumer, "mismatches")
        with _lock:
            _diffs.setdefault(consumer, deque(maxlen=MAX_DIFFS_PER_CONSUMER)).append(description)
        logger.warning("context shadow mismatch [%s] %s", consumer, description)
        return False
    except Exception:
        _record(consumer, "errors")
        logger.debug("context shadow comparison failed for %s", consumer, exc_info=True)
        return False
