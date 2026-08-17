"""Pluggable historical incident retrieval.

Answers "which past incidents look like this one?" and stops there. It performs no
RCA, names no root cause for the current incident, and recommends no action —
those are inference, and this seam returns evidence.

Usage::

    from aiops.tools.incident_history import RetrievalQuery, search_similar

    attempts = search_similar(RetrievalQuery(service="payment", signatures=[...]))
    for attempt in attempts:
        attempt.provider, attempt.status, attempt.matches

Default chain is ``mock`` (the demo truth files). Vector store, Elasticsearch,
Postgres and ``rca_outcomes`` are opt-in via ``AIOPS_INCIDENT_HISTORY_PROVIDERS``
since none is configured or populated in this deployment.

Two populations, and the difference matters
-------------------------------------------
``mock``/``embedding``/``vector``/``elastic``/``postgres`` search the truth-file
corpus — hand-written records of what broke and what fixed it. ``rca_outcomes``
searches *verified RCA outcomes*: past predictions whose recovery a verifier
confirmed. The RCA agent may only recall the second, because the ecommerce truth
files are also its graded evaluation cases and recalling them would be answer
lookup rather than diagnosis. See ``providers/outcomes.py`` for the full argument.
"""

from aiops.tools.incident_history.base import (
    IncidentHistoryProvider,
    IncidentMatch,
    ResolutionMetadata,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStatus,
    jaccard,
    overlap,
)
from aiops.tools.incident_history.retriever import (
    register_provider,
    reset_for_tests,
    search_similar,
)

__all__ = [
    "IncidentHistoryProvider",
    "IncidentMatch",
    "ResolutionMetadata",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalStatus",
    "jaccard",
    "overlap",
    "register_provider",
    "reset_for_tests",
    "search_similar",
]
