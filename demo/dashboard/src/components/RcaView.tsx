import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { RefreshCw, ShieldAlert, CheckCircle2, XCircle, Undo2, Gavel, Star, Clock } from 'lucide-react';
import { api } from '@/lib/api';
import type { RCAVerdict, BlastRadius, RankedFixStep, RemediationOption } from '@/types/api';
import { clsx } from '@/lib/format';

// ─── Shared RCA result renderer (RCA + remediation + auto-heal, merged) ─────
//
// The single source of truth for drawing a root-cause verdict AND driving its
// remediation. The RCA Agent now owns everything the former Remediation
// Recommender (PRS-001) and Auto-Healer (PRS-002) did: it presents a ranked set
// of executable REMEDIATION OPTIONS, and EACH option carries its own
// REQUIRED-HITL "Approve & apply" control. The operator picks one option and
// approves it; RCA drives the execution (no separate agent):
//   • flag-flip options  → apply-fix seam (real flagd flip + resolution verifier)
//   • other options      → the gated execute seam (rollback / scale / restart)
//
// Imported by the RCA Agent console (PRS-008 ★) and the Incident Commander
// console (RA-008). When the verdict has no ``remediation_options`` (the IC
// path doesn't compose them), we fall back to rendering ``ranked_fix_steps``.
//
// ``incidentId`` is the ServiceNow incident number. When set, a flag-flip apply
// forwards it (+ service + RCA verdict) so the backend fires the resolution
// verifier after the flip — that's what raises the 2nd (ticket-close) HITL.

const BLAST_RADIUS_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

// Maps an affected service to the flagd failure flag whose flip is the real,
// reversible remediation. Used as a fallback when a set_flag option doesn't
// carry its flag in tool_args (and for the ranked_fix_steps fallback path).
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

// A single option, normalized from either a RemediationOption (POST /api/rca)
// or a RankedFixStep (Incident Commander fallback). ``flag`` is set only when
// the option is a one-click flag flip; ``raw`` is the original RemediationOption
// used to drive the gated execute seam for non-flag actions.
interface DisplayOption {
  id: string;
  title: string;
  description: string;
  blast_radius: BlastRadius;
  action_type: string;
  rollback: string;
  rationale: string | null;
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
        rationale: o.rationale,
        mttrMinutes: o.estimated_mttr_minutes,
        toolCapability: o.tool_capability,
        recommended: o.option_id === v.recommended_option_id,
        flag,
        variant,
        raw: o,
      };
    });
  }
  // Fallback: derive display options from the raw ranked fix steps.
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
      rationale: null,
      mttrMinutes: null,
      toolCapability: isFlag ? 'feature_flags.set_variant' : null,
      recommended: i === 0,
      flag,
      variant: s.variant ?? 'off',
      raw: null,
    };
  });
}

// Raw outcome-store statuses from both the apply-fix seam
// (executed/denied/expired/blocked/unsupported/error) and the execute seam
// (executed/dry_run_ok/blocked/execution_failed/refused). ``pending`` while the
// approval is in flight; ``idle`` before the operator clicks.
type RawStatus = string;
type Phase = 'idle' | 'pending' | 'success' | 'denied' | 'expired' | 'blocked' | 'error';

function phaseOf(status: RawStatus): Phase {
  switch (status) {
    case 'idle':
      return 'idle';
    case 'pending':
      return 'pending';
    case 'executed':
    case 'dry_run_ok':
    case 'approved':
      return 'success';
    case 'denied':
      return 'denied';
    case 'expired':
      return 'expired';
    case 'blocked':
    case 'refused':
    case 'pending_approval':
      return 'blocked';
    default:
      // execution_failed | unsupported | error | anything unexpected
      return 'error';
  }
}

interface ApplyState {
  status: RawStatus;
  error: string | null;
  approver: string | null;
  approvalId: string | null;
  dryRun: boolean; // true when this option went through the (dry-run) execute seam
}

const IDLE: ApplyState = { status: 'idle', error: null, approver: null, approvalId: null, dryRun: false };

export function RcaView({ v, incidentId }: { v: RCAVerdict; incidentId: string | null }) {
  const options = optionsFromVerdict(v);
  const [applyById, setApplyById] = useState<Record<string, ApplyState>>({});

  // New verdict → clear all per-option apply state.
  useEffect(() => {
    setApplyById({});
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps

  const stateFor = (id: string): ApplyState => applyById[id] ?? IDLE;
  const patch = (id: string, next: Partial<ApplyState>) =>
    setApplyById((prev) => ({ ...prev, [id]: { ...(prev[id] ?? IDLE), ...next } }));

  // One poller for every option whose approval is in flight. Re-subscribes when
  // applyById changes; a resolved option drops out of the pending set.
  useEffect(() => {
    const pending = Object.entries(applyById).filter(
      ([, s]) => s.status === 'pending' && s.approvalId,
    );
    if (pending.length === 0) return;
    let alive = true;
    const timer = setInterval(() => {
      pending.forEach(async ([id, s]) => {
        if (!s.approvalId) return;
        try {
          const o = await api.hitlOutcome(s.approvalId);
          if (!alive || !o.status || o.status === 'pending') return;
          patch(id, { status: o.status, error: o.error ?? null, approver: o.approver ?? null });
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

  const applyOption = async (opt: DisplayOption) => {
    patch(opt.id, { status: 'pending', error: null, approver: null });
    try {
      if (opt.flag) {
        // Flag-flip → real, reversible remediation via the apply-fix seam, which
        // also fires the resolution verifier (2nd ticket-close HITL).
        const context: Record<string, unknown> = {
          service: v.affected_service,
          rca_verdict: v,
          timeout_seconds: 600,
        };
        if (incidentId) context.incident_id = incidentId;
        const res = await api.applyRcaFix(opt.flag, opt.variant, 'set_flag', undefined, context);
        patch(opt.id, { approvalId: res.approval_id, dryRun: false });
      } else if (opt.raw) {
        // Non-flag action (rollback / scale / restart / manual). No live executor
        // exists for these yet, so run the gate as a dry-run: the HITL approval
        // still happens and the verdict reports "would execute".
        const res = await api.executeOption(opt.raw, v.affected_service, {
          incidentId: incidentId ?? undefined,
          dryRun: true,
        });
        patch(opt.id, { approvalId: res.approval_id, dryRun: true });
      } else {
        // Advisory-only fallback step with no executor.
        patch(opt.id, { status: 'error', error: 'This step has no automated executor — perform it manually.' });
      }
    } catch (e) {
      patch(opt.id, { status: 'error', error: e instanceof Error ? e.message : String(e) });
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
            each is HITL-gated — approve &amp; apply the one you choose
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
                      <p className="text-sm font-medium leading-snug text-ink-900 dark:text-ink-50">
                        {opt.title}
                      </p>
                      {opt.recommended && (
                        <span className="chip !border-accent/40 !text-accent">
                          <Star className="mr-1 inline h-3 w-3" /> recommended
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[12px] leading-snug text-ink-600 dark:text-ink-300">
                      {opt.description}
                    </p>

                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <span className={clsx('chip', BLAST_RADIUS_STYLE[opt.blast_radius])}>
                        blast: {opt.blast_radius}
                      </span>
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

                    {/* Per-option approve & apply control */}
                    {executable ? (
                      <OptionApply
                        phase={phase}
                        dryRun={st.dryRun}
                        error={st.error}
                        approver={st.approver}
                        flag={opt.flag}
                        variant={opt.variant}
                        closeFollows={!!incidentId && !!opt.flag}
                        onApply={() => applyOption(opt)}
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
            <li key={i} className="leading-relaxed">
              {i + 1}. {line}
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

function OptionApply({
  phase, dryRun, error, approver, flag, variant, closeFollows, onApply,
}: {
  phase: Phase;
  dryRun: boolean;
  error: string | null;
  approver: string | null;
  flag: string | null;
  variant: string;
  closeFollows: boolean;
  onApply: () => void;
}) {
  const by = approver ? ` by ${approver}` : '';
  const busy = phase === 'pending';
  const done = phase === 'success';
  const label = busy ? 'Awaiting approval…' : done ? 'Applied' : phase === 'idle' ? 'Approve & apply' : 'Retry';

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
          {busy ? (
            <RefreshCw className="h-3.5 w-3.5 animate-spin" />
          ) : done ? (
            <CheckCircle2 className="h-3.5 w-3.5" />
          ) : (
            <ShieldAlert className="h-3.5 w-3.5" />
          )}
          {label}
        </button>
      </div>

      {phase === 'idle' && (
        <p className="mt-2 text-[11px] text-ink-500 dark:text-ink-400">
          HITL-gated: clicking requests approval — a human then approves it in the{' '}
          <span className="font-medium text-ink-700 dark:text-ink-200">Approvals</span> console (or Slack).
          {flag ? ' The flag flips only after that.' : ' Nothing runs until then.'}
        </p>
      )}
      {phase === 'pending' && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] text-ink-500 dark:text-ink-400">
            Approval requested — grant it to apply. Nothing has changed yet.
          </p>
          <Link
            to="/console/approvals"
            className="inline-flex items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] font-medium text-accent transition hover:bg-accent/20"
          >
            <Gavel className="h-3.5 w-3.5" /> Open Approvals console to approve
          </Link>
        </div>
      )}
      {phase === 'success' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-ok">
          <CheckCircle2 className="h-3 w-3" /> Approved{by} —{' '}
          {flag
            ? `${flag} set to ${variant}. ${closeFollows ? 'Verifying recovery — a ticket-close approval will appear shortly.' : 'The service should recover shortly.'}`
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
      {(phase === 'error' || phase === 'blocked') && (
        <p className="mt-2 text-[11px] text-bad">{error || 'Could not apply this option.'}</p>
      )}
    </div>
  );
}
