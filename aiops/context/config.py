"""Configuration for the Context Engineering Layer.

Every read here is **per call**, never snapshotted into a module-level constant.

That is not a style preference, it is a lesson this repo already paid for. RA-007's
three opt-in gates (``AIOPS_INCIDENT_HISTORY``, ``AIOPS_CHANGE_CONTEXT``,
``AIOPS_TIMELINE_K8S``) are evaluated at import, so by the time a pytest fixture
runs, the value is already baked in and ``monkeypatch.delenv`` changes nothing —
``tests/conftest.py::_opt_in_enrichment_seams_off`` has to reach into the module
object and patch the private constant instead. A per-call read means
``monkeypatch.setenv`` and ``delenv`` both work, no fourth such fixture is needed,
and a developer's ``.env`` cannot silently put their laptop on a different code
path than CI.
"""

from __future__ import annotations

import os
from typing import Literal

ContextMode = Literal["off", "shadow", "on"]

_VALID_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})

DEFAULT_MODE: ContextMode = "off"
"""Off until parity is proven per agent.

Every migrated agent keeps its legacy retrieval as a fallback arm while this is
``off``, so the layer can land incrementally without any agent changing behaviour.
"""


def context_mode() -> ContextMode:
    """Which context path the caller should take.

    ``off``     — do not build a context; agents use their existing retrieval.
    ``shadow``  — agents return their *legacy* answer, and the context is built
                  alongside and diffed for reporting only. Doubles the I/O; never
                  a default, never in CI, never during a timed demo.
    ``on``      — agents consume the context and fall back to legacy only when a
                  section is unavailable.

    An unrecognised value degrades to ``off`` rather than raising: a typo in an
    operator's ``.env`` must not take the incident path down.
    """
    raw = os.environ.get("AIOPS_CONTEXT_LAYER", DEFAULT_MODE).strip().lower()
    return raw if raw in _VALID_MODES else DEFAULT_MODE  # type: ignore[return-value]


def enabled() -> bool:
    """True when the caller should build and consume a context (``on`` only)."""
    return context_mode() == "on"


def shadow_enabled() -> bool:
    """True when the caller should build a context for diffing but not consume it."""
    return context_mode() == "shadow"
