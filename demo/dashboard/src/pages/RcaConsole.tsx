import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  Sparkles, RefreshCw, ShieldAlert, CheckCircle2, XCircle, Inbox, Brain,
} from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, LoadingState, ErrorState } from '@/components/states';
import { SeverityBadge, StatusChip } from '@/components/SeverityBadge';
import type { TriageVerdict, RCAVerdict, BlastRadius } from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';

// ─── RCA Agent console (PRS-008 ★) ──────────────────────────────────────────
//
// This is the RCA Agent's OWN surface — independent of Alert Triage. It takes
// the triage verdicts Alert Triage produced, lets the operator pick one, and
// runs root-cause analysis → ranked fix steps (each with a tested rollback) →
// a REQUIRED-HITL "approve & apply" gate. Alert Triage only generates the
// verdict; everything below the verdict belongs here.

export default function RcaConsole() {
  const verdicts = useFetch(api.triageLive, { intervalMs: 0 });
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [rca, setRca] = useState<RCAVerdict | null>(null);
  const [rcaBusy, setRcaBusy] = useState(false);
  const [rcaError, setRcaError] = useState<string | null>(null);

  // Service handed off from Alert Triage's "Generate RCA" button (router state).
  const location = useLocation();
  const wantedService = (location.state as { service?: string } | null)?.service;
  const handedOff = useRef(false);
  const handoffAttempts = useRef(0);

  const results = verdicts.data?.results ?? [];
  const list: TriageVerdict[] = results.map((r) => r.verdict);
  const selected: TriageVerdict | null = list[selectedIdx] ?? null;

  // Clear any RCA when the selected verdict changes so we never show a stale
  // analysis for a different incident.
  useEffect(() => {
    setRca(null);
    setRcaError(null);
  }, [selectedIdx]);

  const runRca = async (target?: TriageVerdict) => {
    const v = target ?? selected;
    if (!v) return;
    setRca(null);
    setRcaError(null);
    setRcaBusy(true);
    try {
      setRca(await api.rca(v));
    } catch (e) {
      setRcaError(e instanceof Error ? e.message : String(e));
    } finally {
      setRcaBusy(false);
    }
  };

  // When arriving from Alert Triage, preselect the matching incident and run
  // RCA automatically — one click on "Generate RCA" lands you on the result.
  useEffect(() => {
    if (handedOff.current || !wantedService) return;
    const idx = list.findIndex((v) => v.affected_service === wantedService);
    if (idx >= 0) {
      handedOff.current = true;
      setSelectedIdx(idx);
      runRca(list[idx]);
      return;
    }
    // The just-triaged verdict may not be in this snapshot yet (intervalMs: 0,
    // one-shot). Retry a few times so the hand-off from "Generate RCA" is
    // deterministic instead of a silent miss when findIndex returns -1.
    if (handoffAttempts.current < 5) {
      const t = setTimeout(() => {
        handoffAttempts.current += 1;
        verdicts.refetch();
      }, 1200);
      return () => clearTimeout(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verdicts.data, wantedService]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <Brain className="h-6 w-6 text-accent" /> RCA Agent
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            PRS-008 ★ · root-cause analysis → executable fix steps with rollback, gated by human approval.
          </p>
        </div>
        <button onClick={verdicts.refetch} className="btn">
          <RefreshCw className={clsx('h-4 w-4', verdicts.loading && 'animate-spin')} /> Refresh verdicts
        </button>
      </div>

      {verdicts.loading && !verdicts.data ? (
        <div className="card"><LoadingState label="Loading triaged incidents…" /></div>
      ) : verdicts.error ? (
        <div className="card"><ErrorState error={verdicts.error} /></div>
      ) : list.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No triaged incidents yet"
            hint="Inject a scenario on the Overview page — once Alert Triage writes a verdict, it lands here for root-cause analysis."
            icon={<Inbox className="h-7 w-7" />}
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          {/* Incident picker */}
          <div className="lg:col-span-2">
            <ul className="space-y-2">
              {list.map((v, i) => (
                <li key={i}>
                  <button
                    onClick={() => setSelectedIdx(i)}
                    className={clsx(
                      'card w-full text-left transition-all hover:border-accent',
                      i === selectedIdx && '!border-accent ring-1 ring-accent/30',
                    )}
                  >
                    <div className="card-body !py-3">
                      <div className="flex items-center gap-2">
                        <SeverityBadge severity={v.severity} />
                        <StatusChip status={v.status} />
                      </div>
                      <h3 className="mt-1.5 truncate text-sm font-semibold text-ink-900 dark:text-ink-50">
                        {v.affected_service}
                      </h3>
                      <p className="mt-0.5 truncate text-xs text-ink-500 dark:text-ink-400">
                        {v.alert_summary}
                      </p>
                      <p className="mt-0.5 font-mono text-[11px] text-ink-400 dark:text-ink-500">
                        {timeAgo(v.audit_metadata.created_at)}
                      </p>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {/* RCA panel */}
          <aside className="lg:col-span-3">
            <div className="card sticky top-20">
              <div className="card-header">
                <h2 className="card-title">Root-cause analysis</h2>
                <button
                  type="button"
                  onClick={() => runRca()}
                  disabled={rcaBusy || !selected}
                  className="btn btn-primary !py-1 !text-xs"
                >
                  {rcaBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  {rca ? 'Re-generate RCA' : 'Generate RCA'}
                </button>
              </div>
              <div className="card-body">
                {!rca && !rcaBusy && !rcaError && (
                  <EmptyState
                    label={selected ? `Analyse ${selected.affected_service}` : 'Select an incident'}
                    hint="Generate RCA to produce the root cause, ranked fix steps, and an approvable remediation."
                  />
                )}
                {rcaBusy && (
                  <div className="space-y-2 animate-pulse">
                    <div className="h-3 w-3/4 rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                  </div>
                )}
                {rcaError && <p className="text-sm text-bad">{rcaError}</p>}
                {rca && !rcaBusy && <RcaView v={rca} />}
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

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

function RcaView({ v }: { v: RCAVerdict }) {
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
