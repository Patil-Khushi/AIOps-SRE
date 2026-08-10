"""Incident evidence timeline for the Log Correlation agent (RA-007).

Naming
------
``CorrelationResult.timeline`` already exists and is a ``list[CorrelatedSignal]``
— the raw, unmerged signal list. It is unchanged. This module builds a *separate*
richer structure exposed as ``CorrelationResult.incident_timeline``, and its entry
type is called ``TimelineEvent`` rather than ``TimelineEntry`` because
``agents.incident_commander.models`` already owns that name for its own
flow-level timeline. Three different "timelines" in one incident is confusing
enough without the types colliding too.

What it adds over the raw signal list
-------------------------------------
The signal timeline answers "what did the telemetry say?". This one answers "what
happened, in order, and what caused what?" — which needs sources the telemetry
does not contain. A latency spike at 10:03 means something quite different if a
deployment rolled out at 10:02, and that fact lives in Kubernetes, not in Loki.

So the timeline unifies six sources:

- **logs / metrics / traces** — derived from Phase 4 ``Evidence``, so each entry
  can point back at the evidence that justifies it.
- **topology** — what the dependency graph looked like, and any cycles found.
- **deployment** — rollouts, restarts, crash-loops (Kubernetes Events).
- **configuration** — feature-flag and ConfigMap changes.

Merging and grouping
--------------------
Two operations, deliberately distinct:

- **Merging** collapses events that are *the same event observed repeatedly* — 50
  restart events for one pod inside a minute is one fact with a count, not 50
  facts. Merged entries union their ``related_evidence_ids`` so no provenance is
  lost.
- **Grouping** tags events that are *different but related* — a deployment at
  10:02 and the error burst at 10:03 belong to one causal story. Grouping assigns
  a shared ``group_id`` without merging them, because they are separate facts and
  collapsing them would destroy the ordering that makes the story legible.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TimelineSource = Literal[
    "logs",
    "metrics",
    "traces",
    "topology",
    "deployment",
    "configuration",
]

# Events closer together than this are treated as the same moment for merging.
# One minute matches the granularity a responder reasons at ("the deploy, then the
# errors") without collapsing a genuine sequence into a single point.
_MERGE_WINDOW_SECONDS = float(os.environ.get("AIOPS_TIMELINE_MERGE_WINDOW", "60"))

# Events within this distance of each other are candidates for one causal group.
# Wider than the merge window: a rollout and the errors it triggers are minutes
# apart, not seconds.
_GROUP_WINDOW_SECONDS = float(os.environ.get("AIOPS_TIMELINE_GROUP_WINDOW", "300"))

_MAX_ENTRIES = int(os.environ.get("AIOPS_TIMELINE_MAX_ENTRIES", "200"))


class TimelineEvent(BaseModel):
    """One thing that happened, at a point in time.

    Immutable for the same reason ``Evidence`` is: it is a record handed to
    another agent, and an audit trail a consumer can edit is not an audit trail.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    event: str
    """Human-readable description. This is what a responder or an LLM reads, so
    it is a sentence rather than a code."""

    service: str
    severity: str = "info"
    source: TimelineSource
    related_evidence_ids: list[str] = Field(default_factory=list)
    """Evidence this entry is derived from or corroborated by.

    The link back matters: a timeline entry asserting "error burst" is only
    actionable if a reader can reach the underlying log lines. Deployment and
    configuration entries usually have none — nothing in the telemetry produced
    them — and an empty list is the honest representation of that."""

    occurrences: int = 1
    """How many identical observations merged into this entry."""

    group_id: str | None = None
    """Shared by events judged part of one causal story. ``None`` means the event
    stands alone, not that it is unrelated to everything."""

    @property
    def is_change_event(self) -> bool:
        """Whether this represents a human/system *change* rather than a symptom.

        Changes are the most valuable entries in an incident timeline — most
        outages follow one — so consumers need to pick them out cheaply.
        """
        return self.source in {"deployment", "configuration"}


class IncidentTimeline(BaseModel):
    """The assembled, ordered account of an incident."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: str
    service: str
    entries: list[TimelineEvent] = Field(default_factory=list)
    sources_present: list[str] = Field(default_factory=list)
    """Which of the six sources actually contributed.

    Recorded because an absent source is ambiguous: no deployment entries could
    mean nothing was deployed, or that Kubernetes was unreachable. Listing what
    was *present* lets a reader tell the difference instead of assuming."""

    truncated: bool = False
    coverage_note: str | None = None

    @property
    def change_events(self) -> list[TimelineEvent]:
        """Deployment and configuration changes, in order — the usual suspects."""
        return [e for e in self.entries if e.is_change_event]

    def render(self, limit: int = 20) -> str:
        """Compact text rendering for a decision trace or an LLM prompt.

        Provided here so every consumer does not re-derive a format, and capped
        because a 200-entry timeline pasted into a prompt is mostly tokens.
        """
        lines = []
        for e in self.entries[:limit]:
            count = f" (x{e.occurrences})" if e.occurrences > 1 else ""
            lines.append(
                f"{e.timestamp.isoformat()} [{e.source}/{e.severity}] {e.service}: {e.event}{count}"
            )
        if len(self.entries) > limit:
            lines.append(f"... {len(self.entries) - limit} more entr(ies) omitted")
        return "\n".join(lines)


def _merge_key(event: TimelineEvent) -> tuple:
    """Identity used to decide two entries are the same event.

    Timestamp is bucketed rather than exact: two restart events a few seconds
    apart are one fact, and requiring identical timestamps would defeat merging
    entirely for anything sampled or retried.
    """
    bucket = int(event.timestamp.timestamp() // _MERGE_WINDOW_SECONDS)
    return (event.source, event.service, event.event, event.severity, bucket)


def merge_duplicates(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Collapse repeated observations of the same event.

    Unions ``related_evidence_ids`` rather than keeping only the first, because
    the whole point of the field is provenance and dropping half of it would make
    a merged entry less traceable than the entries it replaced. Keeps the earliest
    timestamp: for an event observed repeatedly, when it *started* is the fact
    that matters for ordering against a deployment.
    """
    merged: dict[tuple, TimelineEvent] = {}
    for event in events:
        key = _merge_key(event)
        existing = merged.get(key)
        if existing is None:
            merged[key] = event
            continue
        ids = list(dict.fromkeys([*existing.related_evidence_ids, *event.related_evidence_ids]))
        merged[key] = existing.model_copy(
            update={
                "occurrences": existing.occurrences + event.occurrences,
                "related_evidence_ids": ids,
                "timestamp": min(existing.timestamp, event.timestamp),
            }
        )
    return list(merged.values())


def group_related(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Tag temporally adjacent events with a shared ``group_id``.

    Grouping is *not* merging: these are distinct facts that belong to one story
    (a rollout, then the error burst that followed it). Collapsing them would
    destroy the ordering that makes the story readable, so they keep their
    identity and gain a shared label.

    A group is seeded by proximity in time only. Causality is deliberately not
    asserted — "these happened close together" is what the data supports;
    "this caused that" is the RCA agent's judgement to make, with the timeline as
    input rather than as a conclusion.
    """
    if not events:
        return []
    ordered = sorted(events, key=lambda e: e.timestamp)
    out: list[TimelineEvent] = []
    group_start = ordered[0].timestamp
    group_index = 0
    for event in ordered:
        if (event.timestamp - group_start) > timedelta(seconds=_GROUP_WINDOW_SECONDS):
            group_index += 1
            group_start = event.timestamp
        gid = hashlib.sha256(f"{group_index}|{group_start.isoformat()}".encode()).hexdigest()[:12]
        out.append(event.model_copy(update={"group_id": gid}))
    return out


def build_timeline(
    *,
    correlation_id: str,
    service: str,
    events: list[TimelineEvent],
    sources_present: list[str] | None = None,
    coverage_note: str | None = None,
    max_entries: int | None = None,
) -> IncidentTimeline:
    """Assemble events into a merged, grouped, chronologically ordered timeline.

    Order of operations matters: merge first, then group, then sort. Merging
    before grouping means a group is not skewed by fifty copies of one event;
    sorting last guarantees the final list is chronological regardless of what the
    earlier passes did to it.
    """
    cap = max_entries if max_entries is not None else _MAX_ENTRIES

    merged = merge_duplicates(events)
    grouped = group_related(merged)
    # Secondary sort keys keep the order stable when timestamps tie, so the same
    # input always renders identically — required for a reproducible trace.
    grouped.sort(key=lambda e: (e.timestamp, e.source, e.service, e.event))

    truncated = len(grouped) > cap
    if truncated:
        # Keep the earliest entries: an incident's opening moments carry the
        # trigger, and silently dropping from the front would remove the cause
        # while keeping the symptoms.
        grouped = grouped[:cap]

    present = (
        sources_present if sources_present is not None else sorted({e.source for e in grouped})
    )
    return IncidentTimeline(
        correlation_id=correlation_id,
        service=service,
        entries=grouped,
        sources_present=present,
        truncated=truncated,
        coverage_note=coverage_note,
    )
