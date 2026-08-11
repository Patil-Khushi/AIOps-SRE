"""Deterministic correlation ids.

A correlation id ties every observation, cache entry and ranked finding from one
incident together. It is derived from the incident's own coordinates rather than a
UUID or the wall clock, and that choice carries three consequences the layer
depends on:

* **Standalone and orchestrated calls agree without coordination.** An agent
  invoked on its own (each agent is individually sellable, so this is a first-class
  path, not an edge case) derives the same id the orchestrator would have, so it
  hits the same cache entries instead of re-querying every backend.
* **Re-runs are comparable.** Building the same incident's context twice yields the
  same ids, so a second verdict can be diffed against the first rather than merely
  replacing it. The eval harness depends on this for reproducibility.
* **Cache keys are incident-scoped.** Every key in ``cache.py`` embeds the
  correlation id, which is what stops a 60-second TTL on ``oncall.schedule.lookup``
  from serving one incident's on-call engineer to a later incident across a shift
  boundary. That failure mode pages the wrong human, so the scoping is a
  correctness requirement rather than a tidiness one.

The window is bucketed before hashing. Two callers reasoning about the same
incident rarely compute byte-identical timestamps — the orchestrator's window and
an agent's independently-derived one can differ by the seconds it took to get
there — and an unbucketed hash would make those two look like different incidents
and silently double every backend call.
"""

from __future__ import annotations

import os
from datetime import datetime

from aiops.context.models import digest

_DEFAULT_BUCKET_SECONDS = 60.0


def bucket_seconds() -> float:
    """Window-rounding granularity, in seconds.

    Read per call (see ``config.py`` for why this repo avoids import-time
    constants). Sixty seconds by default: long enough to absorb the drift between
    two independently-derived windows for one incident, short enough that two
    genuinely distinct incidents on the same service minutes apart do not collide
    and share stale evidence.
    """
    raw = os.environ.get("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_BUCKET_SECONDS
    return value if value > 0 else _DEFAULT_BUCKET_SECONDS


def _bucket(moment: datetime, granularity: float) -> int:
    """Round a timestamp down to a bucket index.

    Uses the POSIX timestamp so a naive and an aware datetime for the same instant
    cannot bucket differently — the repo mixes both (``Alert.timestamp`` arrives
    from webhooks in either shape), and an id that depended on tzinfo presence
    would split one incident in two.
    """
    return int(moment.timestamp() // granularity)


def derive_correlation_id(
    service: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Stable id for one incident's context.

    Mirrors the intent of ``agents/log_correlation/evidence.py``'s
    ``make_correlation_id``, reimplemented here because ``aiops/`` may not import
    ``agents/``. The two are deliberately *not* required to produce equal values:
    RA-007's ids identify its own evidence records, these identify context sections,
    and coupling them would mean neither could change its inputs without breaking
    the other.
    """
    granularity = bucket_seconds()
    return digest(
        "ctx",
        service.strip().lower(),
        _bucket(window_start, granularity),
        _bucket(window_end, granularity),
    )
