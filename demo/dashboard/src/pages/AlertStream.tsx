import { useEffect, useMemo, useState } from 'react';
import {
  Search,
  Filter,
  RefreshCw,
  Inbox,
  Sparkles,
  ShieldAlert,
  CheckCircle2,
  XCircle,
} from 'lucide-react';
import { useAlertsSocket } from '@/lib/ws';
import { SeverityBadge } from '@/components/SeverityBadge';
import { EmptyState } from '@/components/states';
import { api } from '@/lib/api';
import type { Severity, PrometheusAlert, TriageVerdict, RCAVerdict, BlastRadius } from '@/types/api';
import { timeAgo, clsx } from '@/lib/format';

function inferSeverity(hint: string | null | undefined): Severity {
  const s = (hint || '').toLowerCase();
  if (s.includes('critical') || s === 'p1') return 'Sev-1';
  if (s.includes('high')     || s === 'p2') return 'Sev-2';
  if (s.includes('warning')  || s === 'p3') return 'Sev-3';
  return 'Sev-4';
}

type SortKey = 'severity' | 'service' | 'time';

const SEV_ORDER: Record<Severity, number> = { 'Sev-1': 0, 'Sev-2': 1, 'Sev-3': 2, 'Sev-4': 3 };

export default function AlertStream() {
  const { alerts, status, lastUpdate } = useAlertsSocket();
  const [q, setQ] = useState('');
  const [sevFilter, setSevFilter] = useState<Severity | 'all'>('all');
  const [sortKey, setSortKey] = useState<SortKey>('severity');
  const [picked, setPicked] = useState<PrometheusAlert | null>(null);
  const [verdict, setVerdict] = useState<TriageVerdict | null>(null);
  const [triageBusy, setTriageBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // RCA state lives alongside the triage verdict — generated on demand, cleared
  // when a new alert is picked so we never show an RCA for a stale verdict.
  const [rca, setRca] = useState<RCAVerdict | null>(null);
  const [rcaBusy, setRcaBusy] = useState(false);
  const [rcaError, setRcaError] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const lc = q.toLowerCase();
    let out = alerts.filter((a) => {
      const sev = inferSeverity(a.severity_hint);
      if (sevFilter !== 'all' && sev !== sevFilter) return false;
      if (!lc) return true;
      return (
        a.service.toLowerCase().includes(lc) ||
        a.metric.toLowerCase().includes(lc) ||
        a.alert_id.toLowerCase().includes(lc)
      );
    });
    out = [...out].sort((a, b) => {
      if (sortKey === 'severity') return SEV_ORDER[inferSeverity(a.severity_hint)] - SEV_ORDER[inferSeverity(b.severity_hint)];
      if (sortKey === 'service')  return a.service.localeCompare(b.service);
      return Date.parse(b.timestamp) - Date.parse(a.timestamp);
    });
    return out;
  }, [alerts, q, sevFilter, sortKey]);

  const runTriage = async (alert: PrometheusAlert) => {
    setPicked(alert);
    setVerdict(null);
    setError(null);
    setRca(null);
    setRcaError(null);
    setTriageBusy(true);
    try {
      const result = await api.triage(alert);
      setVerdict(result.verdict);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTriageBusy(false);
    }
  };

  const runRca = async () => {
    if (!verdict) return;
    setRca(null);
    setRcaError(null);
    setRcaBusy(true);
    try {
      const result = await api.rca(verdict);
      setRca(result);
    } catch (e) {
      setRcaError(e instanceof Error ? e.message : String(e));
    } finally {
      setRcaBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            Real-time alert stream
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            Pushed from Prometheus over WebSocket · {alerts.length} firing · stream {status}
          </p>
        </div>
        {lastUpdate && <span className="chip">updated {timeAgo(lastUpdate)}</span>}
      </div>

      {/* Toolbar */}
      <div className="card">
        <div className="card-body flex flex-wrap items-center gap-3">
          <div className="relative min-w-[240px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search by service, metric, alert ID…"
              className="input pl-9"
            />
          </div>
          <div className="flex items-center gap-2">
            <Filter className="h-4 w-4 text-ink-400" />
            <select value={sevFilter} onChange={(e) => setSevFilter(e.target.value as Severity | 'all')} className="input !w-auto !py-1">
              <option value="all">All severities</option>
              <option value="Sev-1">Sev-1</option>
              <option value="Sev-2">Sev-2</option>
              <option value="Sev-3">Sev-3</option>
              <option value="Sev-4">Sev-4</option>
            </select>
            <select value={sortKey} onChange={(e) => setSortKey(e.target.value as SortKey)} className="input !w-auto !py-1">
              <option value="severity">Sort: Severity</option>
              <option value="time">Sort: Newest</option>
              <option value="service">Sort: Service</option>
            </select>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {/* List */}
        <div className="lg:col-span-3">
          {filtered.length === 0 ? (
            <div className="card">
              <EmptyState
                label="No alerts match"
                hint={alerts.length === 0 ? 'No alerts are firing. Inject a scenario from the Overview page.' : 'Try a wider filter.'}
                icon={<Inbox className="h-7 w-7" />}
              />
            </div>
          ) : (
            <ul className="space-y-2">
              {filtered.map((a) => {
                const sev = inferSeverity(a.severity_hint);
                const isPicked = picked?.alert_id === a.alert_id;
                return (
                  <li
                    key={a.alert_id}
                    onClick={() => runTriage(a)}
                    className={clsx(
                      'card cursor-pointer transition-all hover:border-accent',
                      isPicked && '!border-accent ring-1 ring-accent/30',
                    )}
                  >
                    <div className="card-body !py-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <SeverityBadge severity={sev} />
                            <span className="truncate font-mono text-xs text-ink-500 dark:text-ink-400">
                              {a.metric}
                            </span>
                          </div>
                          <h3 className="mt-1.5 truncate text-sm font-semibold text-ink-900 dark:text-ink-50">
                            {a.service}
                          </h3>
                          <p className="mt-0.5 truncate text-xs text-ink-500 dark:text-ink-400">
                            {a.annotations.summary || a.annotations.description || a.alert_id}
                          </p>
                        </div>
                        <div className="flex-shrink-0 text-right font-mono text-[11px] text-ink-500 dark:text-ink-400">
                          {timeAgo(a.timestamp)}
                        </div>
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Triage panel */}
        <aside className="lg:col-span-2">
          <div className="card sticky top-20">
            <div className="card-header">
              <h2 className="card-title">Triage verdict</h2>
              {triageBusy && <RefreshCw className="h-4 w-4 animate-spin text-accent" />}
            </div>
            <div className="card-body">
              {!picked && (
                <EmptyState label="Select an alert" hint="Click a row on the left to run RA-001." />
              )}
              {picked && triageBusy && (
                <div className="space-y-2 animate-pulse">
                  <div className="h-4 w-2/3 rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-3 w-full rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
                  <div className="h-3 w-3/4 rounded bg-ink-200 dark:bg-ink-700" />
                </div>
              )}
              {error && <p className="text-sm text-bad">{error}</p>}
              {verdict && !triageBusy && (
                <>
                  <VerdictView v={verdict} />
                  <div className="!mt-4 border-t border-ink-200 pt-3 dark:border-ink-700">
                    <div className="flex items-center justify-between gap-3">
                      <p className="card-title !text-[10px]">RCA Agent · PRS-008 ★</p>
                      <button
                        type="button"
                        onClick={runRca}
                        disabled={rcaBusy}
                        className={clsx(
                          'inline-flex items-center gap-1.5 rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1',
                          'text-xs font-medium text-accent transition hover:bg-accent/20',
                          'disabled:cursor-not-allowed disabled:opacity-50',
                        )}
                      >
                        {rcaBusy ? (
                          <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Sparkles className="h-3.5 w-3.5" />
                        )}
                        {rca ? 'Re-generate RCA' : 'Generate RCA'}
                      </button>
                    </div>
                    {rcaBusy && !rca && (
                      <div className="mt-3 space-y-2 animate-pulse">
                        <div className="h-3 w-3/4 rounded bg-ink-200 dark:bg-ink-700" />
                        <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
                        <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                        <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                      </div>
                    )}
                    {rcaError && <p className="mt-2 text-sm text-bad">{rcaError}</p>}
                    {rca && !rcaBusy && <RcaView v={rca} />}
                  </div>
                </>
              )}
            </div>
          </div>
        </aside>
      </div>
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
// "Approve & apply" action — everything else stays advisory-only (we don't
// fake an executor we haven't built; see aiops/tools/rca_remediation.py).
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
  | 'idle'
  | 'pending'
  | 'executed'
  | 'denied'
  | 'expired'
  | 'blocked'
  | 'unsupported'
  | 'error';

function RemediationBox({
  flag,
  status,
  error,
  approver,
  onApply,
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
  const label =
    busy ? 'Awaiting approval…' : done ? 'Applied' : status === 'idle' ? 'Approve & apply' : 'Retry';

  return (
    <div className="rounded-md border border-accent/40 bg-accent/5 p-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="card-title !text-[10px]">Apply remediation</p>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Set flag <span className="font-mono text-ink-700 dark:text-ink-200">{flag}</span> →{' '}
            <span className="font-mono text-ink-700 dark:text-ink-200">off</span> · requires HITL
            approval
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

      {status === 'pending' && (
        <p className="mt-2 text-[11px] text-ink-500 dark:text-ink-400">
          Approval requested — approve or deny in Slack or on the Notifications page. The flag is
          unchanged until then.
        </p>
      )}
      {status === 'executed' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-ok">
          <CheckCircle2 className="h-3 w-3" /> Approved{by} — {flag} set to off. The service should
          recover shortly.
        </p>
      )}
      {status === 'denied' && (
        <p className="mt-2 flex items-center gap-1 text-[11px] text-bad">
          <XCircle className="h-3 w-3" /> Denied{by} — flag left unchanged.
        </p>
      )}
      {status === 'expired' && (
        <p className="mt-2 text-[11px] text-warn">Approval expired — no change made.</p>
      )}
      {(status === 'error' || status === 'blocked' || status === 'unsupported') && (
        <p className="mt-2 text-[11px] text-bad">{error || 'Could not apply the fix.'}</p>
      )}
    </div>
  );
}

function RcaView({ v }: { v: RCAVerdict }) {
  // Follow the executable action the RCA agent annotated on a fix step. Fall
  // back to the legacy service→flag map only for verdicts that predate
  // step-level action annotation (keeps old cached verdicts working).
  const fixStep = v.ranked_fix_steps.find((s) => s.action_type === 'set_flag' && s.flag);
  const flag = fixStep?.flag ?? flagForService(v.affected_service);
  const fixVariant = fixStep?.variant ?? 'off';
  const [applyStatus, setApplyStatus] = useState<ApplyStatus>('idle');
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyApprover, setApplyApprover] = useState<string | null>(null);
  const [approvalId, setApprovalId] = useState<string | null>(null);

  // Reset the apply state whenever a fresh RCA verdict is shown so we never
  // carry a stale "Applied" from a previous alert.
  useEffect(() => {
    setApplyStatus('idle');
    setApplyError(null);
    setApplyApprover(null);
    setApprovalId(null);
  }, [v]);

  // Poll the HITL outcome while an approval is in flight.
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
    <div className="mt-3 space-y-3 text-sm">
      <div>
        <div className="flex items-baseline justify-between gap-2">
          <p className="card-title !text-[10px]">Root cause</p>
          <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">
            confidence {(v.confidence_score * 100).toFixed(0)}%
          </span>
        </div>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-900 dark:text-ink-50">
          {v.root_cause}
        </p>
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
        <p className="card-title !text-[10px]">
          Ranked fix steps ({v.ranked_fix_steps.length})
        </p>
        <ol className="mt-2 space-y-2">
          {v.ranked_fix_steps.map((step, i) => (
            <li
              key={i}
              className="rounded-md border border-ink-200 bg-ink-50/50 p-2.5 dark:border-ink-700 dark:bg-ink-800/30"
            >
              <div className="flex items-start gap-2">
                <span className="flex-shrink-0 rounded bg-ink-200 px-1.5 text-[10px] font-bold text-ink-700 dark:bg-ink-700 dark:text-ink-200">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm leading-snug text-ink-900 dark:text-ink-50">
                    {step.description}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <span className={clsx('chip', BLAST_RADIUS_STYLE[step.blast_radius])}>
                      blast: {step.blast_radius}
                    </span>
                    <span className="chip !border-accent/40 !text-accent">
                      <ShieldAlert className="mr-1 inline h-3 w-3" />
                      HITL required
                    </span>
                    {step.action_type === 'set_flag' && step.flag ? (
                      <span className="chip !border-ok/40 !text-ok" title="One-click remediable">
                        <CheckCircle2 className="mr-1 inline h-3 w-3" />
                        auto: set {step.flag}→{step.variant}
                      </span>
                    ) : (
                      <span
                        className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400"
                        title="No automated executor — perform manually"
                      >
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
            <li key={i} className="leading-relaxed">
              {i + 1}. {line}
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

function VerdictView({ v }: { v: TriageVerdict }) {
  return (
    <dl className="space-y-2.5 text-sm">
      <Row k="Severity">
        <SeverityBadge severity={v.severity} />
        <span className="ml-2 font-mono text-xs text-ink-500 dark:text-ink-400">
          confidence {(v.confidence_score * 100).toFixed(0)}%
        </span>
      </Row>
      <Row k="Service"><span className="font-mono text-ink-900 dark:text-ink-50">{v.affected_service}</span></Row>
      <Row k="Status">
        <span className={clsx(
          'chip',
          v.status === 'Active' ? '!border-ok/40 !text-ok' : '!border-warn/40 !text-warn',
        )}>{v.status}</span>
      </Row>
      <Row k="Assigned"><span className="text-ink-900 dark:text-ink-50">{v.assigned_team}</span></Row>
      {v.assigned_engineer && (
        <Row k="On-call"><span className="font-mono text-ink-700 dark:text-ink-200">{v.assigned_engineer}</span></Row>
      )}
      {v.recommended_runbook && (
        <Row k="Runbook">
          <a href={v.recommended_runbook} target="_blank" rel="noreferrer" className="break-all font-mono text-xs text-accent hover:underline">
            {v.recommended_runbook}
          </a>
        </Row>
      )}
      <Row k="Dup count"><span className="font-mono">{v.duplicate_alert_count}</span></Row>
      <div className="!mt-3 border-t border-ink-200 pt-3 dark:border-ink-700">
        <p className="card-title mb-2 text-[10px]">AI summary</p>
        <p className="text-sm leading-relaxed text-ink-700 dark:text-ink-200">{v.alert_summary}</p>
      </div>
      <details className="!mt-3">
        <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
          Decision trace ({v.audit_metadata.decision_trace.length} steps)
        </summary>
        <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
          {v.audit_metadata.decision_trace.map((line, i) => (
            <li key={i} className="leading-relaxed">{i + 1}. {line}</li>
          ))}
        </ol>
      </details>
    </dl>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-20 flex-shrink-0 text-xs uppercase tracking-wider text-ink-500 dark:text-ink-400">{k}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  );
}
