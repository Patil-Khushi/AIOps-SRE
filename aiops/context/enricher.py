"""Stage 5 — enrichment: the metadata that makes an observation *actionable*.

Stages 2–4 establish what happened: a latency spike, an error burst, a restart loop.
None of that tells a responder what to do next, and two questions decide that — neither
answerable from telemetry:

* **Who owns this?** A true observation nobody can be paged about is a dead end. The
  owning team, the engineer on shift and the applicable runbook are the difference
  between "checkout p99 tripled" and a page that reaches someone who can act.
* **What changed?** ``recent_change`` is the highest-value key this stage writes. A
  deploy minutes before onset is the most common real-world root cause and the one
  signal metrics, logs and traces *structurally* cannot provide — they can show the
  symptom appearing, never that something shipped. Handing an RCA prompt "this error
  started 90 seconds after deploy ``a1b2c3`` shipped" is worth more than another
  telemetry series.

Why this stage is pure
----------------------
Both facts have already been fetched. ``cmdb``, ``oncall``, ``runbooks`` and
``deployments`` are collected in stage 1 like any other section, so this stage only
*projects* them onto the observations they apply to. It must never call a collector or
the registry: the reason this layer exists is that ``oncall.schedule.lookup`` used to
fire four times per incident, and an enricher that "just double-checks the schedule"
puts the fourth call straight back — while also making stage 5 untestable without
mocks and non-deterministic in the eval harness.

Only known facts get attached
-----------------------------
A key lands in ``Observation.metadata`` only when a source actually named something.
Never ``"unknown"``, never ``""``. A consumer rendering "Owner: {owning_team}" has to
be able to tell "nobody told us" from "the CMDB says the owner is the empty string",
and a placeholder collapses those into one — the same absent-is-not-empty discipline
``SectionStatus`` enforces a level up. Providers that answer with the literal word
"unknown" (a ServiceNow empty reference, ``RollbackStatus.UNKNOWN``) are read as
*not knowing* rather than copied through, because copying them through would fabricate
the distinction they were trying to preserve.

Ownership is scoped to the service it is a fact about
-----------------------------------------------------
The CMDB and on-call lookups asked about the *incident's* service. A context routinely
also carries observations from neighbours — a downstream dependency's error log, an
upstream trace — and stamping the incident service's team onto those would assert an
ownership the CMDB never claimed. Attributing something to the wrong team mid-incident
is a real harm, not a cosmetic one (the same reason
``change_context.ChangeRecord.author_username`` refuses to infer an account from a git
config string). So per-observation ownership is attached only where the fact is about
that observation's service, and the *incident-level* answer — "who do I page for this
incident?" — is what ``ownership_of()`` is for.

``recent_change`` is deliberately *not* scoped that way, and the asymmetry is the point:
ownership is an attribution ("this team owns this thing") while a change is a temporal
fact ("this shipped before that"). The change list belongs to the incident, so noting
that the incident's deploy landed 90 seconds before a *downstream* service started
erroring is exactly the cross-service hint an RCA agent needs, and withholding it would
lose the most valuable correlation in the pack.

What this stage never does
--------------------------
It does not overwrite. Any key already present in an observation's metadata belongs to
whoever wrote it (the normaliser's provider extras, the correlator's cross-source
agreement) and survives untouched, presence being the test rather than truthiness — a
correlator that deliberately wrote ``None`` to mean "checked, unknown" is making a
claim, and clobbering it would erase one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, NamedTuple

from aiops.context.models import Observation
from aiops.context.pack import ContextSection

logger = logging.getLogger(__name__)

OWNING_TEAM = "owning_team"
ONCALL_ENGINEER = "oncall_engineer"
RUNBOOK = "runbook"
RECENT_CHANGE = "recent_change"

ENRICHED_KEYS: frozenset[str] = frozenset({OWNING_TEAM, ONCALL_ENGINEER, RUNBOOK, RECENT_CHANGE})
"""The complete vocabulary this stage writes.

Named rather than left implicit so the redactor, the budgeter and the agent adapters
can reason about enrichment without re-deriving the key list from this module's body,
and so a test can assert that nothing else was added.
"""

__all__ = [
    "ENRICHED_KEYS",
    "ONCALL_ENGINEER",
    "OWNING_TEAM",
    "RECENT_CHANGE",
    "RUNBOOK",
    "enrich",
    "ownership_of",
]


# A provider saying "I do not know" in words. Copying any of these into metadata would
# destroy the not-enriched/enriched-with-nothing distinction the whole stage rests on,
# so they are read as absence. Note what is deliberately *not* here: "Platform On-Call"
# is the CMDB's genuine catch-all team and a real answer a responder can act on.
_NOT_A_FACT = frozenset({"-", "--", "n/a", "na", "none", "null", "nil", "unknown", "unset", "tbd"})

# Precedence when two changes share a timestamp to the microsecond, which happens
# constantly: the SCM seam reports a commit and the deployment that carried it from the
# same event. Prefer the change that demonstrably reached production — naming the commit
# would send a responder to code that may never have shipped.
_CHANGE_WEIGHT: dict[str, int] = {
    "deployment": 0,
    "rollout": 0,
    "rollback": 0,
    "feature_flag": 1,
    "config": 1,
    "commit": 2,
    "pull_request": 2,
}
_UNRANKED_CHANGE = 3
"""Weight for a change type this module has not seen before.

Sorts last among equal timestamps rather than first: an unrecognised type is not
evidence of having reached production, and a new ``ChangeType`` should degrade to
"considered, ranked conservatively" instead of silently outranking a real deployment.
"""


class _Change(NamedTuple):
    """One orderable change candidate, plus the flat projection to attach.

    ``detail`` is built once here rather than per observation because it is copied onto
    potentially hundreds of observations: it stays small and flat on purpose (no nested
    provider payload, no flag map) so ``recent_change`` cannot dominate a token budget.
    The full record is still one hop away in the ``deployments`` section's ``raw``.
    """

    timestamp: datetime
    weight: int
    change_id: str
    detail: dict[str, Any]


def enrich(
    sections: dict[str, ContextSection],
    *,
    incident_service: str,
) -> dict[str, ContextSection]:
    """Return new sections whose observations carry ownership/change metadata.

    Reads the cmdb, oncall, runbooks and deployments sections already present in
    ``sections`` -- it does NOT fetch anything.

    Sections whose ``status.usable`` is false are passed through by identity, both as
    sources (an ``UNAVAILABLE`` CMDB is not an ownership fact) and as targets. Nothing
    is mutated: enriched observations are rebuilt with ``model_copy``.

    An empty ``incident_service`` disables per-observation ownership entirely — there
    is nothing to scope the fact to, and guessing would attribute one service's owner
    to another's evidence. ``ownership_of`` still reports what was collected.
    """
    ownership = ownership_of(sections)
    changes = _changes(sections)
    # Most observations in a section share a timestamp (one Prometheus scrape, one log
    # burst), so the scan over candidate changes is memoised per instant. Local state,
    # so the function stays pure from the outside.
    closest: dict[datetime, _Change | None] = {}

    enriched: dict[str, ContextSection] = {}
    for name, section in sections.items():
        if not section.status.usable or not section.observations:
            enriched[name] = section
            continue
        observations = tuple(
            _enrich_observation(
                observation,
                ownership=ownership,
                changes=changes,
                closest=closest,
                incident_service=incident_service,
            )
            for observation in section.observations
        )
        # Identity comparison, not equality: an observation with nothing to add comes
        # back as the same object, so a section that gained nothing is returned as-is
        # instead of being needlessly rebuilt.
        if all(new is old for new, old in zip(observations, section.observations, strict=True)):
            enriched[name] = section
        else:
            enriched[name] = section.model_copy(update={"observations": observations})
    return enriched


def ownership_of(sections: dict[str, ContextSection]) -> dict[str, str]:
    """Flattened ownership facts (team, on-call engineer, runbook) if available.

    The incident-level answer to "who do I page, and what do they read?", collapsed out
    of three sections into one dict a notification or a prompt can use directly.

    Only keys whose fact is actually known are present, so a caller must treat every
    one as optional; an empty dict means the ownership sections told us nothing. Values
    are strings — a CMDB that answers with a numeric group id is not something a
    responder can page, so it is not reported as ownership.
    """
    facts: dict[str, str] = {}

    cmdb = sections.get("cmdb")
    # ``support_group`` is ServiceNow's own column name; the provider normally maps it
    # to ``team`` for us, but a customer-tailored CMDB reaching this layer unmapped
    # should still resolve rather than silently drop ownership.
    team = _fact(cmdb, ("team", "owning_team", "support_group", "assignment_group"))
    if team is None:
        # The on-call payload echoes the team it was asked about. That echo is the only
        # ownership fact left when the CMDB section is FAILED, and it is a real one:
        # something resolved that team name in order to ask about it.
        team = _fact(sections.get("oncall"), ("team",))
    if team is not None:
        facts[OWNING_TEAM] = team

    # Email before display name: it is the identifier that actually routes (paging, the
    # Slack user map) and it survives a person changing how their name is spelled.
    engineer = _fact(sections.get("oncall"), ("engineer_email", "engineer_name", "engineer"))
    if engineer is not None:
        facts[ONCALL_ENGINEER] = engineer

    runbook = _runbook(sections)
    if runbook is not None:
        facts[RUNBOOK] = runbook

    return facts


# ── ownership extraction ─────────────────────────────────────────────────────


def _runbook(sections: dict[str, ContextSection]) -> str | None:
    """The runbook to read, preferring the section that exists to answer that.

    The ``runbooks`` section is asked explicitly, whereas the CMDB's runbook column is
    an optional customer-tailored field (``u_runbook_url``) a stock ServiceNow PDI does
    not even ship — so a dedicated answer outranks it. The list form is handled because
    a runbook provider may return several and the first is the one it ranked highest.
    """
    runbooks = sections.get("runbooks")
    named = _fact(runbooks, ("runbook", "runbook_url"))
    if named is not None:
        return named
    for payload in _payloads(runbooks):
        listed = _first_in_list(payload.get("runbooks"))
        if listed is not None:
            return listed
    return _fact(sections.get("cmdb"), ("runbook", "runbook_url"))


def _first_in_list(entries: object) -> str | None:
    """First identifiable runbook in a list of strings or of ``{url, name, ...}`` dicts."""
    if not isinstance(entries, list | tuple):
        return None
    for entry in entries:
        if isinstance(entry, str):
            value = _known(entry)
            if value is not None:
                return value
        elif isinstance(entry, dict):
            for key in ("url", "runbook", "runbook_url", "name", "title", "id"):
                value = _known(entry.get(key))
                if value is not None:
                    return value
    return None


def _fact(section: ContextSection | None, keys: tuple[str, ...]) -> str | None:
    """First known value for any of ``keys`` in a usable section."""
    for payload in _payloads(section):
        for key in keys:
            value = _known(payload.get(key))
            if value is not None:
                return value
    # Second place to look: a section may carry observations whose ``raw`` payload did
    # not survive — a context trimmed by the budgeter, or one assembled from normalised
    # observations alone. The fact is the same fact; only its container differs.
    if section is not None and section.status.usable:
        for observation in section.observations:
            for key in keys:
                value = _known(observation.metadata.get(key))
                if value is not None:
                    return value
    return None


def _payloads(section: ContextSection | None) -> tuple[dict[str, Any], ...]:
    """Every dict payload a usable section carries, in a canonical order.

    Sorted by query id rather than taken in insertion order: a section can hold several
    queries, and byte-identical output must not depend on which collector happened to
    merge its result first.
    """
    if section is None or not section.status.usable or not section.raw:
        return ()
    return tuple(
        payload
        for query_id in sorted(section.raw)
        if isinstance(payload := section.raw[query_id], dict)
    )


def _known(value: object) -> str | None:
    """The value as a fact, or ``None`` when the source did not actually name one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in _NOT_A_FACT:
        return None
    return text


# ── change selection ─────────────────────────────────────────────────────────


def _changes(sections: dict[str, ContextSection]) -> tuple[_Change, ...]:
    """Every orderable change from the deployments section, ascending.

    Sorted by ``(timestamp, weight, change_id)`` so the selection scan below can stop
    early and so ties resolve identically on every run — the eval harness compares a
    verdict against its predecessor, which a coin-flip between two same-second changes
    would break.
    """
    section = sections.get("deployments")
    if section is None or not section.status.usable:
        return ()

    changes: list[_Change] = []
    for payload in _payloads(section):
        records = payload.get("records")
        if not isinstance(records, list | tuple):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                change = _change_from_record(record)
            except Exception:  # one malformed record must cost one record, not the stage
                logger.debug("enricher: unparseable change record skipped", exc_info=True)
                continue
            if change is not None:
                changes.append(change)

    if not changes:
        # The raw payload is the richer source, so it wins when present. Falling back to
        # the normalised change observations keeps enrichment working for a context
        # whose raw payloads were dropped, rather than treating "no records key" as
        # "nothing shipped" — which is the exact absent-is-not-empty error this package
        # exists to prevent.
        for observation in section.observations:
            try:
                change = _change_from_record(_record_from_observation(observation))
            except Exception:
                logger.debug("enricher: unparseable change observation skipped", exc_info=True)
                continue
            if change is not None:
                changes.append(change)

    return tuple(
        sorted(changes, key=lambda change: (change.timestamp, change.weight, change.change_id))
    )


def _record_from_observation(observation: Observation) -> dict[str, Any]:
    """View a normalised change observation as a change record.

    Lets one parser serve both shapes the deployments section can present, so the
    tie-break rules and the "no timestamp, no candidate" rule cannot drift apart
    between them.
    """
    metadata = observation.metadata
    return {
        "change_id": metadata.get("change_id") or observation.observation_id,
        "change_type": metadata.get("change_type") or observation.category,
        "timestamp": observation.timestamp,
        "service": metadata.get("service") or observation.service,
        "summary": metadata.get("summary") or observation.evidence,
        "commit_sha": metadata.get("commit_sha"),
        "author": metadata.get("author"),
        "author_username": metadata.get("author_username"),
        "url": metadata.get("url"),
        "source": metadata.get("source"),
        "rollback_status": metadata.get("rollback_status"),
    }


def _change_from_record(record: dict[str, Any]) -> _Change | None:
    """One change candidate, or ``None`` when it cannot be placed in time.

    An undated change is dropped from selection outright. It cannot be shown to precede
    anything, and the alternatives are worse: assuming it is recent would manufacture
    the causal hint this key exists to provide, and assuming it is old would hide a real
    one. It stays visible in the ``deployments`` section either way — this stage is
    declining to *order* it, not hiding it.
    """
    moment = _timestamp(record.get("timestamp"))
    if moment is None:
        return None

    change_type = _known(record.get("change_type")) or _known(record.get("type"))
    identity = (
        _known(record.get("change_id"))
        or _known(record.get("deployment_id"))
        or _known(record.get("commit_sha"))
    )
    detail: dict[str, Any] = {"timestamp": moment.isoformat()}
    for key, value in (
        ("change_id", identity),
        ("change_type", change_type),
        ("source", _known(record.get("source"))),
        ("service", _known(record.get("service"))),
        ("summary", _known(record.get("summary")) or _known(record.get("commit_message"))),
        ("commit_sha", _known(record.get("commit_sha"))),
        # ``author_username`` is a verified platform account; ``author`` is whatever git
        # was configured with locally. Preferring the former keeps the change seam's
        # attribution honesty intact instead of promoting a config string to an identity.
        ("author", _known(record.get("author_username")) or _known(record.get("author"))),
        ("url", _known(record.get("url"))),
        # ``RollbackStatus.UNKNOWN`` self-filters through ``_known``, so this key appears
        # only when someone actually looked — a change already rolled back is much weaker
        # as a suspect, and "nobody checked" must not read as "still live".
        ("rollback_status", _known(record.get("rollback_status"))),
    ):
        if value is not None:
            detail[key] = value

    return _Change(
        timestamp=moment,
        weight=_CHANGE_WEIGHT.get(change_type or "", _UNRANKED_CHANGE),
        change_id=identity or "",
        detail=detail,
    )


def _closest_change(moment: datetime, changes: tuple[_Change, ...]) -> _Change | None:
    """The most recent change at or before ``moment``.

    Inclusive at the boundary: a deploy stamped to the same second as the first failing
    sample is the single most likely cause in the whole context, and excluding it on a
    tie would drop precisely the case this key was written for.

    ``changes`` is ascending by ``(timestamp, weight, change_id)``, so replacing only on
    a strictly newer timestamp leaves the lowest-weight, lowest-id candidate as the
    winner among equal timestamps.
    """
    best: _Change | None = None
    for change in changes:
        if change.timestamp > moment:
            break
        if best is None or change.timestamp > best.timestamp:
            best = change
    return best


def _change_detail(change: _Change, moment: datetime) -> dict[str, Any]:
    """A fresh projection of ``change`` for one observation.

    Fresh per observation rather than shared: ``metadata`` is a plain dict by design in
    this package, and handing the same dict object to twenty observations would let a
    consumer that edits one silently edit all twenty.
    """
    detail = dict(change.detail)
    # Pre-computed because it is what a reader actually reasons with ("shipped 90s
    # before this"), and because deriving it correctly means knowing this module's
    # naive-is-UTC convention — which a consumer should not have to rediscover.
    detail["age_seconds"] = int((moment - change.timestamp).total_seconds())
    return detail


def _timestamp(value: object) -> datetime | None:
    """Parse the two timestamp shapes the deployments section can present.

    A ``datetime`` when the section holds the in-memory ``ChangeRecord``, an ISO-8601
    string once it has been through ``model_dump(mode="json")`` or a cache round-trip.
    Anything else is not a timestamp this stage will guess at.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            return _as_utc(datetime.fromisoformat(value.strip()))
        except ValueError:
            return None
    return None


def _as_utc(moment: datetime) -> datetime:
    """Pin a naive timestamp to UTC so any two timestamps are comparable.

    Comparing a naive and an aware datetime raises ``TypeError``, and this stage
    routinely holds one of each: a provider API timestamp is aware, a normalised sample
    from a payload that omitted its zone is not. UTC matches every producer in this repo
    (``datetime.now(UTC)`` throughout), but the assumption is load-bearing — if a source
    ever hands over local wall-clock time, a deploy would appear hours from the
    observation it caused and the strongest signal here would quietly stop firing.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# ── attachment ───────────────────────────────────────────────────────────────


def _enrich_observation(
    observation: Observation,
    *,
    ownership: dict[str, str],
    changes: tuple[_Change, ...],
    closest: dict[datetime, _Change | None],
    incident_service: str,
) -> Observation:
    """The observation with whatever is known and not already claimed, else itself."""
    additions: dict[str, Any] = {}

    if _same_service(observation.service, incident_service):
        for key, value in ownership.items():
            if key not in observation.metadata:
                additions[key] = value

    # Change observations are excluded: the nearest change at or before a deploy is
    # frequently that deploy itself, and an observation carrying itself as its own
    # antecedent reads like a causal claim while saying nothing.
    if (
        changes
        and RECENT_CHANGE not in observation.metadata
        and observation.source != "deployments"
    ):
        moment = _as_utc(observation.timestamp)
        if moment not in closest:
            closest[moment] = _closest_change(moment, changes)
        change = closest[moment]
        if change is not None:
            additions[RECENT_CHANGE] = _change_detail(change, moment)

    if not additions:
        return observation
    # A new dict every time — never ``observation.metadata.update(...)``. ``frozen=True``
    # stops attribute rebinding but not in-place mutation of a dict field, and this
    # object is shared by every agent reading the context.
    return observation.model_copy(update={"metadata": {**observation.metadata, **additions}})


def _same_service(observed: str, incident_service: str) -> bool:
    """Whether an ownership fact about ``incident_service`` is a fact about ``observed``.

    Case- and whitespace-insensitive, plus one deliberate concession to a naming split
    this repo documents and lives with: the telemetry ``service_name`` label carries a
    deployment prefix (``ecommerce-payment``) while alert payloads, truth files and CMDB
    rows use the bare name (``payment``) — see the call-graph note in
    ``aiops/tools/mock_providers.py``. Without the suffix rule, every Prometheus- and
    Loki-derived observation in the demo would go unowned.

    The rule is intentionally narrow (a full segment, on a ``-`` boundary) rather than a
    substring match, because a loose match here produces exactly the false attribution
    this stage is trying to avoid.
    """
    left = observed.strip().casefold()
    right = incident_service.strip().casefold()
    if not left or not right:
        return False
    return left == right or left.endswith(f"-{right}") or right.endswith(f"-{left}")
