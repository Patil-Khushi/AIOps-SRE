import type { Investigation } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';
import { clsx } from '@/lib/format';

// Ranked best-first (investigation.matrices[0] is the winner). When the
// investigation did NOT discriminate (top two within the margin), no winner
// styling is applied to either of the top two — showing a confident winner
// when the platform itself couldn't pick one would misrepresent the verdict.
export function HypothesesTab({ investigation }: { investigation: Investigation | null }) {
  if (!investigation) {
    return (
      <SectionShell state="unavailable" message="No investigation available" reason="The deterministic investigation stage did not run for this verdict.">
        <div />
      </SectionShell>
    );
  }
  if (investigation.matrices.length === 0) {
    return (
      <SectionShell state="empty" message="No hypotheses were scored">
        <div />
      </SectionShell>
    );
  }

  const tiedTop2 = !investigation.discriminated;

  return (
    <div className="space-y-3">
      {!investigation.discriminated && (
        <p className="icc-dashed rounded-md border border-[var(--icc-unknown)]/40 px-3 py-2 text-xs text-[var(--icc-unknown)]">
          The top two candidates are close enough that the platform did not discriminate between them.
        </p>
      )}
      <ol className="space-y-2">
        {investigation.matrices.map((m, i) => {
          const isWinner = i === 0 && !tiedTop2;
          const score = m.score?.score ?? 0;
          return (
            <li
              key={m.hypothesis.hypothesis_id}
              className={clsx(
                'rounded-lg border p-3',
                isWinner ? 'border-[var(--icc-accent)]/50 bg-[var(--icc-accent-soft)]' : 'border-[var(--icc-border)]',
              )}
            >
              <div className="flex items-baseline justify-between gap-2">
                <p className="text-sm font-medium text-[var(--icc-fg)]">
                  {i + 1}. {m.hypothesis.category}
                  {isWinner && <span className="ml-2 text-[10px] uppercase text-[var(--icc-accent)]">selected</span>}
                </p>
                <span className="font-mono text-xs text-[var(--icc-fg-muted)]">score {score.toFixed(2)}</span>
              </div>
              <p className="mt-1 text-xs text-[var(--icc-fg-muted)]">{m.hypothesis.mechanism}</p>
              {m.score && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-[11px] text-[var(--icc-fg-faint)] hover:text-[var(--icc-accent)]">
                    scoring factors ({m.score.factors.length}) {m.score.capped && '· capped'}
                  </summary>
                  <ul className="mt-1 space-y-0.5 border-l border-[var(--icc-border)] pl-2 font-mono text-[11px] text-[var(--icc-fg-muted)]">
                    {m.score.factors.map((f) => (
                      <li key={f.rule_id}>
                        {f.delta >= 0 ? '+' : ''}{f.delta.toFixed(2)} — {f.description}
                      </li>
                    ))}
                    {m.score.unapplied.map((u) => (
                      <li key={u.rule_id} className="text-[var(--icc-fg-faint)]">
                        (not applied) {u.reason}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <div className="mt-2 grid grid-cols-2 gap-1 text-[11px] text-[var(--icc-fg-faint)] sm:grid-cols-4">
                <span>supports: {m.supporting.length}</span>
                <span>contradicts: {m.contradicting.length}</span>
                <span>checked-absent: {m.checked_absent.length}</span>
                <span className={m.gaps.length > 0 ? 'text-[var(--icc-gap)]' : undefined}>gaps: {m.gaps.length}</span>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
