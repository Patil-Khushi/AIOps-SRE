"""Read-only Q&A over one frozen RCA `Investigation`.

Not a second reasoning path. The chat can explain, cite, and quantify what the
platform already computed; it cannot move a number, execute anything, pull new
memory, or trigger a fresh investigation. Those are structural properties of
the types below (``ChatAnswer`` has no field to put a new verdict in), not just
prompt instructions — see ``tests/test_rca_chat_boundary.py``.

Two answer paths, exactly like ``agent.analyze``'s LLM/fallback split:

* ``answer()`` calls the model, grounded on a rendered snapshot of the
  ``Investigation`` (the "grounding pack"), then validates the reply against
  that same snapshot — unknown evidence ids are dropped and counted
  (``fabricated_citations``), a stated confidence that disagrees with the
  platform's is left in the prose but flagged (``warnings``), never silently
  edited.
* ``_deterministic_answer()`` runs when the model is unavailable (``stub``,
  timeout, unparseable JSON) — keyword-intent routing over a small closed set,
  rendering the corresponding ``Investigation`` section directly. No model, no
  fabrication, real content — the chat analogue of ``agent._fallback_verdict``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.rca_agent import incident_rag
from agents.rca_agent.agent import (
    _extract_json_object,
    _rca_model,
    _rca_provider,
    _render_action_block,
    _render_investigation_block,
)
from agents.rca_agent.investigation.models import EvidenceMatrix, Investigation
from agents.rca_agent.investigation_context import (
    InvestigationContextProvider,
    _evidence_stance_index,
    all_section_keys,
)
from agents.rca_agent.models import RCAVerdict
from agents.rca_agent.prompts import (
    RCA_CHAT_GROUNDING_BLOCK,
    RCA_CHAT_PLANNER_USER_V1,
    RCA_CHAT_PLANNER_V1,
    RCA_CHAT_SYSTEM_PROMPT_V1,
    RCA_CHAT_USER_V1,
)
from aiops.llm import Message
from aiops.llm import complete as llm_complete

logger = logging.getLogger(__name__)

# Prior turns are dropped, not summarized, past this many pairs — a model
# summary of prior turns would be a second hidden call and a fresh
# fabrication surface inside the very context meant to keep it honest.
MAX_HISTORY_TURNS = int(os.environ.get("AIOPS_RCA_CHAT_HISTORY_TURNS", "8"))

# A stated confidence more than this far from the platform's number is flagged.
_CONFIDENCE_DIVERGENCE = 0.15

_UNAVAILABLE = "(section unavailable)"


class ChatTurn(BaseModel):
    """One prior turn, verbatim — no summarization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    text: str


class SuggestedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["reanalyze", "open_tab", "review_option"]
    reason: str = ""
    tab: str | None = None
    recovery_option_id: str | None = None


class CitationDetail(BaseModel):
    """A citation plus its evidence stance — derived by looking the id up in
    the frozen Investigation (agents.rca_agent.investigation_context
    ._evidence_stance_index), never stated by the model. The model only ever
    names an evidence id; this is what makes a citation self-describing on
    the wire without asking it to also get the stance right."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    stance: str


class HistoricalIncidentRef(BaseModel):
    """One similar PAST incident, from a real search this turn — never
    parsed from the model's JSON (there is no field for the model to fill
    in here; see ``answer()``, which attaches this server-side after the
    fact, the same trust pattern as ``verdict_snapshot`` in
    ``demo/ui/rca_chat_routes.py``). Deliberately its own field, with its
    own id shape (``incident_id`` from ``aiops.state.repository``), so it can
    never be confused with a current-incident ``evidence_id`` (the "EV-nn"
    namespace validated by ``citation_details`` above) — Phase 20 item M13."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    similarity: float
    recorded_fix: str | None = None


class ChatAnswer(BaseModel):
    """The chat's response to one turn. No confidence/root-cause field exists
    here by design — see the module docstring."""

    model_config = ConfigDict(extra="forbid")

    answer: str = ""
    answerable: bool = True
    citations: tuple[str, ...] = ()
    citation_details: tuple[CitationDetail, ...] = ()
    missing: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    referenced_hypotheses: tuple[str, ...] = ()
    suggested_actions: tuple[SuggestedAction, ...] = ()
    # HISTORICAL — NOT CURRENT EVIDENCE. Always server-attached from a real
    # search (agents.rca_agent.incident_rag), never asserted by the model.
    historical_incidents: tuple[HistoricalIncidentRef, ...] = ()
    source: Literal["model", "deterministic"] = "model"
    fabricated_citations: int = 0
    warnings: tuple[str, ...] = ()
    history_truncated: bool = False


class GroundingPack(BaseModel):
    """A rendered, frozen snapshot of one Investigation, built once per
    session and reused for every turn (never re-rendered mid-conversation —
    the frozen-verdict guarantee depends on grounding staying fixed)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    text: str
    investigation: Investigation | None
    valid_evidence_ids: frozenset[str]
    valid_hypothesis_ids: frozenset[str]
    # evidence_id -> stance ("supports"/"contradicts"/"checked_absent"/...),
    # for structured citations (ChatAnswer.citation_details). Additive field
    # — existing callers that only read text/valid_evidence_ids/
    # valid_hypothesis_ids are unaffected.
    evidence_stance: dict[str, str] = Field(default_factory=dict)


def _safe(render: Any, *args: Any) -> str:
    """Run one section renderer; a bad field never blanks the whole pack.

    The investigation package is mid-upgrade (uncommitted), so a field rename
    should shrink one section, not raise mid-request — this is what makes
    that degrade rather than crash. tests/test_rca_chat.py's
    ``test_every_section_renders_against_a_full_fixture`` catches a rename
    that actually happens.
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
        f"blast_radius={o.blast_radius})"
        for o in inv.recovery_options[:5]
    ]
    return "\nRecovery options:\n" + "\n".join(lines)


def _render_verification_section(inv: Investigation) -> str:
    plan = inv.verification
    if plan is None:
        return "\nVerification plan: none produced."
    return (
        "\nVerification plan:\n  checks: "
        + "; ".join(plan.checks)
        + "\n  success criteria: "
        + "; ".join(plan.success_criteria)
    )


def _render_historical_section(inv: Investigation) -> str:
    hi = inv.historical_influence
    return (
        f"\nHistorical influence (precedent only, never current evidence): {hi.level}, "
        f"{len(hi.priors_applied)} verified prior(s) applied"
        + (", changed the ranking" if hi.changed_ranking else "")
    )


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


def build_grounding_pack(
    verdict: RCAVerdict, investigation: Investigation | None, service: str
) -> GroundingPack:
    """Render once per session; every turn reuses this unchanged."""
    parts = [
        _render_investigation_block(investigation),
        _render_action_block(service),
    ]
    if investigation is not None:
        parts.extend(
            [
                _safe(_render_timeline_section, investigation),
                _safe(_render_blast_radius_section, investigation),
                _safe(_render_completeness_section, investigation),
                _safe(_render_recovery_section, investigation),
                _safe(_render_verification_section, investigation),
                _safe(_render_historical_section, investigation),
            ]
        )
    text = "\n".join(p for p in parts if p)
    return GroundingPack(
        text=text
        or f"No investigation is available for {service}; reasoning from the verdict alone.",
        investigation=investigation,
        valid_evidence_ids=_collect_evidence_ids(investigation),
        valid_hypothesis_ids=_collect_hypothesis_ids(investigation),
        evidence_stance=_evidence_stance_index(investigation),
    )


_CONFIDENCE_RE = re.compile(r"\b(\d{1,3})\s?%|\b0\.\d+\b")


def _stated_confidence(text: str) -> float | None:
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return None
    whole = m.group(0)
    if whole.endswith("%"):
        return int(m.group(1)) / 100
    try:
        return float(whole)
    except ValueError:
        return None


def _coerce_answer(raw: dict[str, Any]) -> ChatAnswer | None:
    try:
        actions = tuple(
            SuggestedAction(**a)
            for a in (raw.get("suggested_actions") or [])
            if isinstance(a, dict)
        )
        return ChatAnswer(
            answer=str(raw.get("answer") or ""),
            answerable=bool(raw.get("answerable", True)),
            citations=tuple(str(c) for c in (raw.get("citations") or [])),
            missing=tuple(str(m) for m in (raw.get("missing") or [])),
            caveats=tuple(str(c) for c in (raw.get("caveats") or [])),
            referenced_hypotheses=tuple(str(h) for h in (raw.get("referenced_hypotheses") or [])),
            suggested_actions=actions,
        )
    except Exception:
        return None


def _validate(
    parsed: ChatAnswer, pack: GroundingPack, verdict: RCAVerdict, *, history_truncated: bool
) -> ChatAnswer:
    """Post-hoc guards, mirroring agent.py's ``_authoritative_confidence`` /
    ``_grounded_in_investigation`` for the chat surface."""
    valid_citations = tuple(c for c in parsed.citations if c in pack.valid_evidence_ids)
    fabricated = len(parsed.citations) - len(valid_citations)
    valid_hypotheses = tuple(
        h for h in parsed.referenced_hypotheses if h in pack.valid_hypothesis_ids
    )
    # The model names an evidence id; the stance is looked up, never stated by
    # it — so a citation can't misreport its own category.
    citation_details = tuple(
        CitationDetail(evidence_id=c, stance=pack.evidence_stance.get(c, "unknown"))
        for c in valid_citations
    )

    warnings = list(parsed.warnings)
    stated = _stated_confidence(parsed.answer)
    if stated is not None and abs(stated - verdict.confidence_score) > _CONFIDENCE_DIVERGENCE:
        warnings.append(
            "stated a confidence the platform did not compute; the platform value is "
            f"{verdict.confidence_score:.2f}"
        )

    return parsed.model_copy(
        update={
            "citations": valid_citations,
            "citation_details": citation_details,
            "referenced_hypotheses": valid_hypotheses,
            "fabricated_citations": fabricated,
            "warnings": tuple(warnings),
            "history_truncated": history_truncated,
            "source": "model",
        }
    )


# ─── section planning — "what does this question need?" ────────────────────
#
# Not a second reasoning engine and not a tool-calling loop: the model never
# gets a callable, only a menu of section KEYS (agents.rca_agent
# .investigation_context.SECTIONS) and is asked to pick from that closed
# list. Whatever comes back is filtered against the same allowlist again
# before anything is rendered from it (InvestigationContextProvider
# .render_sections drops unknown keys silently) — so a malformed or
# adversarial planner response can only ever narrow or widen the *rendered*
# sections, never smuggle in an arbitrary one.

# question-keyword -> section keys, reusing the SAME intent matcher the
# deterministic answerer uses (_match_intent below), so keyword coverage
# only has to be maintained in one place. `None` (unmatched) falls through
# to every section — the safe default when we can't tell what's needed.
_INTENT_SECTIONS: dict[str, tuple[str, ...]] = {
    # Severity lives in the always-on header (InvestigationContextProvider's
    # scope line), not in any selectable section — no extra section needed.
    "severity": (),
    "cause": ("evidence_detail",),
    "ruled_out": ("evidence_detail",),
    "gaps": ("evidence_detail",),
    "blast_radius": ("blast_radius",),
    "changes": ("changes", "timeline"),
    "verification": ("verification",),
    "confidence": ("evidence_detail",),
    "history": ("history",),
    "remediation": ("recovery", "evidence_detail"),
    "resolution_status": ("verification",),
    "similar_incidents": ("similar_incidents_rag",),
}


def _fallback_sections(question: str) -> tuple[str, ...]:
    """Keyword-based section selection, used when the planner LLM call is
    unavailable (stub, timeout, bad JSON) — the same PRIMARY/FALLBACK split
    as the answer itself. Unmatched -> everything, not nothing: an
    unrecognized question is exactly the case where under-retrieving would
    silently produce a worse answer."""
    intent = _match_intent(question)
    if intent is None:
        return all_section_keys()
    return _INTENT_SECTIONS.get(intent, all_section_keys())


def _plan_sections(
    question: str, history: list[ChatTurn], ctx_provider: InvestigationContextProvider
) -> tuple[str, ...]:
    """Which allowlisted sections does this question need? LLM-driven when
    available; falls back to the keyword mapping above. Never raises —
    a failure here just means the fuller keyword-based set gets rendered."""
    menu = ctx_provider.list_sections()
    if not menu:
        return ()
    menu_text = "\n".join(f"- {s.key}: {s.description}" for s in menu)
    history_text = "\n".join(f"{t.role}: {t.text}" for t in history[-6:]) or "(none yet)"
    rca_provider, model = _rca_provider(), _rca_model()
    try:
        resp = llm_complete(
            messages=[
                Message(role="system", content=RCA_CHAT_PLANNER_V1),
                Message(
                    role="user",
                    content=RCA_CHAT_PLANNER_USER_V1.format(
                        menu=menu_text, history=history_text, question=question
                    ),
                ),
            ],
            provider=rca_provider,
            model=model,
            temperature=0.0,
            max_tokens=200,
        )
        text = (resp.text or "").strip()
        if not text or text.startswith("[stub]"):
            return _fallback_sections(question)
        raw = _extract_json_object(text)
        if raw is None:
            return _fallback_sections(question)
        valid_keys = {s.key for s in menu}
        picked = tuple(
            key for key in (raw.get("sections") or []) if isinstance(key, str) and key in valid_keys
        )
        return picked or _fallback_sections(question)
    except Exception as exc:
        logger.debug("RCA chat section planning failed (%s); using keyword fallback", exc)
        return _fallback_sections(question)


# ─── historical incident RAG — advisory, read-only, never authoritative ────
#
# CURRENT INCIDENT EVIDENCE > HISTORICAL RAG, always. This section only ever
# ADDS a labeled block of real search results to the context text and
# server-attaches the same results to the returned ChatAnswer; it never
# touches confidence_score, root_cause_status, or anything scoring-related,
# and agents.rca_agent.incident_rag is a hard AST-checked wall away from
# investigation/memory.py (tests/test_rca_chat_boundary.py).

_HISTORICAL_BANNER = "HISTORICAL — NOT CURRENT EVIDENCE (advisory only; current investigation evidence is authoritative)"


def _wants_similar_incidents(question: str, selected_keys: tuple[str, ...]) -> bool:
    return (
        "similar_incidents_rag" in selected_keys or _match_intent(question) == "similar_incidents"
    )


def _current_category(verdict: RCAVerdict) -> str | None:
    if verdict.investigation is None:
        return None
    selected = verdict.investigation.selected
    return selected.hypothesis.category if selected else None


def _search_similar_incidents_for_verdict(
    verdict: RCAVerdict, *, incident_id: str | None
) -> list[incident_rag.SimilarIncident]:
    try:
        return incident_rag.search_similar_incidents(
            service=verdict.affected_service,
            summary=verdict.root_cause,
            category=_current_category(verdict),
            exclude_incident_id=incident_id,
        )
    except Exception as exc:  # pragma: no cover - defensive, mirrors _safe()
        logger.warning("RCA chat: historical incident search failed (%s)", exc)
        return []


def _render_similar_incidents_block(matches: list[incident_rag.SimilarIncident]) -> str:
    if not matches:
        return (
            f"\n{_HISTORICAL_BANNER}\n"
            "No sufficiently similar resolved incident was found in the available history."
        )
    lines = [f"\n{_HISTORICAL_BANNER}"]
    for m in matches:
        fix = f" Recorded fix: {m.recorded_fix}." if m.recorded_fix else " No fix was recorded."
        lines.append(
            f"  - {m.incident_id} (similarity {m.similarity:.2f}, {m.affected_service}"
            f"{', ' + m.category if m.category else ''}): {m.root_cause_summary}{fix}"
        )
    return "\n".join(lines)


def _to_historical_refs(
    matches: list[incident_rag.SimilarIncident],
) -> tuple[HistoricalIncidentRef, ...]:
    return tuple(
        HistoricalIncidentRef(
            incident_id=m.incident_id, similarity=m.similarity, recorded_fix=m.recorded_fix
        )
        for m in matches
    )


def _ask_llm(
    pack: GroundingPack,
    history: list[ChatTurn],
    question: str,
    verdict: RCAVerdict,
    context_text: str,
    *,
    history_truncated: bool,
) -> ChatAnswer | None:
    """One grounded answering call over ``context_text`` (either the
    selectively-retrieved pack or the full one). Returns ``None`` when the
    LLM path is unusable (stub/timeout/unparseable) so the caller falls back
    — mirrors ``agent.analyze()``'s own LLM/fallback split. Never raises."""
    rca_provider, model = _rca_provider(), _rca_model()
    messages = [
        Message(role="system", content=RCA_CHAT_SYSTEM_PROMPT_V1),
        Message(role="user", content=RCA_CHAT_GROUNDING_BLOCK.format(pack=context_text)),
    ]
    for turn in history:
        messages.append(Message(role=turn.role, content=turn.text))
    messages.append(Message(role="user", content=RCA_CHAT_USER_V1.format(question=question)))

    try:
        resp = llm_complete(
            messages=messages, provider=rca_provider, model=model, temperature=0.2, max_tokens=800
        )
        text = (resp.text or "").strip()
        if not text or text.startswith("[stub]"):
            return None
        raw = _extract_json_object(text)
        if raw is None:
            return None
        parsed = _coerce_answer(raw)
        if parsed is None:
            return None
    except Exception as exc:
        logger.warning("RCA chat LLM call failed (%s)", exc)
        return None

    return _validate(parsed, pack, verdict, history_truncated=history_truncated)


def answer(
    pack: GroundingPack,
    history: list[ChatTurn],
    question: str,
    verdict: RCAVerdict,
    *,
    incident_id: str | None = None,
) -> ChatAnswer:
    """One turn.

    Understand -> retrieve -> reason -> explain, strictly downstream of the
    frozen ``pack``/``verdict`` — this function never calls ``analyze()`` and
    never receives anything that could (see ``tests/test_rca_chat_boundary
    .py``). The primary path selects which investigation sections this
    specific question needs (``_plan_sections``) and answers grounded on
    just those (cheaper, more focused than always dumping the whole
    investigation); if that answer comes back ``answerable=False``, one
    retry is made against the FULL pack before accepting the abstention —
    selective retrieval must never be the reason a real answer was missed.
    Falls back to the deterministic keyword-routed answerer when the LLM
    path is unavailable at any stage, exactly as before.

    ``incident_id`` (additive, keyword-only — omitting it changes nothing
    else) excludes the current incident from its own "similar incidents"
    search; it is otherwise unused. Whenever the question calls for
    historical incidents, a real search runs (``agents.rca_agent
    .incident_rag``) and its results are ALWAYS attached to the returned
    ``ChatAnswer.historical_incidents`` server-side — never parsed from the
    model's own JSON, so the model cannot invent one. CURRENT INCIDENT
    EVIDENCE always outranks this: the search never touches
    ``confidence_score``/``root_cause_status``, only adds an explicitly
    labeled, advisory block of real past-incident facts.
    """
    kept = history[-(MAX_HISTORY_TURNS * 2) :] if MAX_HISTORY_TURNS else history
    history_truncated = len(kept) < len(history)

    ctx_provider = InvestigationContextProvider(
        verdict, pack.investigation, verdict.affected_service
    )
    selected_keys = _plan_sections(question, kept, ctx_provider)
    selective_text = ctx_provider.render_sections(selected_keys)

    similar: list[incident_rag.SimilarIncident] = []
    if _wants_similar_incidents(question, selected_keys):
        similar = _search_similar_incidents_for_verdict(verdict, incident_id=incident_id)
        selective_text += _render_similar_incidents_block(similar)

    result = _ask_llm(
        pack, kept, question, verdict, selective_text, history_truncated=history_truncated
    )

    if result is not None and not result.answerable and pack.investigation is not None:
        full_text = ctx_provider.render_all()
        if not similar:
            # The retry tries EVERYTHING before accepting an abstention —
            # including a historical search that the primary pass, having
            # judged it irrelevant, skipped.
            similar = _search_similar_incidents_for_verdict(verdict, incident_id=incident_id)
        full_text += _render_similar_incidents_block(similar)
        if full_text != selective_text:
            retried = _ask_llm(
                pack, kept, question, verdict, full_text, history_truncated=history_truncated
            )
            if retried is not None:
                result = retried

    if result is None:
        result = _deterministic_answer(pack, verdict, question, history_truncated=history_truncated)
        if not similar and _match_intent(question) == "similar_incidents":
            similar = _search_similar_incidents_for_verdict(verdict, incident_id=incident_id)

    if similar:
        result = result.model_copy(update={"historical_incidents": _to_historical_refs(similar)})
    return result


# ─── deterministic answerer — stub / unavailable-LLM path ──────────────────

_INTENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # More specific phrasings are checked before the generic "why"/"cause"
    # intent, since e.g. "why was X ruled out" would otherwise match "why".
    (
        ("ruled out", "rule out", "why not", "why isn't", "eliminated", "other candidate"),
        "ruled_out",
    ),
    (
        (
            "gap",
            "couldn't check",
            "could not check",
            "couldn't you check",
            "didn't check",
            "missing",
            "blind spot",
            "not checked",
        ),
        "gaps",
    ),
    (("severity", "how severe", "sev-", "sev "), "severity"),
    (("blast radius", "who else", "affected", "impact", "downstream"), "blast_radius"),
    (("changed", "change", "deploy", "commit", "rollout"), "changes"),
    (("verify", "verification", "confirm", "re-check", "recheck"), "verification"),
    (
        ("confiden", "how sure", "certain", "how likely", "how uncertain", "why uncertain"),
        "confidence",
    ),
    (
        (
            "should i do",
            "recommended fix",
            "safest fix",
            "what happens if i approve",
            "what happens if you approve",
            "how do i fix",
            "how to fix",
            "remediat",
            "recommend a fix",
            "what's the fix",
            "what is the fix",
        ),
        "remediation",
    ),
    (
        (
            "is it resolved",
            "is the incident resolved",
            "is it fixed",
            "still happening",
            "still broken",
            "resolved yet",
        ),
        "resolution_status",
    ),
    (("why", "cause", "explain", "what happened", "evidence", "support"), "cause"),
    # "similar incident"/"before"/"seen this" describe a search over OTHER past
    # incidents (agents.rca_agent.incident_rag) — a different question from
    # "precedent"/"histor" below, which is about whether RCA's OWN
    # verified-memory recall influenced THIS ranking (historical_influence,
    # already inside the frozen Investigation).
    (
        (
            "similar incident",
            "similar incidents",
            "happened before",
            "seen this before",
            "seen this dependency",
            "seen this failure",
            "what fixed",
            "what was different",
            "how is this different",
            "different from the previous",
            "different from last",
            "previous incident",
        ),
        "similar_incidents",
    ),
    (("histor", "precedent"), "history"),
)


def _match_intent(question: str) -> str | None:
    q = question.lower()
    for keywords, intent in _INTENTS:
        if any(k in q for k in keywords):
            return intent
    return None


def _abstain(history_truncated: bool, missing: str) -> ChatAnswer:
    """An honest "I don't have that" — never a bare empty string.

    ``missing`` stays the precise, debuggable reason (still carried for
    citations/logs and for a human who opens the raw payload), but the
    user-facing ``answer`` is the one sentence a real SRE would actually say
    out loud, not a rendering of that internal reason. The UI shows this
    text directly; it must never need a special "not answerable" banner to
    make sense on its own.
    """
    return ChatAnswer(
        answer="I don't have that information available for this incident.",
        answerable=False,
        missing=(missing,),
        source="deterministic",
        history_truncated=history_truncated,
    )


def _deterministic_answer(
    pack: GroundingPack, verdict: RCAVerdict, question: str, *, history_truncated: bool
) -> ChatAnswer:
    inv = pack.investigation
    if inv is None:
        return _abstain(
            history_truncated,
            "no investigation is attached to this verdict; nothing to answer from",
        )

    intent = _match_intent(question)
    if intent is None:
        return _abstain(
            history_truncated,
            "no model is configured; this deployment can only answer a closed set of "
            "questions from the investigation record (cause, ruled-out candidates, "
            "gaps, blast radius, changes, verification, confidence, remediation, "
            "resolution status, history, similar past incidents)",
        )

    if intent == "severity":
        return ChatAnswer(
            answer=f"This is a {inv.scope.severity} incident on {inv.scope.affected_service}.",
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "cause":
        selected = inv.selected
        if selected is None:
            return _abstain(history_truncated, "no hypothesis was selected")
        return ChatAnswer(
            answer=selected.hypothesis.mechanism
            + " "
            + " ".join(s.statement for s in selected.supporting[:3]),
            citations=tuple(s.evidence_id for s in selected.supporting[:3]),
            referenced_hypotheses=(selected.hypothesis.hypothesis_id,),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "ruled_out":
        rejected: tuple[EvidenceMatrix, ...] = inv.rejected
        if not rejected:
            return ChatAnswer(
                answer="No other candidates were scored for this incident.",
                source="deterministic",
                history_truncated=history_truncated,
            )
        lines = [
            f"{m.hypothesis.category}: "
            + ("; ".join(c.statement for c in m.contradicting[:2]) or "scored lower")
            for m in rejected[:3]
        ]
        return ChatAnswer(
            answer=" | ".join(lines),
            referenced_hypotheses=tuple(m.hypothesis.hypothesis_id for m in rejected[:3]),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "gaps":
        selected = inv.selected
        gaps = selected.gaps if selected else ()
        if not gaps:
            return ChatAnswer(
                answer="No gaps were recorded for the selected hypothesis.",
                source="deterministic",
                history_truncated=history_truncated,
            )
        return ChatAnswer(
            answer="Could not check: " + "; ".join(g.statement for g in gaps[:5]),
            citations=tuple(g.evidence_id for g in gaps[:5]),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "blast_radius":
        br = inv.blast_radius
        if br is None:
            return _abstain(history_truncated, "blast radius was not examined for this incident")
        if not br.impacts:
            return ChatAnswer(
                answer="Blast radius was examined; no services were placed in it.",
                source="deterministic",
                history_truncated=history_truncated,
            )
        lines = [f"{i.service}: {i.state.value}" for i in br.impacts]
        return ChatAnswer(
            answer="; ".join(lines), source="deterministic", history_truncated=history_truncated
        )

    if intent == "changes":
        events = tuple(e for e in inv.timeline.events if e.is_change)
        if not events:
            return ChatAnswer(
                answer="No changes (deploys, config, infrastructure) were found in the timeline.",
                source="deterministic",
                history_truncated=history_truncated,
            )
        lines = [f"{e.service}: {e.event} ({e.temporal_relation.value})" for e in events[:5]]
        return ChatAnswer(
            answer="; ".join(lines), source="deterministic", history_truncated=history_truncated
        )

    if intent == "verification":
        plan = inv.verification
        if plan is None:
            return _abstain(history_truncated, "no verification plan was produced")
        return ChatAnswer(
            answer="Checks: "
            + "; ".join(plan.checks)
            + ". Success criteria: "
            + "; ".join(plan.success_criteria),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "confidence":
        status = verdict.root_cause_status.value
        base = f"Status: {status}. Platform confidence: {verdict.confidence_score:.2f}."
        if status == "uncertain":
            tied = inv.matrices[:2] if not inv.discriminated else ()
            answer = (
                base
                + " No single root cause was confirmed — the evidence does not discriminate between the top candidates."
            )
            if tied:
                answer += (
                    " Competing hypotheses: " + ", ".join(m.hypothesis.category for m in tied) + "."
                )
            return ChatAnswer(
                answer=answer,
                referenced_hypotheses=tuple(m.hypothesis.hypothesis_id for m in tied),
                source="deterministic",
                history_truncated=history_truncated,
            )
        if status == "insufficient_evidence":
            return ChatAnswer(
                answer=base + " There is not enough evidence yet to name a cause.",
                source="deterministic",
                history_truncated=history_truncated,
            )
        return ChatAnswer(
            answer=base + f" Discriminated from the runner-up: {inv.discriminated}.",
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "remediation":
        if verdict.root_cause_status.value not in ("confirmed", "probable"):
            return ChatAnswer(
                answer=(
                    "No remediation is offered while the root cause is uncertain or evidence is "
                    "insufficient — a confirmed or probable cause is needed before a fix is proposed."
                ),
                source="deterministic",
                history_truncated=history_truncated,
            )
        options = inv.recovery_options
        if not options:
            return _abstain(
                history_truncated, "no recovery options were proposed for this investigation"
            )
        lines = [
            f"{o.option_id}: {o.description} (risk={o.risk.level}, blast_radius={o.blast_radius}, "
            f"executable={o.executable})"
            for o in options[:3]
        ]
        return ChatAnswer(
            answer=(
                "Recommended option(s) — every one requires human approval before anything runs: "
                + " | ".join(lines)
            ),
            suggested_actions=(
                SuggestedAction(kind="review_option", recovery_option_id=options[0].option_id),
            ),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "resolution_status":
        return _abstain(
            history_truncated,
            "live resolution status is not available in this chat context — this investigation is a "
            "frozen snapshot; check the incident's verification result or ticket for current status",
        )

    if intent == "history":
        hi = inv.historical_influence
        return ChatAnswer(
            answer=(
                f"Historical influence: {hi.level} — {len(hi.priors_applied)} verified prior(s) applied"
                + (", which changed the ranking." if hi.changed_ranking else ".")
            ),
            source="deterministic",
            history_truncated=history_truncated,
        )

    if intent == "similar_incidents":
        # Self-contained: does its own search (a second, cheap search is
        # harmless) rather than depending on answer()'s outer-scope result,
        # so this branch produces coherent prose even when called directly.
        matches = _search_similar_incidents_for_verdict(verdict, incident_id=None)
        if not matches:
            return ChatAnswer(
                answer="No sufficiently similar resolved incident was found in the available history.",
                source="deterministic",
                historical_incidents=(),
                history_truncated=history_truncated,
            )
        lines = []
        for m in matches:
            fix = f" Recorded fix: {m.recorded_fix}." if m.recorded_fix else " No fix was recorded."
            lines.append(
                f"{m.incident_id} (similarity {m.similarity:.2f}): {m.root_cause_summary}.{fix}"
            )
        return ChatAnswer(
            answer=(
                "Historical — not current evidence — similar resolved incident(s): "
                + " | ".join(lines)
            ),
            historical_incidents=_to_historical_refs(matches),
            source="deterministic",
            history_truncated=history_truncated,
        )

    return _abstain(history_truncated, "question did not match a known intent")
