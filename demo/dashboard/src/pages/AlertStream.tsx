import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, Filter, RefreshCw, Inbox, Sparkles, ChevronRight } from 'lucide-react';
import { useAlertsSocket } from '@/lib/ws';
import { SeverityBadge } from '@/components/SeverityBadge';
import { EmptyState } from '@/components/states';
import { api } from '@/lib/api';
import { setConsoleAgent } from '@/lib/consoleScope';
import type { Severity, PrometheusAlert, TriageVerdict } from '@/types/api';
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
  const navigate = useNavigate();
  const { alerts, status, lastUpdate } = useAlertsSocket();
  const [q, setQ] = useState('');
  const [sevFilter, setSevFilter] = useState<Severity | 'all'>('all');
  const [sortKey, setSortKey] = useState<SortKey>('severity');
  const [picked, setPicked] = useState<PrometheusAlert | null>(null);
  const [verdict, setVerdict] = useState<TriageVerdict | null>(null);
  const [triageBusy, setTriageBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  // Hand the triaged incident off to the RCA Agent: scope the console to RCA,
  // then open its surface with the affected service so it can preselect this
  // incident and run root-cause analysis. Alert Triage doesn't do RCA itself.
  const generateRca = () => {
    if (!verdict) return;
    setConsoleAgent('rca-agent');
    navigate('/console/rca', { state: { service: verdict.affected_service } });
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
                  <button
                    type="button"
                    onClick={generateRca}
                    className="btn btn-primary mt-4 w-full justify-center"
                    title="Hand this incident to the RCA Agent for root-cause analysis"
                  >
                    <Sparkles className="h-4 w-4" />
                    Generate RCA
                    <ChevronRight className="h-4 w-4" />
                  </button>
                  <p className="mt-1.5 text-center text-[11px] text-ink-500 dark:text-ink-400">
                    Opens the RCA Agent console for {verdict.affected_service}.
                  </p>
                </>
              )}
            </div>
          </div>
        </aside>
      </div>
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
      <Row k="Service">
        <span className="font-mono text-ink-900 dark:text-ink-50">{v.affected_service}</span>
        {v.customer_facing != null && (
          <span className={clsx(
            'chip ml-2',
            v.customer_facing ? '!border-bad/40 !text-bad' : '!border-ink-300/40 !text-ink-500',
          )}>
            {v.customer_facing ? 'Customer-facing' : 'Internal'}
          </span>
        )}
      </Row>
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
          <a
            href={`/api/runbooks/by-service/${encodeURIComponent(v.affected_service)}`}
            target="_blank"
            rel="noreferrer"
            className="break-all font-mono text-xs text-accent hover:underline"
          >
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
