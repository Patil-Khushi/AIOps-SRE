import type { Investigation } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';
import { usableForRanking } from '@/lib/rcaDerive';

// Historical evidence gets a persistently distinct "not current data"
// treatment (dashed, --icc-historical) throughout this tab — history can
// reorder a ranking but never manufacture confidence, and it must never be
// mistaken for evidence observed on this incident.
export function HistoryTab({ investigation }: { investigation: Investigation | null }) {
  if (!investigation) {
    return (
      <SectionShell state="unavailable" message="No historical influence data available">
        <div />
      </SectionShell>
    );
  }
  const hi = investigation.historical_influence;
  const allPriors = investigation.matrices.flatMap((m) => m.priors.map((p) => ({ prior: p, category: m.hypothesis.category })));

  return (
    <div className="space-y-3">
      <div
        className="icc-dashed rounded-lg border p-3"
        style={{ borderColor: 'var(--icc-historical)', background: 'color-mix(in srgb, var(--icc-historical) 8%, transparent)' }}
      >
        <p className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--icc-historical)' }}>
          Historical evidence — not current data
        </p>
        <p className="mt-1 text-xs text-[var(--icc-fg-muted)]">
          Influence: <strong className="text-[var(--icc-fg)]">{hi.level}</strong> · {hi.priors_considered} considered,{' '}
          {hi.priors_eligible} eligible, {hi.priors_applied.length} applied
          {hi.changed_ranking ? ' · changed the ranking' : ''}
        </p>
        {hi.overridden_by_current_evidence.length > 0 && (
          <p className="mt-1 text-[11px] text-[var(--icc-fg-faint)]">
            Overridden by current evidence: {hi.overridden_by_current_evidence.join(', ')}
          </p>
        )}
        {hi.note && <p className="mt-1 text-[11px] text-[var(--icc-fg-faint)]">{hi.note}</p>}
      </div>

      {allPriors.length === 0 ? (
        <SectionShell state="empty" message="No prior incidents matched">
          <div />
        </SectionShell>
      ) : (
        <ul className="space-y-1.5">
          {allPriors.map(({ prior, category }) => {
            const usable = usableForRanking(prior.status);
            return (
              <li
                key={prior.memory_id}
                className="icc-dashed rounded-md border p-2.5 text-xs"
                style={{ borderColor: 'var(--icc-historical)', opacity: usable ? 1 : 0.55 }}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-medium text-[var(--icc-fg)]">{category}</span>
                  <span className="font-mono text-[10px] text-[var(--icc-fg-faint)]">
                    similarity {prior.similarity.toFixed(2)} · {prior.status}
                  </span>
                </div>
                {prior.recorded_cause && <p className="mt-0.5 text-[var(--icc-fg-muted)]">{prior.recorded_cause}</p>}
                {!usable && (
                  <p className="mt-0.5 text-[10px] text-[var(--icc-fg-faint)]">not eligible to influence ranking</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
