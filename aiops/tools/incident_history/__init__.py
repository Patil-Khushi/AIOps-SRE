"""Pluggable historical incident retrieval.

Answers "which past incidents look like this one?" and stops there. It performs no
RCA, names no root cause for the current incident, and recommends no action —
those are inference, and this seam returns evidence.

Usage::

    from aiops.tools.incident_history import RetrievalQuery, search_similar

    attempts = search_similar(RetrievalQuery(service="payment", signatures=[...]))
    for attempt in attempts:
        attempt.provider, attempt.status, attempt.matches

Default chain is ``mock`` (the demo truth files). Vector store, Elasticsearch and
Postgres are opt-in via ``AIOPS_INCIDENT_HISTORY_PROVIDERS`` since none is
configured in this deployment.
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
