import type { ChatAnswer } from '@/types/rca';
import type { RCAVerdict } from '@/types/api';
import { isActionable } from '@/lib/rcaDerive';

// The {head, lead, facts[], bullets[], sources[], note} answer-card shape
// from the design spec. ChatAnswer itself doesn't carry these field names —
// this is the one place that maps the backend's real shape onto the card.
export interface AnswerCardVM {
  /** Omitted for ordinary Q&A — a plain chat answer needs no banner above it.
   *  Verdict cards keep a real, informative header (status + confidence). */
  head?: string;
  lead: string;
  facts: { label: string; value: string }[];
  bullets: string[];
  sources: string[];
  note?: string;
  /** 0-100. Historical-match similarity for a /similar answer, or the
   *  platform's own confidence for a verdict card. Omitted when neither
   *  applies (a plain Q&A answer has no single number to show). */
  confidencePct?: number;
  /** Short badge next to the confidence dot — e.g. "pattern match" for a
   *  historical-incident answer. Never implies causality on its own. */
  tag?: string;
  /** Raw text this card represents, for the Copy/Teams/Postmortem actions —
   *  kept separate from the rendered `lead`/`bullets` so those can stay
   *  concise while the shared text carries full context. */
  shareText: string;
}

export function toAnswerCard(answer: ChatAnswer): AnswerCardVM {
  // `answer.answer` is always a real sentence now — the backend never leaves
  // it empty (see agents/rca_agent/chat.py::_abstain) — so there is nothing
  // for the UI to paper over with a banner. Caveats/warnings still surface as
  // bullets below it, exactly where a human SRE would mention them.
  const lead = answer.answer;

  const bullets = [...answer.caveats, ...answer.warnings.map((w) => `Warning: ${w}`)];

  const facts: AnswerCardVM['facts'] = [];
  if (answer.fabricated_citations > 0) {
    facts.push({ label: 'Unverifiable citations dropped', value: String(answer.fabricated_citations) });
  }

  const suggestReanalyze = answer.suggested_actions.find((a) => a.kind === 'reanalyze');
  const note = suggestReanalyze
    ? `Suggests a fresh investigation: ${suggestReanalyze.reason}`
    : answer.history_truncated
      ? 'Earlier turns were dropped from context to stay within the history window.'
      : undefined;

  // A /similar-style answer carries historical matches server-attached,
  // never parsed from the model's own JSON (see agents/rca_agent/chat.py) —
  // the best match's similarity is the one number worth surfacing as a badge.
  const best = answer.historical_incidents.reduce<ChatAnswer['historical_incidents'][number] | null>(
    (acc, cur) => (!acc || cur.similarity > acc.similarity ? cur : acc),
    null,
  );
  const confidencePct = best ? Math.round(best.similarity * 100) : undefined;
  const tag = best ? 'pattern match' : undefined;

  const shareText = [lead, ...bullets].join('\n');
  return { lead, facts, bullets, sources: answer.citations, note, confidencePct, tag, shareText };
}

// The RCA verdict itself, rendered as the chat's first "message" — this is
// what a fresh Debug run actually kicks off (POST /api/rca), not a chat
// question over a verdict that doesn't exist yet. Confidence/status are the
// platform-computed numbers (RCAVerdict.confidence_score /
// root_cause_status), never llm_stated_confidence.
export function toVerdictCard(v: RCAVerdict): AnswerCardVM {
  const actionable = isActionable(v.root_cause_status);
  const confidencePct = Math.round(v.confidence_score * 100);
  const head = `Root-cause analysis — ${v.root_cause_status} (confidence ${confidencePct}%)`;
  const bullets = v.ranked_fix_steps.slice(0, 3).map((s) => s.description);
  const note = actionable
    ? undefined
    : 'The evidence does not settle this — the next step is a human looking, not a fix executing.';
  const shareText = [head, v.root_cause, ...bullets].join('\n');
  return {
    head,
    lead: v.root_cause,
    facts: [],
    bullets,
    sources: [],
    note,
    confidencePct,
    tag: v.root_cause_status,
    shareText,
  };
}
