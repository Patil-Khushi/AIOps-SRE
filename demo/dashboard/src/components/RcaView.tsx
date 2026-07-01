import { useEffect, useState } from 'react';
import { RefreshCw, ShieldAlert, CheckCircle2, XCircle, Undo2 } from 'lucide-react';
import { api } from '@/lib/api';
import type { RCAVerdict, BlastRadius, RankedFixStep } from '@/types/api';
import { clsx } from '@/lib/format';

// ─── Shared RCA result renderer ─────────────────────────────────────────────
//
// The single source of truth for drawing a root-cause verdict AND driving its
// remediation: root cause + confidence, a HUMAN-SELECTABLE list of ranked fix
// steps (each with a tested rollback), and a REQUIRED-HITL "approve & apply"
// gate for the step the operator picks. This is where the former standalone
// Remediation Recommender folds in — RCA no longer just shows steps, it lets a
// human choose which one to run and approve it. Imported by both the RCA Agent
// console (PRS-008 ★) and the Incident Commander console (RA-008) so the two
// never drift.
//
// ``incidentId`` is the ServiceNow incident number for the verdict. When set,
// apply-fix forwards it (+ service + RCA verdict) so the backend persists the
// verdict and fires the resolution verifier after the flag flip — that's what
// raises the 2nd (ticket-close) HITL approval. With no incident_id the verifier
// is skipped and only the fix approval appears.

const BLAST_RADIUS_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

// Maps an affected service to the flagd failure flag whose flip is the real,
// reversible remediation. Only services with a known flag get the
// "Approve & apply" action — everything else stays advisory-only.
const SERVICE_FLAG: Record<string, string> = {
  payment: 'paymentFailure',
  paymentservice: 'paymentFailure',
  productcatalog: 'productCatalogFailure',
  'product-catalog': 'productCatalogFailure',
  productcatalogservice: 'productCatalogFailure',
  cart: 'cartFailure',
  cartservice: 'cartFailure',
  ad: 'adFailure',
  adservice: 'adFailure',
  recommendation: 'recommendationCacheFailure',
  recommendationservice: 'recommendationCacheFailure',
};

function flagForService(service: string): string | null {
  return SERVICE_FLAG[service.toLowerCase().trim()] ?? null;
}

// A step is one-click executable when it flips a known feature flag. Everything
// else is advisory — the operator carries it out manually.
function stepFlag(step: RankedFixStep): string | null {
  return step.action_type === 'set_flag' && step.flag ? step.flag : null;
}

type ApplyStatus =
  | 'idle' | 'pending' | 'executed' | 'denied' | 'expired' | 'blocked' | 'unsupported' | 'error';

function ApprovalBox({
  flag, variant, status, error, approver, onApply, closeFollows = false,
}: {
  flag: string;
  variant: string;
  status: ApplyStatus;
  error: string | null;
  approver: string | null;
  onApply: () => void;
  closeFollows?: boolean;
}) {
  const by = approver ? ` by ${approver}` : '';
  const busy = status === 'pending';
  const done = status === 'executed';
  const label = busy ? 'Awaiting approval…' : done ? 'Applied' : status === 'idle' ? 'Approve & apply' : 'Retry';

  return (
    <div className="mt-2 rounded-md border border-accent/40 bg-accent/5 p-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="card-title !text-[10px]">Apply this step</p>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Set flag <span className="font-mono text-ink-700 dark:text-ink-200">{flag}</span> →{' '}
            <span className="font-mono text-ink-700 dark:text-ink-200">{variant}</span> · requires HITL approval
          </p>
        </div>
        <button
          type="button"
          onClick={onApply}
          disabled={busy || done}
          className={clsx(
            'inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition',
            'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20',
            'disabled:cursor-not-allowed disabled:opacity-50',
          )}
        >
          {busy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : done ? <CheckCircle2 className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
          {label}
        </button>
      </div>

      {status === 'pending' && (
        <p className="mt-2 text-[11px] text-ink-500 dark:text-ink-400">
          Approval requested — approve or deny in the Approvals console or Slack. The flag is unchanged until then.
        </p>
      )}
      {status === 'executed' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-ok">
          <CheckCircle2 className="h-3 w-3" /> Approved{by} — {flag} set to {variant}.{' '}
          {closeFollows
            ? 'Verifying recovery — a ticket-close approval will appear in the HITL console shortly.'
            : 'The service should recover shortly.'}
        </p>
      )}
      {status === 'denied' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-bad">
          <XCircle className="h-3 w-3" /> Denied{by} — flag left unchanged.
        </p>
      )}
      {status === 'expired' && <p className="mt-2 text-[11px] text-warn">Approval expired — no change made.</p>}
      {(status === 'error' || status === 'blocked' || status === 'unsupported') && (
        <p className="mt-2 text-[11px] text-bad">{error || 'Could not apply the fix.'}</p>
      )}
    </div>
  );
}

export function RcaView({ v, incidentId }: { v: RCAVerdict; incidentId: string | null }) {
  const steps = v.ranked_fix_steps;
  // Default the selection to the first one-click-executable step (so the safest
  // remediable action is pre-highlighted); fall back to the first step.
  const firstExecutable = steps.findIndex((s) => stepFlag(s));
  const [selectedStep, setSelectedStep] = useState(firstExecutable >= 0 ? firstExecutable : 0);

  const [applyStatus, setApplyStatus] = useState<ApplyStatus>('idle');
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyApprover, setApplyApprover] = useState<string | null>(null);
  const [approvalId, setApprovalId] = useState<string | null>(null);

  const chosen: RankedFixStep | undefined = steps[selectedStep];
  // The flag the chosen step would flip. Fall back to the service's known flag
  // so a set_flag step with a missing flag field still resolves to something.
  const flag = chosen
    ? (stepFlag(chosen) ?? (chosen.action_type === 'set_flag' ? flagForService(v.affected_service) : null))
    : null;
  const fixVariant = chosen?.variant ?? 'off';

  // New verdict → reset the selection to its safest executable step and clear
  // any in-flight approval state.
  useEffect(() => {
    const idx = steps.findIndex((s) => stepFlag(s));
    setSelectedStep(idx >= 0 ? idx : 0);
    setApplyStatus('idle');
    setApplyError(null);
    setApplyApprover(null);
    setApprovalId(null);
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps

  // Switching to a different step abandons the previous step's approval state —
  // each step is approved on its own.
  const pickStep = (i: number) => {
    if (i === selectedStep) return;
    setSelectedStep(i);
    setApplyStatus('idle');
    setApplyError(null);
    setApplyApprover(null);
    setApprovalId(null);
  };

  useEffect(() => {
    if (applyStatus !== 'pending' || !approvalId) return;
    let alive = true;
    const timer = setInterval(async () => {
      try {
        const o = await api.hitlOutcome(approvalId);
        if (!alive || !o.status || o.status === 'pending') return;
        setApplyStatus(o.status as ApplyStatus);
        setApplyError(o.error ?? null);
        setApplyApprover(o.approver ?? null);
        clearInterval(timer);
      } catch {
        /* transient — keep polling */
      }
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [applyStatus, approvalId]);

  const applyFix = async () => {
    if (!flag) return;
    setApplyStatus('pending');
    setApplyError(null);
    try {
      // Always forward the service + RCA verdict so the backend fires the
      // resolution verifier after the flag flip — that's what raises the 2nd
      // (ticket-close) HITL approval. Include the incident number when we have
      // it; when the analyzed verdict was a Suppressed duplicate with no ticket,
      // the backend recovers the open incident for this service so the close
      // approval still appears.
      const context: Record<string, unknown> = {
        service: v.affected_service,
        rca_verdict: v,
      };
      if (incidentId) context.incident_id = incidentId;
      const res = await api.applyRcaFix(flag, fixVariant, 'set_flag', undefined, context);
      setApprovalId(res.approval_id);
    } catch (e) {
      setApplyStatus('error');
      setApplyError(e instanceof Error ? e.message : String(e));
    }
  };

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
          <p className="card-title !text-[10px]">Ranked fix steps ({steps.length})</p>
          <span className="text-[10px] text-ink-500 dark:text-ink-400">select a step to approve</span>
        </div>
        <ol className="mt-2 space-y-2">
          {steps.map((step, i) => {
            const executable = !!stepFlag(step);
            const isSelected = i === selectedStep;
            return (
              <li key={i}>
                <button
                  type="button"
                  onClick={() => pickStep(i)}
                  aria-pressed={isSelected}
                  className={clsx(
                    'w-full rounded-md border p-2.5 text-left transition-colors',
                    isSelected
                      ? '!border-accent bg-accent/5 ring-1 ring-accent/30'
                      : 'border-ink-200 bg-ink-50/50 hover:border-accent/50 dark:border-ink-700 dark:bg-ink-800/30',
                  )}
                >
                  <div className="flex items-start gap-2">
                    {/* Radio indicator — makes the single-select nature obvious. */}
                    <span
                      className={clsx(
                        'mt-0.5 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full border',
                        isSelected ? 'border-accent' : 'border-ink-300 dark:border-ink-600',
                      )}
                    >
                      {isSelected && <span className="h-2 w-2 rounded-full bg-accent" />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-1.5">
                        <span className="flex-shrink-0 rounded bg-ink-200 px-1.5 text-[10px] font-bold text-ink-700 dark:bg-ink-700 dark:text-ink-200">
                          {i + 1}
                        </span>
                        <p className="text-sm leading-snug text-ink-900 dark:text-ink-50">{step.description}</p>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <span className={clsx('chip', BLAST_RADIUS_STYLE[step.blast_radius])}>blast: {step.blast_radius}</span>
                        <span className="chip !border-accent/40 !text-accent">
                          <ShieldAlert className="mr-1 inline h-3 w-3" /> HITL required
                        </span>
                        {executable ? (
                          <span className="chip !border-ok/40 !text-ok" title="One-click remediable">
                            <CheckCircle2 className="mr-1 inline h-3 w-3" /> auto: set {step.flag}→{step.variant}
                          </span>
                        ) : (
                          <span className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400" title="No automated executor — perform manually">
                            manual
                          </span>
                        )}
                      </div>
                      <div className="mt-1.5 flex items-start gap-1 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
                        <Undo2 className="mt-0.5 h-3 w-3 flex-shrink-0 text-ink-500 dark:text-ink-400" />
                        <span><span className="text-ink-500 dark:text-ink-400">rollback:</span> {step.rollback}</span>
                      </div>

                      {/* The approval gate renders inline under the SELECTED step. */}
                      {isSelected && (
                        flag ? (
                          <ApprovalBox
                            flag={flag}
                            variant={fixVariant}
                            status={applyStatus}
                            error={applyError}
                            approver={applyApprover}
                            onApply={applyFix}
                            closeFollows={!!incidentId}
                          />
                        ) : (
                          <p className="mt-2 rounded-md border border-ink-200 bg-ink-50/50 p-2 text-[11px] text-ink-500 dark:border-ink-700 dark:bg-ink-800/30 dark:text-ink-400">
                            This step has no automated executor — perform it manually, then verify recovery.
                          </p>
                        )
                      )}
                    </div>
                  </div>
                </button>
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
