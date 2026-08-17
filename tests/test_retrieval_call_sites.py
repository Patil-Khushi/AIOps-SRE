"""Ratchet: the duplicated-retrieval ledger cannot grow while the migration runs.

The Context Engineering Layer exists to collapse retrieval that four agents
currently do independently. That migration is phased, so for several PRs the old
call sites and the new layer coexist — and during exactly that window it is easy
for a new direct ``get_registry().call("observability.metrics.query", ...)`` to be
added in good faith, because the surrounding code is full of them.

This test freezes today's ledger as data. It does not forbid the existing call
sites (they are the fallback path until parity is proven per agent); it fails when
the count in a file *changes*, which turns "route this through aiops/context/
instead" into CI feedback rather than a review comment someone has to remember to
make.

Counts, not line numbers, deliberately. A line-number ledger churns on every
unrelated edit above it, which trains people to update the fixture without reading
it — and a ratchet nobody reads is a ratchet that ratchets nothing.

**When a migration phase legitimately removes a call site, lower the count here in
the same commit.** A count going *down* fails too, on purpose: it is the check that
each phase actually deleted the duplication it claimed to.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# capability -> {file relative to repo root: number of textual references}
#
# Captured from the pre-migration tree. Every entry is a *retrieval* capability —
# a read that the context layer can serve once and share. Mutation capabilities
# (automation.*, itsm.incident.create, notify.send, chatops.*) are deliberately
# absent: they are not evidence, and aiops/context/denylist.py refuses to wrap them.
RETRIEVAL_LEDGER: dict[str, dict[str, int]] = {
    "observability.metrics.query": {
        # 2 references: the by_capability() pre-flight probe at :654 plus the call
        # at :663. The probe is load-bearing — it lets triage emit one
        # "capability not registered" trace line instead of one per query — so the
        # adapter has to preserve that fast path, not just the call.
        "agents/alert_triage/agent.py": 2,
        "agents/log_correlation/agent.py": 1,
        # 2 references, only one a genuine call site — the other is Phase 5's
        # shadow-mode legacy reconstruction. See the matching comment on this
        # file's entry under "observability.traces.search" below.
        "agents/notification_assembler/agent.py": 2,
        # New in Phase 5, same reasoning as the rca_agent/context_adapter.py
        # entry below: context-layer usage (a SectionSpec param plus the
        # "unavailable" fallback item's `source=` string), not a bypass.
        "agents/notification_assembler/context_adapter.py": 2,
        "agents/rca_agent/evidence.py": 1,
        # No context_adapter.py entry here: unlike the alerts capability below,
        # RCA's context request never names "observability.metrics.query"
        # literally — it is the metrics collector's *default* capability, so a
        # plain `SectionSpec(source="metrics", ...)` with no override reaches it.
        # NOT migrated: verifier's PromQL arrives on VerifyContext from its caller,
        # so there is no shared query to collapse.
        "agents/resolution_verifier/verifier.py": 1,
    },
    "observability.metrics.alerts": {
        # 2 references, only one of which is a call site: the live registry call at
        # `_live_alerts` (unchanged since before the migration) plus the new
        # `ALERTS_QUERY_ID = "observability.metrics.alerts"` constant added in the
        # RCA migration (Phase 4). That constant is a shared *label*, not a second
        # call — it lets `context_adapter.py`'s ContextBackend look up the same
        # capability's context-collected payload by the identical string
        # `build_context_request_specs` used to request it, so the two cannot name
        # it differently and drift apart.
        "agents/rca_agent/evidence.py": 2,
        # New in Phase 4. This is context-LAYER usage, not a bypass: it names the
        # capability on a `SectionSpec.capability` override, which the collector
        # then routes through the registry with the normal guard/cache/status
        # mapping — i.e. exactly the "route it through aiops/context/ instead"
        # outcome this ratchet asks for when it fires. Recorded here (rather than
        # exempting context_adapter.py files generally) so a FUTURE direct
        # `get_registry().call(...)` accidentally added to an adapter still trips
        # the ratchet if it changes this count.
        "agents/rca_agent/context_adapter.py": 1,
    },
    "observability.logs.query": {
        "agents/log_correlation/agent.py": 1,
        "agents/rca_agent/evidence.py": 1,
    },
    "observability.traces.search": {
        "agents/alert_triage/agent.py": 1,
        "agents/log_correlation/agent.py": 1,
        # New: trace evidence for RCA (error status + latency, a third source
        # alongside metrics/logs). Deliberately outside the Context Engineering
        # Layer, same documented precedent as this file's own `recent_changes`
        # (evidence.py's module docstring) — a live-only category, not a bypass
        # of shared retrieval that the context layer would otherwise dedupe.
        "agents/rca_agent/evidence.py": 1,
        # 2 references, only one a genuine call site. The Phase 5 migration added
        # a second: shadow mode (AIOPS_CONTEXT_LAYER=shadow) must compare against
        # the *real* legacy answer, so `_telemetry_items` reconstructs the exact
        # original `_context_item(...)` calls verbatim for that comparison only —
        # never returned to the caller. See the "observability.metrics.query"
        # entry below and context_adapter.py's for the matching pattern.
        "agents/notification_assembler/agent.py": 2,
        # New in Phase 5 — context-layer usage (SectionSpec.params + the source
        # string on the "unavailable" fallback item), not a bypass. Same
        # reasoning as rca_agent/context_adapter.py's entry above.
        "agents/notification_assembler/context_adapter.py": 2,
    },
    # The clearest duplication in the repo: four lookups per incident for an answer
    # that cannot change within one incident.
    "oncall.schedule.lookup": {
        "agents/alert_triage/agent.py": 1,
        # Deliberate re-query, kept: the classifier is sold standalone and does not
        # trust an upstream value. It routes through the context cache rather than
        # the shared context, so the round-trip is saved without collapsing the
        # independence. See the plan's §A-note.
        "agents/alert_triage/classifier.py": 1,
        # 2 references: the primary lookup at :143 and the per-dependency loop at
        # :451. The loop stays — each iteration is a different team and service.
        "agents/notification_assembler/agent.py": 2,
    },
    "itsm.cmdb.lookup": {
        "agents/alert_triage/agent.py": 1,
        "agents/alert_triage/classifier.py": 1,
        "agents/notification_assembler/agent.py": 1,
    },
    "itsm.cmdb.dependencies": {
        "agents/alert_triage/classifier.py": 1,
        "agents/notification_assembler/agent.py": 1,
    },
    "scm.commit.history": {
        # Two genuinely different queries inside one agent, both rendered into the
        # same prompt: :499 sends since=48h/limit=10 against a curated path map,
        # evidence.py:271 sends limit=5 with no since against demo/ecommerce/<svc>.
        # They must stay two specs — merging them changes the prompt.
        "agents/rca_agent/agent.py": 1,
        "agents/rca_agent/evidence.py": 1,
    },
    "incident.resolvers.lookup": {
        "agents/notification_assembler/agent.py": 1,
    },
}


def _count_references(capability: str) -> dict[str, int]:
    """Count double-quoted references to ``capability`` in every agent module.

    Matches the quoted literal so a capability named in prose or a docstring is not
    counted — the strings this ledger tracks always appear quoted at a call site.
    ``evals/`` fixtures are skipped: golden JSON is test data, not a call site.
    """
    needle = re.compile(re.escape(f'"{capability}"'))
    found: dict[str, int] = {}
    for path in (REPO_ROOT / "agents").rglob("*.py"):
        parts = path.relative_to(REPO_ROOT).parts
        if "__pycache__" in parts or "evals" in parts:
            continue
        hits = len(needle.findall(path.read_text(encoding="utf-8")))
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_retrieval_ledger_matches_the_tree():
    """Every tracked capability's call sites match the recorded ledger."""
    drift: list[str] = []
    for capability, expected in RETRIEVAL_LEDGER.items():
        actual = _count_references(capability)
        if actual == expected:
            continue
        for path in sorted(set(expected) | set(actual)):
            was, now = expected.get(path, 0), actual.get(path, 0)
            if was != now:
                drift.append(f"  {capability}  {path}: ledger says {was}, tree has {now}")

    assert not drift, (
        "Direct retrieval call sites drifted from the ledger.\n\n"
        "If you ADDED one: route it through aiops/context/ instead of calling the\n"
        "capability directly — sharing retrieval is the point of that package.\n\n"
        "If a migration phase REMOVED one: lower the count in RETRIEVAL_LEDGER in\n"
        "this same commit. That edit is the proof the phase deleted what it claimed.\n\n"
        + "\n".join(drift)
    )


def test_ledger_has_no_stale_files():
    """Every file named in the ledger still exists.

    A renamed or deleted module would otherwise leave an entry that can never fail,
    quietly shrinking what the ratchet covers.
    """
    missing = [
        f"{capability} -> {path}"
        for capability, files in RETRIEVAL_LEDGER.items()
        for path in files
        if not (REPO_ROOT / path).exists()
    ]
    assert not missing, "RETRIEVAL_LEDGER names files that no longer exist:\n" + "\n".join(missing)
