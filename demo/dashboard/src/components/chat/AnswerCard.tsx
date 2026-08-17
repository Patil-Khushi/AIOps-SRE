import type { AnswerCardVM } from '@/lib/chatAnswerCard';
import { MessageActions } from './MessageActions';

export function AnswerCard({
  card,
  incidentId,
  service,
}: {
  card: AnswerCardVM;
  incidentId?: string | null;
  service?: string | null;
}) {
  return (
    <div className="rounded-lg border border-[var(--icc-border)] bg-[var(--icc-surface-2)] p-3 text-sm">
      {card.head && (
        <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">{card.head}</p>
      )}
      <p className={card.head ? 'mt-1 leading-relaxed text-[var(--icc-fg)]' : 'leading-relaxed text-[var(--icc-fg)]'}>
        {card.lead}
      </p>

      {(card.confidencePct !== undefined || card.tag) && (
        <div className="mt-1.5 flex items-center gap-1.5">
          {card.confidencePct !== undefined && (
            <span className="inline-flex items-center gap-1 text-[11px] text-[var(--icc-fg-muted)]">
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--icc-warn)]" />
              {card.confidencePct}% confidence
            </span>
          )}
          {card.tag && <span className="chip !text-[10px] !text-[var(--icc-accent)]">{card.tag}</span>}
        </div>
      )}

      {card.facts.length > 0 && (
        <dl className="mt-2 space-y-1 border-t border-[var(--icc-border)] pt-2">
          {card.facts.map((f, i) => (
            <div key={i} className="flex items-baseline justify-between gap-2 text-[12px]">
              <dt className="text-[var(--icc-fg-muted)]">{f.label}</dt>
              <dd className="font-mono text-[var(--icc-fg)]">{f.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {card.bullets.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-[var(--icc-border)] pt-2 text-[12px] text-[var(--icc-fg-muted)]">
          {card.bullets.map((b, i) => (
            <li key={i}>· {b}</li>
          ))}
        </ul>
      )}

      {card.sources.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1 border-t border-[var(--icc-border)] pt-2">
          {card.sources.map((s) => (
            <span key={s} className="chip font-mono !text-[10px]">
              {s}
            </span>
          ))}
        </div>
      )}

      {card.note && <p className="mt-2 text-[11px] italic text-[var(--icc-fg-faint)]">{card.note}</p>}

      <MessageActions shareText={card.shareText} title={card.head ?? card.lead} incidentId={incidentId} service={service} />
    </div>
  );
}
