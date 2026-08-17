import { ShieldAlert, CheckCircle2, XCircle, RefreshCw, Star, Clock, Undo2 } from 'lucide-react';
import type { DisplayOption, Phase } from '@/hooks/useHitlApply';
import type { RecoveryOption } from '@/types/rca';
import { RiskMatrix } from './RiskMatrix';
import { clsx } from '@/lib/format';

const BLAST_RADIUS_STYLE: Record<string, string> = {
  low: 'border-[var(--icc-ok)]/40 text-[var(--icc-ok)]',
  medium: 'border-[var(--icc-warn)]/40 text-[var(--icc-warn)]',
  high: 'border-[var(--icc-bad)]/40 text-[var(--icc-bad)]',
};

export function RemediationCard({
  option,
  recovery,
  phase,
  error,
  onApply,
  onReview,
}: {
  option: DisplayOption;
  recovery?: RecoveryOption;
  phase: Phase;
  error: string | null;
  onApply: () => void;
  onReview: () => void;
}) {
  const executable = !!option.flag || !!option.raw;

  return (
    <li
      className={clsx(
        'rounded-lg border p-3',
        option.recommended ? 'border-[var(--icc-accent)]/50 bg-[var(--icc-accent-soft)]' : 'border-[var(--icc-border)]',
      )}
    >
      <div className="flex flex-wrap items-baseline gap-1.5">
        <p className="text-sm font-medium text-[var(--icc-fg)]">{option.title}</p>
        {option.recommended && (
          <span className="chip !border-[var(--icc-accent)]/40 !text-[var(--icc-accent)]">
            <Star className="mr-1 inline h-3 w-3" /> recommended
          </span>
        )}
      </div>
      <p className="mt-0.5 text-xs text-[var(--icc-fg-muted)]">{option.description}</p>

      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <span className={clsx('chip', BLAST_RADIUS_STYLE[option.blast_radius])}>blast: {option.blast_radius}</span>
        <span className="chip !border-[var(--icc-accent)]/40 !text-[var(--icc-accent)]">
          <ShieldAlert className="mr-1 inline h-3 w-3" /> HITL required
        </span>
        {option.flag && (
          <span className="chip !border-[var(--icc-ok)]/40 !text-[var(--icc-ok)]">
            <CheckCircle2 className="mr-1 inline h-3 w-3" /> auto: {option.flag}→{option.variant}
          </span>
        )}
        {option.mttrMinutes != null && (
          <span className="chip !text-[var(--icc-fg-muted)]">
            <Clock className="mr-1 inline h-3 w-3" /> ~{option.mttrMinutes}m
          </span>
        )}
      </div>

      <div className="mt-1.5 flex items-start gap-1 rounded bg-[var(--icc-surface-2)] px-2 py-1 font-mono text-[11px] text-[var(--icc-fg-muted)]">
        <Undo2 className="mt-0.5 h-3 w-3 flex-shrink-0" />
        <span>rollback: {option.rollback}</span>
      </div>

      {recovery && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] text-[var(--icc-fg-faint)] hover:text-[var(--icc-accent)]">
            risk assessment {!recovery.grounded && '· not grounded against the action registry'}
          </summary>
          <div className="mt-2">
            <RiskMatrix risk={recovery.risk} />
          </div>
        </details>
      )}

      {!executable ? (
        <p className="mt-2 rounded-md border border-[var(--icc-border)] bg-[var(--icc-surface-2)] p-2 text-[11px] text-[var(--icc-fg-muted)]">
          No automated action available for this option — perform it manually, then verify recovery.
        </p>
      ) : (
        <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent-soft)] p-2.5">
          {phase === 'idle' && (
            <>
              <p className="text-[11px] text-[var(--icc-fg-muted)]">Nothing applied yet.</p>
              <button
                type="button"
                onClick={onApply}
                className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent)]/10 px-2.5 py-1 text-xs font-medium text-[var(--icc-accent)] transition hover:bg-[var(--icc-accent)]/20"
              >
                <ShieldAlert className="h-3.5 w-3.5" /> Apply fix
              </button>
            </>
          )}
          {phase === 'opening' && (
            <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--icc-fg-muted)]">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Requesting…
            </span>
          )}
          {phase === 'awaiting' && (
            <>
              <p className="text-[11px] text-[var(--icc-warn)]">Approval open — nothing changed yet.</p>
              <button
                type="button"
                onClick={onReview}
                className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent)]/10 px-2.5 py-1 text-xs font-medium text-[var(--icc-accent)] transition hover:bg-[var(--icc-accent)]/20"
              >
                Review
              </button>
            </>
          )}
          {phase === 'deciding' && (
            <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--icc-fg-muted)]">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Applying…
            </span>
          )}
          {phase === 'success' && (
            <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-[var(--icc-ok)]">
              <CheckCircle2 className="h-3.5 w-3.5" /> Applied
            </span>
          )}
          {(phase === 'denied' || phase === 'expired' || phase === 'blocked' || phase === 'error') && (
            <>
              <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--icc-bad)]">
                <XCircle className="h-3.5 w-3.5" /> {error || phase}
              </span>
              <button
                type="button"
                onClick={onApply}
                className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent)]/10 px-2.5 py-1 text-xs font-medium text-[var(--icc-accent)] transition hover:bg-[var(--icc-accent)]/20"
              >
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </button>
            </>
          )}
        </div>
      )}
    </li>
  );
}
