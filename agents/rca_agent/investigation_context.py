"""Read-only accessors over one frozen ``(RCAVerdict, Investigation)`` pair.

This is the "tool layer" the chat's conversational retrieval is built on:
``InvestigationContextProvider`` exposes named, allowlisted sections of the
Investigation, so the chat's understanding step can pick a handful of
relevant sections instead of always rendering the whole thing — but the
model never gets a Python function to call. It gets a menu of section
*names*, chooses from that closed menu, and this module does the rendering.

Boundary (checked by AST in ``tests/test_rca_chat_boundary.py``, the same
discipline as ``chat.py`` itself): this module never calls ``analyze()``,
the tool registry, the policy gate, the incident-history/memory subsystem,
or anything that mutates state. Every accessor either renders real data
already sitting in the frozen objects passed to the constructor, or returns
an honest "not available" string — never a guess.

Deliberately independent of ``chat.py``'s own ``_render_*_section``
functions rather than importing them: those are patched by name in existing
tests (``monkeypatch.setattr(chat, "_render_blast_radius_section", ...)``),
which only affects lookups in ``chat.py``'s own module namespace. Reusing
them here would mean this module's renderers silently ignore that patch (a
lookup via this module's own globals), which is a worse trap than the small
duplication. ``chat.py``'s ``build_grounding_pack``/``GroundingPack.text``
are untouched by this module — they keep using ``chat.py``'s own renderers,
exactly as before. This module is the renderer for the NEW selective,
per-question retrieval path only.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from agents.rca_agent.investigation.models import EvidenceMatrix, Investigation
from agents.rca_agent.models import RCAVerdict

logger = logging.getLogger(__name__)

_UNAVAILABLE = "(section unavailable)"


def _safe(render: Any, *args: Any) -> str:
    """Run one section renderer; a bad field never blanks the whole pack.

    The investigation package is mid-upgrade (uncommitted), so a field rename
    should shrink one section, not raise mid-request.
    """
    try:
        return render(*args)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "chat grounding section %s failed (%s)", getattr(render, "__name__", render), exc
        )
        return _UNAVAILABLE


def _render_timeline_section(inv: Investigation) -> str:
    tl = inv.timeline
    if not tl.events:
        return ""
    lines = [
        f"  - {e.timestamp.isoformat()} {e.service}: {e.event}"
        f" ({e.temporal_relation.value}){' [change]' if e.is_change else ''}"
        for e in tl.events[:15]
    ]
    note = (
        f"\n  sources unavailable: {', '.join(tl.sources_unavailable)}"
        if tl.sources_unavailable
        else ""
    )
    return "\nTimeline:\n" + "\n".join(lines) + note


def _render_changes_section(inv: Investigation) -> str:
    """Just the ``is_change`` events — a focused subset of the timeline for
    "what changed" questions. Every row is tagged as correlation, never
    presented as the cause — that judgment belongs to the scored hypothesis,
    not to a change existing near the onset."""
    changes = [e for e in inv.timeline.events if e.is_change]
    if not changes:
        return "\nChanges: none found in the timeline (temporal correlation only, never asserted as cause)."
    lines = [
        f"  - {e.timestamp.isoformat()} {e.service}: {e.event} "
        f"(temporal correlation only — {e.temporal_relation.value})"
        for e in changes[:10]
    ]
    return "\nChanges (temporal correlation, not established causation):\n" + "\n".join(lines)


def _render_blast_radius_section(inv: Investigation) -> str:
    br = inv.blast_radius
    if br is None:
        return "\nBlast radius: not examined for this incident."
    if not br.impacts:
        return "\nBlast radius: examined, no services placed in it."
    lines = [f"  - {i.service}: {i.state.value} ({i.rationale})" for i in br.impacts]
    return (
        "\nBlast radius (topology "
        + ("available" if br.topology_available else "unavailable")
        + "):\n"
        + "\n".join(lines)
    )


def _render_completeness_section(inv: Investigation) -> str:
    c = inv.completeness
    per_source = ", ".join(f"{k}={v}" for k, v in c.per_source.items())
    gaps = f"; critical gaps: {', '.join(c.critical_gaps)}" if c.critical_gaps else ""
    return f"\nInvestigation completeness: {c.overall:.0%} ({per_source}){gaps}"


def _render_recovery_section(inv: Investigation) -> str:
    if not inv.recovery_options:
        return "\nRecovery options: none proposed."
    lines = [
        f"  - {o.option_id}: {o.description} (grounded={o.grounded}, executable={o.executable}, "
        f"blast_radius={o.blast_radius}, risk={o.risk.level})"
        for o in inv.recovery_options[:5]
    ]
    return (
        "\nRecovery options (explain only — execution is HITL-gated, never from this chat):\n"
        + "\n".join(lines)
    )


def _render_verification_section(inv: Investigation) -> str:
    plan = inv.verification
    if plan is None:
        return "\nVerification plan: none produced."
    return (
        "\nVerification plan:\n  checks: "
        + "; ".join(plan.checks)
        + "\n  success criteria: "
        + "; ".join(plan.success_criteria)
        + "\n  live verification status: not available in this chat context "
        "(no per-incident verification-outcome endpoint exists yet — do not guess whether it passed)"
    )


def _render_historical_section(inv: Investigation) -> str:
    hi = inv.historical_influence
    return (
        f"\nHistorical influence (precedent only, never current evidence): {hi.level}, "
        f"{len(hi.priors_applied)} verified prior(s) applied"
        + (", changed the ranking" if hi.changed_ranking else "")
    )


def _render_evidence_detail_section(inv: Investigation) -> str:
    """The full (untruncated) evidence per hypothesis — deeper than the
    always-on header's top-4/3-supports-2-contradicts summary. Selected only
    when a question needs it, so the common case stays cheap."""
    if not inv.matrices:
        return ""
    lines: list[str] = ["\nFull evidence detail (all hypotheses, all evidence):"]
    for m in inv.matrices:
        lines.append(f"  {m.hypothesis.category} (id={m.hypothesis.hypothesis_id}):")
        for label, bucket in (
            ("supports", m.supporting),
            ("contradicts", m.contradicting),
            ("checked_absent", m.checked_absent),
            ("gap", m.gaps),
        ):
            for item in bucket:
                lines.append(f"    [{label}] {item.evidence_id}: {item.statement}")
    return "\n".join(lines)


def _collect_evidence_ids(inv: Investigation | None) -> frozenset[str]:
    if inv is None:
        return frozenset()
    ids: set[str] = set()
    for matrix in inv.matrices:
        for bucket in (matrix.supporting, matrix.contradicting, matrix.checked_absent, matrix.gaps):
            ids.update(item.evidence_id for item in bucket)
    return frozenset(ids)


def _collect_hypothesis_ids(inv: Investigation | None) -> frozenset[str]:
    if inv is None:
        return frozenset()
    return frozenset(m.hypothesis.hypothesis_id for m in inv.matrices)


def _evidence_stance_index(inv: Investigation | None) -> dict[str, str]:
    """evidence_id -> stance, for structured citations (Phase 14). Read-only,
    derived entirely from the frozen Investigation — the model never states
    a stance itself, this is looked up, not asked for."""
    if inv is None:
        return {}
    index: dict[str, str] = {}
    for matrix in inv.matrices:
        for bucket in (matrix.supporting, matrix.contradicting, matrix.checked_absent, matrix.gaps):
            for item in bucket:
                index[item.evidence_id] = item.stance.value
    return index


@dataclass(frozen=True)
class SectionInfo:
    """One menu entry — shown to the section-planning LLM call as
    ``key: description``, never as a callable."""

    key: str
    label: str
    description: str


# The closed, allowlisted set of selectable sections. "investigation_summary"
# is not listed here — it is always included (see render_sections) because it
# is small and almost always relevant (root cause, status, confidence, top
# hypotheses' evidence). Adding a new key means adding it BOTH here and to
# InvestigationContextProvider._RENDERERS, or it silently can never be picked.
_SECTIONS: tuple[SectionInfo, ...] = (
    SectionInfo(
        "evidence_detail",
        "Full evidence detail",
        "Every evidence item (supports/contradicts/checked_absent/gap) for every hypothesis, "
        "untruncated. Use for detailed 'what evidence' or 'what did you rule out' questions.",
    ),
    SectionInfo(
        "timeline",
        "Timeline",
        "The chronological sequence of observed events, with source and temporal relation to onset.",
    ),
    SectionInfo(
        "changes",
        "Changes",
        "Deploys/config/infra changes near the incident, each tagged as temporal correlation only.",
    ),
    SectionInfo(
        "blast_radius",
        "Blast radius",
        "Which services are directly affected, indirectly affected, observed healthy, not observed, or unknown.",
    ),
    SectionInfo(
        "completeness",
        "Investigation completeness",
        "What fraction of evidence sources answered, and which are critically missing.",
    ),
    SectionInfo(
        "recovery",
        "Recovery options",
        "Proposed fix options with their risk level, blast radius, and whether they are grounded/executable.",
    ),
    SectionInfo(
        "verification",
        "Verification plan",
        "The checks and success criteria that would confirm a fix worked, and their current status.",
    ),
    SectionInfo(
        "history",
        "Historical influence",
        "Whether similar past incidents (verified outcomes only) influenced the ranking.",
    ),
    # Menu-only: the LLM planner can pick this key, but it is NOT one of
    # InvestigationContextProvider's own renderers (see _RENDERERS below and
    # render_sections' guard) — it is rendered by chat.py from a live search
    # over agents/rca_agent/incident_rag.py, a DIFFERENT corpus (persisted RCA
    # verdicts, real past incidents) from "history" above (RCA's own
    # verified-outcome memory, baked into the frozen Investigation already).
    # Kept in this menu rather than a second one so the planner only ever
    # consults a single closed list.
    SectionInfo(
        "similar_incidents_rag",
        "Similar past incidents",
        "Semantic search over OTHER resolved incidents this deployment has processed — for "
        "'has this happened before', 'similar incident', 'what fixed it last time' questions. "
        "Historical precedent, not current evidence.",
    ),
)

_SECTION_KEYS = frozenset(s.key for s in _SECTIONS)


def all_section_keys() -> tuple[str, ...]:
    """Every selectable section key, in a fixed order — the safe default when
    a question's intent can't be classified (chat.py's keyword fallback uses
    this rather than guessing a narrower set)."""
    return tuple(s.key for s in _SECTIONS)


class InvestigationContextProvider:
    """Read-only view over one frozen ``(RCAVerdict, Investigation | None)``
    pair. Never calls ``analyze()``, the tool registry, the policy gate, or
    any memory-write path — every method either renders real data already
    present in the objects passed to the constructor, or says so isn't
    available.
    """

    def __init__(
        self, verdict: RCAVerdict, investigation: Investigation | None, service: str
    ) -> None:
        self._verdict = verdict
        self._inv = investigation
        self._service = service

    # ── the menu ─────────────────────────────────────────────────────────

    def list_sections(self) -> tuple[SectionInfo, ...]:
        return _SECTIONS

    # ── always-on facts (small, cheap, foundational — see get_incident_context) ──

    def get_incident_context(self) -> dict[str, Any]:
        v = self._verdict
        out: dict[str, Any] = {
            "affected_service": v.affected_service,
            "root_cause": v.root_cause,
            "root_cause_status": v.root_cause_status.value,
            "confidence_score": v.confidence_score,
        }
        # Severity lives on the investigation's scope (set at triage time),
        # not on the RCAVerdict itself — omitted here meant a trivially known
        # fact ("what severity is this?") looked unanswerable and produced a
        # spurious "a fresh investigation would be needed" suggestion.
        if self._inv is not None:
            out["severity"] = self._inv.scope.severity
        return out

    def get_investigation_summary(self) -> str | None:
        """None when no investigation ran — the caller must say so, not
        invent one."""
        if self._inv is None:
            return None
        return (
            f"status={self._inv.status.value} confidence={self._inv.confidence:.2f} "
            f"discriminated={self._inv.discriminated}"
        )

    def get_hypotheses(self) -> list[dict[str, Any]]:
        if self._inv is None:
            return []
        return [
            {
                "hypothesis_id": m.hypothesis.hypothesis_id,
                "category": m.hypothesis.category,
                "mechanism": m.hypothesis.mechanism,
                "score": m.score.score if m.score else None,
            }
            for m in self._inv.matrices
        ]

    def get_hypothesis_details(self, hypothesis_id: str) -> dict[str, Any] | None:
        """None when the id doesn't exist — never fabricated."""
        if self._inv is None:
            return None
        matrix: EvidenceMatrix | None = next(
            (m for m in self._inv.matrices if m.hypothesis.hypothesis_id == hypothesis_id), None
        )
        if matrix is None:
            return None
        return {
            "hypothesis_id": matrix.hypothesis.hypothesis_id,
            "category": matrix.hypothesis.category,
            "mechanism": matrix.hypothesis.mechanism,
            "score": matrix.score.score if matrix.score else None,
            "supporting": [i.statement for i in matrix.supporting],
            "contradicting": [i.statement for i in matrix.contradicting],
            "checked_absent": [i.statement for i in matrix.checked_absent],
            "gaps": [i.statement for i in matrix.gaps],
        }

    def get_evidence(
        self,
        hypothesis_id: str | None = None,
        bucket: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._inv is None:
            return []
        out: list[dict[str, Any]] = []
        for m in self._inv.matrices:
            if hypothesis_id and m.hypothesis.hypothesis_id != hypothesis_id:
                continue
            for stance_name, items in (
                ("supports", m.supporting),
                ("contradicts", m.contradicting),
                ("checked_absent", m.checked_absent),
                ("gap", m.gaps),
            ):
                if bucket and bucket != stance_name:
                    continue
                for item in items:
                    if source and item.source != source:
                        continue
                    out.append(
                        {
                            "evidence_id": item.evidence_id,
                            "hypothesis_id": m.hypothesis.hypothesis_id,
                            "bucket": stance_name,
                            "statement": item.statement,
                            "source": item.source,
                        }
                    )
        return out

    def get_blast_radius(self) -> str:
        if self._inv is None:
            return "not examined — no investigation ran"
        return _safe(_render_blast_radius_section, self._inv)

    def get_recovery_options(self) -> str:
        if self._inv is None:
            return "none — no investigation ran"
        return _safe(_render_recovery_section, self._inv)

    def get_verification_plan(self) -> str:
        if self._inv is None:
            return "none — no investigation ran"
        return _safe(_render_verification_section, self._inv)

    def get_verification_status(self) -> str:
        """Deliberately honest, per Phase 11/17: no per-incident live
        verification-outcome endpoint exists yet. Never guessed."""
        return "not available in this chat context"

    def get_historical_influence(self) -> str:
        if self._inv is None:
            return "none — no investigation ran"
        return _safe(_render_historical_section, self._inv)

    def get_changes(self) -> str:
        if self._inv is None:
            return "none — no investigation ran"
        return _safe(_render_changes_section, self._inv)

    def get_timeline(self) -> str:
        if self._inv is None:
            return "none — no investigation ran"
        return _safe(_render_timeline_section, self._inv)

    # ── rendering (used by chat.py's grounding pack / selective retrieval) ──

    _RENDERERS: ClassVar[dict[str, str]] = {
        "evidence_detail": "_render_evidence_detail_section",
        "timeline": "_render_timeline_section",
        "changes": "_render_changes_section",
        "blast_radius": "_render_blast_radius_section",
        "completeness": "_render_completeness_section",
        "recovery": "_render_recovery_section",
        "verification": "_render_verification_section",
        "history": "_render_historical_section",
    }

    def _render_scope_header(self) -> str:
        """Basic incident metadata (severity, alert name) — set once at
        triage time and never re-derived by the investigation, so it lives on
        ``Investigation.scope``, not on any hypothesis. Without this line,
        severity/alert-name questions had no path into the model's context at
        all and produced a spurious "a fresh investigation would be needed"
        answer for facts already in hand.
        """
        if self._inv is None:
            return ""
        scope = self._inv.scope
        alert = f", alert={scope.alert_name}" if scope.alert_name else ""
        return f"Incident: severity={scope.severity}{alert}, service={scope.affected_service}"

    def _header(self) -> list[str]:
        """The always-on part: basic incident metadata, platform action
        vocabulary, and the investigation block (top-ranked hypotheses with
        their evidence, already rendered by agent.py's own renderer, which
        this module deliberately reuses rather than duplicating)."""
        from agents.rca_agent.agent import _render_action_block, _render_investigation_block

        return [
            self._render_scope_header(),
            _render_investigation_block(self._inv),
            _render_action_block(self._service),
        ]

    def render_sections(self, keys: Sequence[str]) -> str:
        """Render the always-on header plus only the requested (validated)
        section keys. Unknown keys are silently ignored — this is the
        allowlist boundary: nothing outside ``_SECTION_KEYS`` can ever be
        rendered, no matter what a caller passes."""
        parts = list(self._header())
        if self._inv is not None:
            for key in keys:
                if key not in _SECTION_KEYS:
                    continue
                fn_name = self._RENDERERS.get(key)
                if fn_name is None:
                    # A menu-only key (e.g. "similar_incidents_rag") — real,
                    # but rendered by the caller from a different source, not
                    # by this provider. Silently skipped here, never an error.
                    continue
                fn = globals()[fn_name]
                parts.append(_safe(fn, self._inv))
        text = "\n".join(p for p in parts if p)
        return (
            text
            or f"No investigation is available for {self._service}; reasoning from the verdict alone."
        )

    def render_all(self) -> str:
        """Every section, in a fixed deterministic order — the full pack,
        byte-identical to what ``build_grounding_pack`` has always produced
        (the original six sections keep their original relative order; the
        two new ones — evidence_detail, changes — are additive)."""
        return self.render_sections(tuple(s.key for s in _SECTIONS))
