"""Vector-store, Elasticsearch and Postgres incident-history providers.

All three share one posture: **unconfigured is not the same as empty**. Each
reports ``UNAVAILABLE`` with a reason when its backend is absent, so a caller can
never read "no similar incidents" off a database that was never connected. That
distinction is the whole reason these are separate statuses.

Why they are honest stubs rather than fabricated implementations
---------------------------------------------------------------
None of these backends exists in this deployment — there is no Qdrant, no
Elasticsearch, and the Postgres instance present is the demo's own application
database, not an incident corpus. Writing speculative query code against schemas
nobody has agreed would produce something that looks finished, cannot be run, and
would need rewriting the moment a real backend appeared.

So each provider implements the parts that are genuinely knowable now — config
detection, health reporting, the query shape, result mapping, and correct status
semantics — and raises no pretence about the rest. The query strings are the ones
that would actually be issued, so wiring a real backend is a connection change
rather than a redesign.
"""

from __future__ import annotations

import logging
import os
import time

from aiops.tools.incident_history.base import (
    IncidentHistoryProvider,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStatus,
)

logger = logging.getLogger(__name__)


def _unavailable(provider: str, detail: str, started: float) -> RetrievalResult:
    return RetrievalResult(
        provider=provider,
        status=RetrievalStatus.UNAVAILABLE,
        note=detail,
        latency_ms=(time.monotonic() - started) * 1000.0,
    )


class VectorIncidentHistoryProvider(IncidentHistoryProvider):
    """Semantic retrieval from a vector store (pgvector or Qdrant).

    The strongest of the four in principle: it can match an incident described in
    different words, which signature-set overlap cannot. It also needs an
    embedding model, so it is the most expensive and the easiest to have silently
    misconfigured — hence the explicit health reporting.
    """

    name = "vector"

    def _config(self) -> tuple[str, str] | None:
        url = os.environ.get("AIOPS_VECTOR_DB_URL", "").strip()
        collection = os.environ.get("AIOPS_VECTOR_DB_COLLECTION", "incidents").strip()
        return (url, collection) if url else None

    def health(self) -> tuple[bool, str]:
        cfg = self._config()
        if cfg is None:
            return False, "AIOPS_VECTOR_DB_URL not set"
        return True, f"vector store configured (collection={cfg[1]})"

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.monotonic()
        cfg = self._config()
        if cfg is None:
            return _unavailable(self.name, "vector store not configured", started)
        # A configured-but-unreachable store must report FAILED, not EMPTY: the
        # difference decides whether a caller retries or trusts the answer.
        return RetrievalResult(
            provider=self.name,
            status=RetrievalStatus.UNAVAILABLE,
            note=(
                "vector client not installed in this deployment; configure an embedding "
                "provider and vector client to enable semantic retrieval"
            ),
            latency_ms=(time.monotonic() - started) * 1000.0,
        )


class ElasticIncidentHistoryProvider(IncidentHistoryProvider):
    """Full-text retrieval from Elasticsearch.

    Well suited to signature matching: an incident index with the error text
    analysed gives good recall on phrasing variants without an embedding model.
    """

    name = "elastic"

    def _config(self) -> tuple[str, str] | None:
        url = os.environ.get("AIOPS_ELASTIC_URL", "").strip()
        index = os.environ.get("AIOPS_ELASTIC_INCIDENT_INDEX", "incidents").strip()
        return (url, index) if url else None

    def health(self) -> tuple[bool, str]:
        cfg = self._config()
        if cfg is None:
            return False, "AIOPS_ELASTIC_URL not set"
        return True, f"elastic configured (index={cfg[1]})"

    def build_query(self, query: RetrievalQuery) -> dict:
        """The query that would be issued — exposed so it is reviewable and
        testable without a running cluster.

        ``should`` rather than ``must`` on signatures: an incident matching some
        signatures is a legitimate partial match, and requiring all of them would
        return nothing on real data.
        """
        return {
            "size": query.limit,
            "query": {
                "bool": {
                    "should": [{"match": {"signatures": sig}} for sig in query.signatures]
                    + [{"term": {"service": query.service}}]
                    + [{"terms": {"topology": query.topology}}]
                    if query.topology
                    else [{"match": {"signatures": sig}} for sig in query.signatures],
                    "minimum_should_match": 1,
                }
            },
        }

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.monotonic()
        if self._config() is None:
            return _unavailable(self.name, "elasticsearch not configured", started)
        return RetrievalResult(
            provider=self.name,
            status=RetrievalStatus.UNAVAILABLE,
            note="elasticsearch client not installed in this deployment",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )


class PostgresIncidentHistoryProvider(IncidentHistoryProvider):
    """Relational retrieval from a Postgres incident table.

    The most likely production home for this data, since incidents are already
    relational (ticket, service, timestamps, resolution). Uses array overlap for
    signature matching rather than a text search, which keeps scoring explainable.
    """

    name = "postgres"

    def _config(self) -> str | None:
        return os.environ.get("AIOPS_INCIDENT_DB_URL", "").strip() or None

    def health(self) -> tuple[bool, str]:
        if self._config() is None:
            return False, "AIOPS_INCIDENT_DB_URL not set"
        return True, "incident database configured"

    def build_query(self, query: RetrievalQuery) -> tuple[str, dict]:
        """The SQL that would be issued, with bound parameters.

        Parameterised, not interpolated: signatures come from log text, so string
        building here would be an injection path straight from a log line into the
        database.
        """
        sql = """
            SELECT incident_id, title, occurred_at, signatures, services, topology,
                   resolution_summary, recorded_cause, resolved_at, resolved_by,
                   cardinality(array(SELECT unnest(signatures) INTERSECT
                                     SELECT unnest(%(signatures)s::text[])))::float
                     / GREATEST(cardinality(signatures), 1) AS similarity
              FROM incidents
             WHERE signatures && %(signatures)s::text[]
                OR service = %(service)s
          ORDER BY similarity DESC
             LIMIT %(limit)s
        """
        return sql, {
            "signatures": query.signatures,
            "service": query.service,
            "limit": query.limit,
        }

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.monotonic()
        if self._config() is None:
            return _unavailable(self.name, "incident database not configured", started)
        return RetrievalResult(
            provider=self.name,
            status=RetrievalStatus.UNAVAILABLE,
            note="postgres driver not wired for the incident corpus in this deployment",
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
