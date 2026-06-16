import { useEffect, useState } from 'react';
import { RefreshCw, ShieldAlert, CheckCircle2, XCircle } from 'lucide-react';
import { api } from '@/lib/api';
import type { RCAVerdict, BlastRadius } from '@/types/api';
import { clsx } from '@/lib/format';

// ─── Shared RCA result renderer ─────────────────────────────────────────────
//
// Extracted from RcaConsole so both the RCA Agent console (PRS-008 ★) and the
// Incident Commander console (RA-008) draw a root-cause verdict identically:
// root cause + confidence, the REQUIRED-HITL "approve & apply" remediation box,
// ranked fix steps (each with a tested rollback), and the decision trace.
// Behavior is unchanged from the original in-page version.

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

type ApplyStatus =
  | 'idle' | 'pending' | 'executed' | 'denied' | 'expired' | 'blocked' | 'unsupported' | 'error';

function RemediationBox({
  flag, status, error, approver, onApply,
}: {
  flag: string;
  status: ApplyStatus;
  error: string | null;
  approver: string | null;
  onApply: () => void;
}) {
  const by = approver ? ` by ${approver}` : '';
  const busy = status === 'pending';
  const done = status === 'executed';
  const label = busy ? 'Awaiting approval…' : done ? 'Applied' : status === 'idle' ? 'Approve & apply' : 'Retry';

  return (
    <div className="rounded-md border border-accent/40 bg-accent/5 p-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="card-title !text-[10px]">Apply remediation</p>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Set flag <span className="font-mono text-ink-700 dark:text-ink-200">{flag}</span> →{' '}
            <span className="font-mono text-ink-700 dark:text-ink-200">off</span> · requires HITL approval
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
          <CheckCircle2 className="h-3 w-3" /> Approved{by} — {flag} set to off. The service should recover shortly.
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

export function RcaView({ v }: { v: RCAVerdict }) {
  const fixStep = v.ranked_fix_steps.find((s) => s.action_type === 'set_flag' && s.flag);
  const flag = fixStep?.flag ?? flagForService(v.affected_service);
  const fixVariant = fixStep?.variant ?? 'off';
  const [applyStatus, setApplyStatus] = useState<ApplyStatus>('idle');
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyApprover, setApplyApprover] = useState<string | null>(null);
  const [approvalId, setApprovalId] = useState<string | null>(null);

  useEffect(() => {
    setApplyStatus('idle');
    setApplyError(null);
    setApplyApprover(null);
    setApprovalId(null);
  }, [v]);

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
      const res = await api.applyRcaFix(flag, fixVariant, 'set_flag');
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

      {flag && (
        <RemediationBox
          flag={flag}
          status={applyStatus}
          error={applyError}
          approver={applyApprover}
          onApply={applyFix}
        />
      )}

      <div>
        <p className="card-title !text-[10px]">Ranked fix steps ({v.ranked_fix_steps.length})</p>
        <ol className="mt-2 space-y-2">
          {v.ranked_fix_steps.map((step, i) => (
            <li key={i} className="rounded-md border border-ink-200 bg-ink-50/50 p-2.5 dark:border-ink-700 dark:bg-ink-800/30">
              <div className="flex items-start gap-2">
                <span className="flex-shrink-0 rounded bg-ink-200 px-1.5 text-[10px] font-bold text-ink-700 dark:bg-ink-700 dark:text-ink-200">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm leading-snug text-ink-900 dark:text-ink-50">{step.description}</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <span className={clsx('chip', BLAST_RADIUS_STYLE[step.blast_radius])}>blast: {step.blast_radius}</span>
                    <span className="chip !border-accent/40 !text-accent">
                      <ShieldAlert className="mr-1 inline h-3 w-3" /> HITL required
                    </span>
                    {step.action_type === 'set_flag' && step.flag ? (
                      <span className="chip !border-ok/40 !text-ok" title="One-click remediable">
                        <CheckCircle2 className="mr-1 inline h-3 w-3" /> auto: set {step.flag}→{step.variant}
                      </span>
                    ) : (
                      <span className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400" title="No automated executor — perform manually">
                        manual
                      </span>
                    )}
                  </div>
                  <div className="mt-1.5 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
                    <span className="text-ink-500 dark:text-ink-400">rollback:</span> {step.rollback}
                  </div>
                </div>
              </div>
            </li>
          ))}
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
