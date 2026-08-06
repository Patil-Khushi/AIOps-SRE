"""Deployment and configuration change context — evidence only.

Scope
-----
This seam answers "what changed around this incident?" and stops there. It does
not claim a change *caused* anything, does not rank changes by suspicion, and
names no root cause. Most outages do follow a change, which is exactly why the
temptation to editorialise is strong and must be resisted here: a collector that
says "this deploy is probably to blame" has made the RCA agent's decision for it,
invisibly and without accountability.

So every record is a statement of fact — this deployment happened, this flag was
flipped, at this time, by this person — and the correlation between that and the
incident is left for a consumer to draw.

Union, not a fallback chain
---------------------------
Unlike the topology and history seams, this one fans out and **merges**. Those
answer a single question where the best available source wins. Here a GitHub
deployment and a feature-flag flip are both true and both relevant; stopping at
the first provider that returned something would silently discard half the
context.

Attribution honesty
-------------------
``author`` is whatever ``git`` was configured with locally, which is *not*
reliably a GitHub account. ``author_username`` is populated only from an API
lookup and stays ``None`` otherwise, so an operator is never shown a git config
string presented as a verified identity.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ChangeSource = StrEnum(
    "ChangeSource",
    {
        "GITHUB": "github",
        "GITLAB": "gitlab",
        "ARGOCD": "argocd",
        "JENKINS": "jenkins",
        "FEATURE_FLAGS": "feature_flags",
        "KUBERNETES": "kubernetes",
        "CONFIGURATION": "configuration",
    },
)


class ChangeType(StrEnum):
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    DEPLOYMENT = "deployment"
    ROLLOUT = "rollout"
    ROLLBACK = "rollback"
    FEATURE_FLAG = "feature_flag"
    CONFIG = "config"


class RollbackStatus(StrEnum):
    """Whether a change was reverted.

    ``UNKNOWN`` is a first-class value, not a default to avoid: a provider that
    cannot see rollback state must say so rather than report ``NONE``, which would
    assert a change is still live when nobody checked.
    """

    NONE = "none"
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class ProviderStatus(StrEnum):
    """Outcome of one provider collection.

    Same four-way split as the topology and history seams, for the same reason:
    an unconfigured GitLab reporting zero deployments must not be readable as
    "nothing shipped".
    """

    COLLECTED = "collected"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ChangeRecord(BaseModel):
    """One change, as fact.

    A single model across all seven sources rather than a hierarchy per provider:
    the consumer wants one chronological list of what changed, and seven shapes
    would push the merging burden onto every reader. Providers populate the fields
    they can observe and leave the rest ``None`` — an absent field means "this
    source does not know", never zero or false.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str
    change_type: ChangeType
    source: str
    timestamp: datetime | None = None
    service: str | None = None
    summary: str | None = None
    """Short human-readable description — what a responder reads first."""

    # ── deployment / VCS ──
    deployment_id: str | None = None
    commit_sha: str | None = None
    commit_message: str | None = None
    author: str | None = None
    """Git-configured author name. Explicitly not a verified account identity."""

    author_email: str | None = None
    author_username: str | None = None
    """Platform account handle, populated **only** from an API lookup.

    Left ``None`` rather than inferred from ``author``: a git config string shown
    as a GitHub account is a false attribution, and attributing a change to the
    wrong person during an incident is a real harm, not a cosmetic one."""

    url: str | None = None

    # ── rollback ──
    rollback_status: RollbackStatus = RollbackStatus.UNKNOWN

    # ── feature flags ──
    feature_flags: dict[str, str] = Field(default_factory=dict)
    """Flag name to active variant. A flipped flag is a change even though nothing
    was deployed, which is why it belongs in this list at all."""

    # ── configuration ──
    configuration_version: str | None = None
    """Version identifier of the config object — a Kubernetes ``resourceVersion``,
    a config-map generation, or a checksum, depending on source."""

    raw_detail: str | None = None


class ChangeContextResult(BaseModel):
    """One provider's contribution, with provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    status: ProviderStatus
    records: list[ChangeRecord] = Field(default_factory=list)
    error: str | None = None
    note: str | None = None
    latency_ms: float = 0.0

    @property
    def collected(self) -> bool:
        return self.status is ProviderStatus.COLLECTED and bool(self.records)


class ChangeContext(BaseModel):
    """Merged change context across every provider that answered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records: list[ChangeRecord] = Field(default_factory=list)
    sources_collected: list[str] = Field(default_factory=list)
    sources_unavailable: list[str] = Field(default_factory=list)
    """Providers that could not be reached or were not configured.

    Reported explicitly because an empty record list is ambiguous otherwise: it
    could mean nothing changed, or that every source was unreachable. Those are
    opposite conclusions during an incident."""

    coverage_note: str | None = None

    @property
    def complete(self) -> bool:
        """Whether every configured source answered.

        ``False`` means the change picture has holes — so absence of a deployment
        record is not evidence that no deployment happened.
        """
        return not self.sources_unavailable


class ChangeContextProvider:
    """What a change-context provider must implement."""

    name: str = "base"
    source: str = "base"

    def health(self) -> tuple[bool, str]:
        """``(healthy, detail)``. Must not raise."""
        raise NotImplementedError

    def collect(
        self, service: str, window_start: datetime, window_end: datetime
    ) -> ChangeContextResult:
        """Gather changes in the window. Must not raise — a change-context outage
        must never break the correlation that asked for it."""
        raise NotImplementedError
