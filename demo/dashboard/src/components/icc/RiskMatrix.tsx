import { Check, X, HelpCircle } from 'lucide-react';
import type { RiskAssessment } from '@/types/rca';
import { unassessedRisks } from '@/lib/rcaDerive';

const QUESTIONS: { key: keyof RiskAssessment; label: string }[] = [
  { key: 'causes_downtime', label: 'Causes downtime' },
  { key: 'interrupts_active_requests', label: 'Interrupts active requests' },
  { key: 'risks_data_loss', label: 'Risks data loss' },
  { key: 'risks_duplicate_transactions', label: 'Risks duplicate transactions' },
  { key: 'affects_downstream', label: 'Affects downstream' },
  { key: 'affects_upstream', label: 'Affects upstream' },
  { key: 'destroys_evidence', label: 'Destroys evidence' },
];

// Tri-state: true -> bad (real risk), false -> ok (checked, no risk), null ->
// unknown ("not assessed" — must never collapse to "safe", which is why null
// gets its own --icc-unknown treatment rather than being rendered as false).
function TriState({ value }: { value: boolean | null }) {
  if (value === null) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px]" style={{ color: 'var(--icc-unknown)' }}>
        <HelpCircle className="h-3.5 w-3.5" /> not assessed
      </span>
    );
  }
  return value ? (
    <span className="inline-flex items-center gap-1 text-[11px] text-[var(--icc-bad)]">
      <X className="h-3.5 w-3.5" /> yes
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-[11px] text-[var(--icc-ok)]">
      <Check className="h-3.5 w-3.5" /> no
    </span>
  );
}

export function RiskMatrix({ risk }: { risk: RiskAssessment }) {
  const unassessed = unassessedRisks(risk);
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">
          Risk level: {risk.level}
        </span>
        {unassessed.length > 0 && (
          <span className="text-[10px]" style={{ color: 'var(--icc-unknown)' }}>
            {unassessed.length} question(s) not assessed
          </span>
        )}
      </div>
      <dl className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {QUESTIONS.map((q) => (
          <div key={q.key} className="flex items-center justify-between gap-2 rounded-md border border-[var(--icc-border)] px-2 py-1">
            <dt className="text-[11px] text-[var(--icc-fg-muted)]">{q.label}</dt>
            <dd>
              <TriState value={risk[q.key] as boolean | null} />
            </dd>
          </div>
        ))}
      </dl>
      <div className="flex items-center gap-3 text-[11px] text-[var(--icc-fg-muted)]">
        <span>reversible: {risk.reversible ? 'yes' : 'no'}</span>
        <span>rollback available: {risk.rollback_available ? 'yes' : 'no'}</span>
      </div>
      {risk.concerns.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-[var(--icc-warn)]">
          {risk.concerns.map((c, i) => (
            <li key={i}>⚠ {c}</li>
          ))}
        </ul>
      )}
      {risk.safer_alternative && (
        <p className="text-[11px] text-[var(--icc-fg-faint)]">Safer alternative: {risk.safer_alternative}</p>
      )}
      <p className="text-[11px] text-[var(--icc-fg-muted)]">{risk.rationale}</p>
    </div>
  );
}
