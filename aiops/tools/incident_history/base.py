"""Provider interface for historical incident retrieval.

Scope: retrieval only
---------------------
This seam answers exactly one question — "which past incidents look like this
one?" — and deliberately stops there. It does not rank causes, does not name a
probable root cause for the *current* incident, and does not recommend a fix.
Those are the RCA agent's job, and conflating retrieval with inference is how a
"we've seen this before" hint quietly becomes an unearned conclusion.

The distinction that keeps this honest: a past incident's recorded cause and
resolution are **historical facts** and therefore evidence. Asserting that the
current incident has the same cause is **inference**, and nothing here does it.
``IncidentMatch`` carries what happened last time; it contains no field for what
is happening now.

Vendor neutrality
-----------------
Four backends are supported behind one interface — vector store, Elasticsearch,
Postgres, and a static mock — because CLAUDE.md principle #1 requires every
external dependency to sit behind a thin internal interface with documented
alternatives. Swapping Qdrant for pgvector must be configuration, not a rewrite.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RetrievalStatus(StrEnum):
    """Outcome of one provider lookup.

    Mirrors the topology chain's four-way distinction for the same reason: "asked
    and there genuinely are no similar incidents" is a useful answer, while "could
    not ask" is not, and collapsing them would let an unconfigured backend look
    like a clean history.
    """

    MATCHED = "matched"
    """Queried successfully and found at least one similar incident."""

    EMPTY = "empty"
    """Queried successfully; nothing similar in the corpus. A real answer."""

    UNAVAILABLE = "unavailable"
    """Backend not configured or not reachable. Not a malfunction, and explicitly
    not the same as an empty history."""

    FAILED = "failed"
    """The query was attempted and errored."""


class ResolutionMetadata(BaseModel):
    """What happened to the *past* incident — history, not advice.

    Every field describes a completed event. There is deliberately no
    ``recommended_action`` or ``suggested_fix``: the moment this object suggests
    what to do now, retrieval has become recommendation, and the caller can no
    longer tell evidence from inference.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved: bool = False
    resolution_summary: str | None = None
    """What was actually done to resolve it, as recorded at the time."""

    resolved_at: datetime | None = None
    time_to_resolve_minutes: float | None = None
    resolved_by: str | None = None
    recorded_cause: str | None = None
    """The cause established for *that* incident after the fact.

    A historical finding, not a claim about the current one. Named
    ``recorded_cause`` rather than ``root_cause`` so no consumer mistakes it for
    a verdict on the incident being investigated."""

    ticket_ref: str | None = None
    runbook_ref: str | None = None

    recorded_hypothesis_class: str | None = None
    """Which failure *class* was concluded for that incident, when the backend records
    structured RCA outcomes rather than only prose.

    Additive and optional: a corpus of hand-written truth files has no such value and
    leaves it ``None``. It exists because matching a remembered sentence against a
    candidate cause by keyword is guesswork, while two records naming the same failure
    class agree structurally. Still a historical fact about the past incident — it says
    which class was concluded *then*, not which applies now.

    Named ``…_class`` rather than ``…_id`` after a bug worth remembering: RCA's
    ``Hypothesis.hypothesis_id`` is ``digest(incident_id, rule_id)``, so it is unique
    *per incident*. Keying recall on it silently matched nothing at all — every prior
    attached to no hypothesis, and memory measurably did nothing while appearing to work.
    The class (``Hypothesis.category``, equal to the catalog rule id) is the value that is
    stable across incidents."""


class IncidentMatch(BaseModel):
    """One past incident judged similar to the query, with why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str
    similarity_score: float = Field(ge=0.0, le=1.0)
    """How close the match is, 0-1. A retrieval score, not a confidence that the
    past cause applies now."""

    title: str | None = None
    occurred_at: datetime | None = None

    matching_signatures: list[str] = Field(default_factory=list)
    """Error signatures shared with the current incident — the substance of the
    match, and what lets a reader judge it rather than trust the score."""

    matching_services: list[str] = Field(default_factory=list)
    matching_topology: list[str] = Field(default_factory=list)
    """Dependency-graph overlap: services related to both incidents in the same
    way. Two incidents on the same service with different dependency shapes are
    weaker evidence than the score alone suggests."""

    resolution: ResolutionMetadata | None = None
    provider: str = "unknown"
    match_explanation: str | None = None
    """Why this scored as it did, so a low-quality match is visible as such
    instead of hiding behind a number."""


class RetrievalQuery(BaseModel):
    """What to search for.

    Built from a correlation result, but expressed in plain terms so a provider
    needs no knowledge of RA-007's models — a retrieval backend should not have to
    import an agent's schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    signatures: list[str] = Field(default_factory=list)
    services_involved: list[str] = Field(default_factory=list)
    topology: list[str] = Field(default_factory=list)
    limit: int = 5
    min_similarity: float = 0.1
    """Floor below which a match is noise rather than evidence."""


class RetrievalResult(BaseModel):
    """A provider's answer, with provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    status: RetrievalStatus
    matches: list[IncidentMatch] = Field(default_factory=list)
    error: str | None = None
    note: str | None = None
    latency_ms: float = 0.0
    corpus_size: int | None = None
    """How many incidents were searched.

    Included because a similarity score is uninterpretable without it: "no
    matches" from a corpus of three past incidents means something very different
    from the same answer over ten thousand."""

    @property
    def matched(self) -> bool:
        return self.status is RetrievalStatus.MATCHED and bool(self.matches)


class IncidentHistoryProvider:
    """What a retrieval backend must implement.

    A plain base class rather than a Protocol so the shared scoring helpers below
    can be inherited — every backend needs the same notion of "similar", and
    duplicating it per provider is how four backends end up disagreeing about
    what a 0.7 means.
    """

    name: str = "base"

    def health(self) -> tuple[bool, str]:
        """``(healthy, detail)``. Must not raise."""
        raise NotImplementedError

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        """Find similar past incidents. Must not raise — every failure mode is a
        ``RetrievalResult`` status, so a retrieval outage cannot break the
        correlation that asked for it."""
        raise NotImplementedError


def jaccard(a: list[str], b: list[str]) -> float:
    """Set overlap of two string lists, 0-1.

    Chosen over a raw shared count because count favours incidents with many
    recorded signatures regardless of relevance — a past incident listing fifty
    signatures would out-score a precise two-signature match. Jaccard normalises
    by the union, so it measures proportion of agreement.
    """
    sa = {s.strip().lower() for s in a if s and s.strip()}
    sb = {s.strip().lower() for s in b if s and s.strip()}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return round(inter / union, 4) if union else 0.0


def overlap(a: list[str], b: list[str]) -> list[str]:
    """Sorted intersection, for reporting *which* items matched."""
    sa = {s.strip().lower() for s in a if s and s.strip()}
    sb = {s.strip().lower() for s in b if s and s.strip()}
    return sorted(sa & sb)


# Words too common in incident text to carry signal. Without this, "service" and
# "error" alone would make every incident look similar to every other.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "was",
        "are",
        "were",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "with",
        "from",
        "by",
        "service",
        "error",
        "errors",
        "failed",
        "failure",
        "alert",
        "alerts",
        "firing",
        "high",
        "rate",
        "active",
        "variant",
        "config",
        "true",
        "false",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(values: list[str], *, min_length: int = 3) -> set[str]:
    """Lowercase content words from a list of strings.

    Short tokens and stopwords are dropped: they inflate similarity without
    carrying meaning, and an inflated score is worse than a missed match here
    because it presents an unrelated incident as precedent.
    """
    tokens: set[str] = set()
    for value in values or []:
        # Split CamelCase *before* lowercasing. Alert names are written
        # "PaymentErrorRateHigh", which without this is a single opaque token that
        # cannot match the word "payment" in a log line — the exact case that made
        # token scoring return 0.0 for two descriptions of the same event.
        split = _CAMEL_BOUNDARY.sub(" ", str(value))
        for tok in _TOKEN_RE.findall(split.lower()):
            if len(tok) >= min_length and tok not in _STOPWORDS:
                tokens.add(tok)
    return tokens


def token_jaccard(a: list[str], b: list[str]) -> float:
    """Token-level overlap between two sets of strings, 0-1.

    Exists because exact-string ``jaccard`` on signatures scores 0 across
    differently-worded sources, which was observed here: the agent produces
    "Payment charge failed: payment service unavailable" while the recorded
    incident says "PaymentErrorRateHigh alert firing". Same event, no shared
    string, so exact matching found nothing at all.

    Kept as a *separate, lower-weighted* dimension rather than replacing exact
    matching: a verbatim signature match is much stronger evidence than shared
    vocabulary, and collapsing them would make a loose word overlap look like a
    precise hit.
    """
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return round(len(ta & tb) / union, 4) if union else 0.0
