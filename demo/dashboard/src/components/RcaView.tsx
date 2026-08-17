import { RefreshCw, ShieldAlert, CheckCircle2, XCircle, Undo2, Star, Clock, Check, X } from 'lucide-react';
import type { RCAVerdict, BlastRadius } from '@/types/api';
import { clsx } from '@/lib/format';
import { useHitlApply, phaseOf, type Phase } from '@/hooks/useHitlApply';

// ─── Shared RCA result renderer (RCA + remediation + auto-heal, merged) ─────
//
// The single source of truth for drawing a root-cause verdict AND driving its
// remediation end to end, ON ONE PAGE. The RCA Agent owns everything the former
// Remediation Recommender (PRS-001) and Auto-Healer (PRS-002) did: it presents a
// ranked set of executable REMEDIATION OPTIONS, and each option carries its own
// inline HITL flow — Apply fix → Approve / Deny (right here, no separate page) →
// on Approve the platform flips the flagd flag off (or runs the gated action)
// and the failure clears.
//
// The apply/approve state machine and option normalization live in
// useHitlApply (src/hooks/useHitlApply.ts) — shared with the Incident Command
// Center's remediation panel, so there is exactly one HITL flow in the app.
//
// Imported by the RCA Agent console (PRS-008 ★) and the Incident Commander
// console (RA-008). When the verdict has no ``remediation_options`` (the IC path
// doesn't compose them), useHitlApply falls back to rendering ``ranked_fix_steps``.

const BLAST_RADIUS_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

export function RcaView({
  v,
  incidentId,
  onResolved,
}: {
  v: RCAVerdict;
  incidentId: string | null;
  onResolved?: () => void;
}) {
  const { options, stateFor, apply, decide } = useHitlApply(v, incidentId, onResolved);

  return (
    <div className="space-y-3 text-sm">
      <div>
        <div className="flex items-baseline justify-between gap-2">
          <p className="card-title !text-[10px]">Root cause</p>
          <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">
            confidence {(v.confidence_score * 100).toFixed(0)}%
          </span>
        </div>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-900 dark:text-ink-50">{v.root_cause}</p>
      </div>

      <div>
        <div className="flex items-baseline justify-between gap-2">
          <p className="card-title !text-[10px]">Remediation options ({options.length})</p>
          <span className="text-[10px] text-ink-500 dark:text-ink-400">
            approve &amp; apply one — decided right here
          </span>
        </div>
        <ol className="mt-2 space-y-2">
          {options.map((opt, i) => {
            const st = stateFor(opt.id);
            const phase = phaseOf(st.status);
            const executable = !!opt.flag || !!opt.raw;
            return (
              <li
                key={opt.id}
                className={clsx(
                  'rounded-md border p-2.5',
                  opt.recommended
                    ? '!border-accent/50 bg-accent/5'
                    : 'border-ink-200 bg-ink-50/50 dark:border-ink-700 dark:bg-ink-800/30',
                )}
              >
                <div className="flex items-start gap-2">
                  <span className="mt-0.5 flex-shrink-0 rounded bg-ink-200 px-1.5 text-[10px] font-bold text-ink-700 dark:bg-ink-700 dark:text-ink-200">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-1.5">
                      <p className="text-sm font-medium leading-snug text-ink-900 dark:text-ink-50">{opt.title}</p>
                      {opt.recommended && (
                        <span className="chip !border-accent/40 !text-accent">
                          <Star className="mr-1 inline h-3 w-3" /> recommended
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[12px] leading-snug text-ink-600 dark:text-ink-300">{opt.description}</p>

                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <span className={clsx('chip', BLAST_RADIUS_STYLE[opt.blast_radius])}>blast: {opt.blast_radius}</span>
                      <span className="chip !border-accent/40 !text-accent">
                        <ShieldAlert className="mr-1 inline h-3 w-3" /> HITL required
                      </span>
                      {opt.flag ? (
                        <span className="chip !border-ok/40 !text-ok" title="One-click remediable (real flag flip)">
                          <CheckCircle2 className="mr-1 inline h-3 w-3" /> auto: set {opt.flag}→{opt.variant}
                        </span>
                      ) : (
                        <span
                          className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400"
                          title={opt.toolCapability ?? 'Manual — no automated executor'}
                        >
                          {opt.action_type}
                        </span>
                      )}
                      {opt.mttrMinutes != null && (
                        <span className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400">
                          <Clock className="mr-1 inline h-3 w-3" /> ~{opt.mttrMinutes}m
                        </span>
                      )}
                    </div>

                    <div className="mt-1.5 flex items-start gap-1 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
                      <Undo2 className="mt-0.5 h-3 w-3 flex-shrink-0 text-ink-500 dark:text-ink-400" />
                      <span>
                        <span className="text-ink-500 dark:text-ink-400">rollback:</span> {opt.rollback}
                      </span>
                    </div>

                    {executable ? (
                      <OptionApply
                        phase={phase}
                        dryRun={st.dryRun}
                        error={st.error}
                        approver={st.approver}
                        flag={opt.flag}
                        variant={opt.variant}
                        onApply={() => apply(opt)}
                        onApprove={() => decide(opt, 'approve')}
                        onDeny={() => decide(opt, 'deny')}
                      />
                    ) : (
                      <p className="mt-2 rounded-md border border-ink-200 bg-ink-50/50 p-2 text-[11px] text-ink-500 dark:border-ink-700 dark:bg-ink-800/30 dark:text-ink-400">
                        This option has no automated executor — perform it manually, then verify recovery.
                      </p>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      </div>

      <details>
        <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
          RCA decision trace ({v.audit_metadata.decision_trace.length} steps)
        </summary>
        <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
          {v.audit_metadata.decision_trace.map((line, i) => (
            <li key={i} className="leading-relaxed">{i + 1}. {line}</li>
          ))}
        </ol>
      </details>
    </div>
  );
}

function OptionApply({
  phase, dryRun, error, approver, flag, variant, onApply, onApprove, onDeny,
}: {
  phase: Phase;
  dryRun: boolean;
  error: string | null;
  approver: string | null;
  flag: string | null;
  variant: string;
  onApply: () => void;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const by = approver ? ` by ${approver}` : '';

  return (
    <div className="mt-2 rounded-md border border-accent/40 bg-accent/5 p-2.5">
      <div className="flex items-center justify-between gap-3">
        <p className="card-title !text-[10px]">
          {flag ? (
            <>Apply — set flag <span className="font-mono">{flag}</span> → <span className="font-mono">{variant}</span></>
          ) : (
            <>Apply this option{dryRun ? ' (dry-run)' : ''}</>
          )}
        </p>

        {/* idle → Apply fix (opens the gate). awaiting → inline Approve / Deny. */}
        {phase === 'idle' && (
          <button
            type="button"
            onClick={onApply}
            className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition hover:bg-accent/20"
          >
            <ShieldAlert className="h-3.5 w-3.5" /> Apply fix
          </button>
        )}
        {phase === 'opening' && (
          <span className="inline-flex items-center gap-1.5 text-xs text-ink-500 dark:text-ink-400">
            <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Requesting…
          </span>
        )}
        {phase === 'awaiting' && (
          <div className="flex flex-shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={onApprove}
              className="inline-flex items-center gap-1.5 rounded-md border border-ok/40 bg-ok/10 px-2.5 py-1 text-xs font-medium text-ok transition hover:bg-ok/20"
            >
              <Check className="h-3.5 w-3.5" /> Approve
            </button>
            <button
              type="button"
              onClick={onDeny}
              className="inline-flex items-center gap-1.5 rounded-md border border-bad/40 bg-bad/10 px-2.5 py-1 text-xs font-medium text-bad transition hover:bg-bad/20"
            >
              <X className="h-3.5 w-3.5" /> Deny
            </button>
          </div>
        )}
        {phase === 'deciding' && (
          <span className="inline-flex items-center gap-1.5 text-xs text-ink-500 dark:text-ink-400">
            <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Applying…
          </span>
        )}
        {phase === 'success' && (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-ok">
            <CheckCircle2 className="h-3.5 w-3.5" /> Applied
          </span>
        )}
        {(phase === 'denied' || phase === 'expired' || phase === 'blocked' || phase === 'error') && (
          <button
            type="button"
            onClick={onApply}
            className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition hover:bg-accent/20"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Retry
          </button>
        )}
      </div>

      {phase === 'idle' && (
        <p className="mt-2 text-[11px] text-ink-500 dark:text-ink-400">
          HITL-gated. Click <span className="font-medium text-ink-700 dark:text-ink-200">Apply fix</span> to open the
          approval right here — then Approve or Deny below.{flag ? ' The flag flips only after you approve.' : ''}
        </p>
      )}
      {phase === 'awaiting' && (
        <p className="mt-2 text-[11px] text-warn">
          Approval open — Approve to apply{flag ? ` (sets ${flag} → ${variant})` : ''}, or Deny to cancel. Nothing has changed yet.
          {error ? ` · ${error}` : ''}
        </p>
      )}
      {phase === 'success' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-ok">
          <CheckCircle2 className="h-3 w-3" /> Approved{by} —{' '}
          {flag
            ? `${flag} set to ${variant}. Failure clearing from Alert Stream + dashboard.`
            : dryRun
              ? 'dry-run only (no live executor for this action).'
              : 'applied.'}
        </p>
      )}
      {phase === 'denied' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-bad">
          <XCircle className="h-3 w-3" /> Denied{by} — no change made.
        </p>
      )}
      {phase === 'expired' && <p className="mt-2 text-[11px] text-warn">Approval expired — no change made.</p>}
      {(phase === 'blocked' || phase === 'error') && (
        <p className="mt-2 text-[11px] text-bad">{error || 'Could not apply this option.'}</p>
      )}
    </div>
  );
}
