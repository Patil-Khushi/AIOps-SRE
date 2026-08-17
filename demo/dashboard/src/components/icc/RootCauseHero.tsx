import type { RCAVerdict } from '@/types/api';
import type { Investigation } from '@/types/rca';
import { tiedCandidates } from '@/lib/rcaDerive';
import { ConfidenceRing } from './ConfidenceRing';
import { StatusVerdictChip } from './StatusVerdictChip';

// UNCERTAIN is a first-class state: no single confident root cause, no
// remediation button (that gate lives in RemediationPanel) — both tied
// candidates are shown here instead of a winner.
export function RootCauseHero({
  verdict,
  investigation,
}: {
  verdict: RCAVerdict;
  investigation: Investigation | null;
}) {
  const tied = investigation ? tiedCandidates(investigation) : [];
  const isTied = tied.length === 2;

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-[var(--icc-border)] bg-[var(--icc-surface)] p-5 sm:flex-row sm:items-center">
      <ConfidenceRing
        value={verdict.confidence_score}
        status={verdict.root_cause_status}
        llmStated={verdict.llm_stated_confidence}
      />
      <div className="min-w-0 flex-1">
        <StatusVerdictChip status={verdict.root_cause_status} />
        {isTied ? (
          <div className="mt-2 space-y-2">
            <p className="text-sm text-[var(--icc-fg-muted)]">
              The evidence does not discriminate between the top two candidates:
            </p>
            {tied.map((m) => (
              <div key={m.hypothesis.hypothesis_id} className="rounded-md border border-[var(--icc-border)] p-2">
                <p className="text-sm font-medium text-[var(--icc-fg)]">{m.hypothesis.category}</p>
                <p className="text-xs text-[var(--icc-fg-muted)]">{m.hypothesis.mechanism}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-sm leading-relaxed text-[var(--icc-fg)]">{verdict.root_cause}</p>
        )}
        <p className="mt-2 font-mono text-[11px] text-[var(--icc-fg-faint)]">{verdict.affected_service}</p>
      </div>
    </div>
  );
}
