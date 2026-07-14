import { useEffect, useState } from 'react';
import { RefreshCw, ShieldAlert, CheckCircle2, XCircle, Undo2, Star, Clock, Check, X } from 'lucide-react';
import { api } from '@/lib/api';
import type { RCAVerdict, BlastRadius, RankedFixStep, RemediationOption } from '@/types/api';
import { clsx } from '@/lib/format';

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
// Flow per option:
//   1. Apply fix  → opens the REQUIRED-HITL gate (api.applyRcaFix / executeOption)
//   2. Approve / Deny inline → api.approve / api.deny on the SAME approval id
//   3. On Approve: flag-flip options really set the flag off (+ resolution
//      verifier); other actions run through the gated execute seam.
//   4. onResolved() fires when a real flag flip executes so the parent can drop
//      the now-resolved incident from the list.
//
// Imported by the RCA Agent console (PRS-008 ★) and the Incident Commander
// console (RA-008). When the verdict has no ``remediation_options`` (the IC path
// doesn't compose them), we fall back to rendering ``ranked_fix_steps``.

const OPERATOR = 'rca-console';

const BLAST_RADIUS_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

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

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

// A single option, normalized from either a RemediationOption (POST /api/rca) or
// a RankedFixStep (Incident Commander fallback). ``flag`` is set only when the
// option is a one-click flag flip; ``raw`` is the original RemediationOption used
// to drive the gated execute seam for non-flag actions.
interface DisplayOption {
  id: string;
  title: string;
  description: string;
  blast_radius: BlastRadius;
  action_type: string;
  rollback: string;
  mttrMinutes: number | null;
  toolCapability: string | null;
  recommended: boolean;
  flag: string | null;
  variant: string;
  raw: RemediationOption | null;
}

function optionsFromVerdict(v: RCAVerdict): DisplayOption[] {
  const service = v.affected_service;
  if (v.remediation_options && v.remediation_options.length > 0) {
    return v.remediation_options.map((o) => {
      const isFlag = o.action_type === 'set_flag';
      const flag = isFlag ? (str(o.tool_args?.flag) ?? flagForService(service)) : null;
      const variant = str(o.tool_args?.variant) ?? 'off';
      return {
        id: o.option_id,
        title: o.title,
        description: o.description,
        blast_radius: o.blast_radius,
        action_type: o.action_type,
        rollback: o.rollback,
        mttrMinutes: o.estimated_mttr_minutes,
        toolCapability: o.tool_capability,
        recommended: o.option_id === v.recommended_option_id,
        flag,
        variant,
        raw: o,
      };
    });
  }
  return v.ranked_fix_steps.map((s: RankedFixStep, i: number) => {
    const isFlag = s.action_type === 'set_flag';
    const flag = isFlag ? (s.flag ?? flagForService(service)) : null;
    return {
      id: `step-${i}`,
      title: `Fix step ${i + 1}`,
      description: s.description,
      blast_radius: s.blast_radius,
      action_type: s.action_type,
      rollback: s.rollback,
      mttrMinutes: null,
      toolCapability: isFlag ? 'feature_flags.set_variant' : null,
      recommended: i === 0,
      flag,
      variant: s.variant ?? 'off',
      raw: null,
    };
  });
}

// Local status (not just backend outcome). 'opening' = firing the apply request;
// 'awaiting' = HITL gate open, showing inline Approve/Deny; 'deciding' = the
// approve/deny call + executor is running. The rest come from the outcome store.
type RawStatus =
  | 'idle' | 'opening' | 'awaiting' | 'deciding'
  | 'executed' | 'dry_run_ok' | 'approved'
  | 'denied' | 'expired' | 'blocked' | 'refused' | 'pending_approval'
  | 'execution_failed' | 'unsupported' | 'error' | 'pending';
type Phase = 'idle' | 'opening' | 'awaiting' | 'deciding' | 'success' | 'denied' | 'expired' | 'blocked' | 'error';

function phaseOf(status: RawStatus): Phase {
  switch (status) {
    case 'idle': return 'idle';
    case 'opening': return 'opening';
    case 'awaiting': return 'awaiting';
    case 'deciding':
    case 'pending':
    case 'pending_approval': return 'deciding';
    case 'executed':
    case 'dry_run_ok':
    case 'approved': return 'success';
    case 'denied': return 'denied';
    case 'expired': return 'expired';
    case 'blocked':
    case 'refused': return 'blocked';
    default: return 'error'; // execution_failed | unsupported | error
  }
}

interface ApplyState {
  status: RawStatus;
  error: string | null;
  approver: string | null;
  approvalId: string | null;
  dryRun: boolean;
}
const IDLE: ApplyState = { status: 'idle', error: null, approver: null, approvalId: null, dryRun: false };

export function RcaView({
  v,
  incidentId,
  onResolved,
}: {
  v: RCAVerdict;
  incidentId: string | null;
  onResolved?: () => void;
}) {
  const options = optionsFromVerdict(v);
  const [applyById, setApplyById] = useState<Record<string, ApplyState>>({});

  useEffect(() => {
    setApplyById({});
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps

  const stateFor = (id: string): ApplyState => applyById[id] ?? IDLE;
  const patch = (id: string, next: Partial<ApplyState>) =>
    setApplyById((prev) => ({ ...prev, [id]: { ...(prev[id] ?? IDLE), ...next } }));

  // Poll every option whose gate is open or executing. Once terminal, stop; if a
  // real flag flip executed, tell the parent so it can drop the resolved incident.
  useEffect(() => {
    const active = Object.entries(applyById).filter(
      ([, s]) => s.approvalId && (s.status === 'awaiting' || s.status === 'deciding'),
    );
    if (active.length === 0) return;
    let alive = true;
    const timer = setInterval(() => {
      active.forEach(async ([id, s]) => {
        if (!s.approvalId) return;
        try {
          const o = await api.hitlOutcome(s.approvalId);
          if (!alive || !o.status || o.status === 'pending') return;
          patch(id, { status: o.status as RawStatus, error: o.error ?? null, approver: o.approver ?? null });
          const opt = options.find((x) => x.id === id);
          if (opt?.flag && phaseOf(o.status as RawStatus) === 'success') onResolved?.();
        } catch {
          /* transient — keep polling */
        }
      });
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [applyById]); // eslint-disable-line react-hooks/exhaustive-deps

  // Step 1 — fire the gated request; move to 'awaiting' so the inline Approve/Deny appears.
  const applyOption = async (opt: DisplayOption) => {
    patch(opt.id, { status: 'opening', error: null, approver: null });
    try {
      if (opt.flag) {
        const context: Record<string, unknown> = {
          service: v.affected_service,
          rca_verdict: v,
          timeout_seconds: 600,
        };
        if (incidentId) context.incident_id = incidentId;
        const res = await api.applyRcaFix(opt.flag, opt.variant, 'set_flag', undefined, context);
        patch(opt.id, { approvalId: res.approval_id, status: 'awaiting', dryRun: false });
      } else if (opt.raw) {
        const res = await api.executeOption(opt.raw, v.affected_service, {
          incidentId: incidentId ?? undefined,
          dryRun: true,
        });
        patch(opt.id, { approvalId: res.approval_id, status: 'awaiting', dryRun: true });
      } else {
        patch(opt.id, { status: 'error', error: 'This option has no automated executor — perform it manually.' });
      }
    } catch (e) {
      patch(opt.id, { status: 'error', error: e instanceof Error ? e.message : String(e) });
    }
  };

  // Step 2 — the HITL decision, INLINE (no separate Approvals page). Approving
  // unblocks the gate; the poller then reports executed → the flag is off.
  const decideOption = async (opt: DisplayOption, kind: 'approve' | 'deny') => {
    const s = stateFor(opt.id);
    if (!s.approvalId) return;
    patch(opt.id, { status: 'deciding', error: null });
    try {
      if (kind === 'approve') await api.approve(s.approvalId, OPERATOR, 'Approved from RCA console');
      else await api.deny(s.approvalId, OPERATOR, 'Denied from RCA console');
      // Leave it in 'deciding' — the poller flips it to the real outcome.
    } catch (e) {
      // Approval registry may not have the request yet (race) — let them retry.
      patch(opt.id, { status: 'awaiting', error: e instanceof Error ? e.message : String(e) });
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
                        onApply={() => applyOption(opt)}
                        onApprove={() => decideOption(opt, 'approve')}
                        onDeny={() => decideOption(opt, 'deny')}
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
