import { useMemo, useState } from 'react';
import {
  BellRing, ShieldAlert, Activity, Zap, PlayCircle, RotateCcw,
  AlertOctagon, Timer, Gauge, Server,
} from 'lucide-react';
import {
  AreaChart, Area, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid,
  PieChart, Pie, Cell, Legend,
} from 'recharts';
import StatCard from '@/components/StatCard';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { useAlertsSocket } from '@/lib/ws';
import { useFetch, clearFetchCache } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import type { Severity } from '@/types/api';
import { timeAgo, clsx } from '@/lib/format';

const SEV_COLOR: Record<Severity, string> = {
  'Sev-1': '#ef4444', 'Sev-2': '#f59e0b', 'Sev-3': '#eab308', 'Sev-4': '#3b82f6',
};

// Palette used to assign a stable colour to each Prometheus alert rule
// shown in the Overview scenario cards. Hoisted to module scope so the
// array isn't re-allocated on every render.
const ALERT_PALETTE = ['#7c3aed', '#06b6d4', '#ef4444', '#f59e0b', '#10b981', '#a78bfa', '#ec4899'];

// Map a Prometheus alert's severity_hint label to a Sev-N bucket for chart slicing.
function inferSeverity(hint: string | null | undefined): Severity {
  const s = (hint || '').toLowerCase();
  if (s.includes('critical') || s === 'p1') return 'Sev-1';
  if (s.includes('high')     || s === 'p2') return 'Sev-2';
  if (s.includes('warning')  || s === 'p3') return 'Sev-3';
  return 'Sev-4';
}

export default function Overview() {
  const { alerts, status, lastUpdate } = useAlertsSocket();
  const scenarios = useFetch(api.scenarios, { intervalMs: 8_000, cacheKey: 'scenarios' });
  const [busy, setBusy] = useState<string | null>(null);
  const [history, setHistory] = useState<{ t: number; count: number }[]>([]);

  // Maintain a rolling 30-point history of alert counts for the trend chart.
  // Updated whenever the WebSocket pushes a new frame.
  useMemo(() => {
    if (!lastUpdate) return;
    setHistory((h) => {
      const next = [...h, { t: Date.parse(lastUpdate), count: alerts.length }];
      return next.slice(-30);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastUpdate]);

  const sevCounts = useMemo(() => {
    const c: Record<Severity, number> = { 'Sev-1': 0, 'Sev-2': 0, 'Sev-3': 0, 'Sev-4': 0 };
    for (const a of alerts) c[inferSeverity(a.severity_hint)]++;
    return c;
  }, [alerts]);

  // Use `metric` as the canonical alert-rule name coming from Prometheus' API
  // when available. Fall back to `alert_id` for older/alternate envelopes.
  const firingAlertNames = useMemo(() => new Set(alerts.map((a) => a.metric || a.alert_id || '')), [alerts]);
  const alertToColor = useMemo(() => {
    const uniq = Array.from(new Set((scenarios.data?.scenarios ?? []).map((s) => s.alert))).filter(Boolean);
    const m: Record<string, string> = {};
    uniq.forEach((name, i) => { m[name] = ALERT_PALETTE[i % ALERT_PALETTE.length]; });
    return m;
  }, [scenarios]);

  const pieData = useMemo(() => (
    (Object.entries(sevCounts) as [Severity, number][])
      .filter(([, n]) => n > 0)
      .map(([k, v]) => ({ name: k, value: v }))
  ), [sevCounts]);

  const trendData = useMemo(
    () => history.map((p, i) => ({ i, time: new Date(p.t).toLocaleTimeString(), count: p.count })),
    [history],
  );

  const inject = async (id: string) => {
    setBusy(id);
    try { await api.injectScenario(id); await scenarios.refetch(); }
    catch { /* surfaced via UI elsewhere */ }
    finally { setBusy(null); }
  };
  const reset = async (id: string) => {
    setBusy(id);
    try { await api.resetScenario(id); clearFetchCache(); await scenarios.refetch(); }
    finally { setBusy(null); }
  };
  const resetAll = async () => {
    setBusy('__all__');
    try { await api.resetAllScenarios(); clearFetchCache(); await scenarios.refetch(); }
    finally { setBusy(null); }
  };

  return (
    <div className="space-y-6">
      <PageHeader status={status} lastUpdate={lastUpdate} />

      {/* Stat row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Firing alerts"
          value={alerts.length}
          icon={<BellRing className="h-4 w-4" />}
          intent={alerts.length > 0 ? 'bad' : 'ok'}
          hint={alerts.length === 0 ? 'No firing alerts' : 'Prometheus rules currently firing'}
          tooltip="Number of Prometheus rules currently in firing state. Each rule can be triggered by one or more injected scenarios."
        />
        <StatCard
          label="Sev-1 / Sev-2"
          value={`${sevCounts['Sev-1']} / ${sevCounts['Sev-2']}`}
          icon={<ShieldAlert className="h-4 w-4" />}
          intent={sevCounts['Sev-1'] > 0 ? 'bad' : sevCounts['Sev-2'] > 0 ? 'warn' : 'ok'}
          hint="Critical + high severity"
        />
        <StatCard
          label="Live stream"
          value={status === 'open' ? 'connected' : status}
          icon={<Activity className="h-4 w-4" />}
          intent={status === 'open' ? 'ok' : status === 'connecting' ? 'warn' : 'bad'}
          hint={lastUpdate ? `last frame ${timeAgo(lastUpdate)}` : 'awaiting first frame'}
        />
        <StatCard
          label="Active scenarios"
          value={(scenarios.data?.scenarios.filter((s) => s.current_variant !== 'off') ?? []).length}
          icon={<Zap className="h-4 w-4" />}
          hint={`${scenarios.data?.scenarios.length ?? 0} configured`}
        />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2">
          <div className="card-header">
            <h2 className="card-title">Alert count · last 30 frames</h2>
            <span className="chip">{trendData.length} samples</span>
          </div>
          <div className="card-body h-64">
            {trendData.length < 2 ? (
              <EmptyState label="Building trend…" hint="Alert counts will plot here as the stream pushes frames." />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={trendData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#4f8cff" stopOpacity={0.6} />
                      <stop offset="95%" stopColor="#4f8cff" stopOpacity={0}   />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#41475533" />
                  <XAxis dataKey="time" tick={{ fontSize: 11, fill: '#8b95a8' }} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: '#8b95a8' }} />
                  <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                  <Area type="monotone" dataKey="count" stroke="#4f8cff" fill="url(#g1)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h2 className="card-title">Severity mix</h2>
          </div>
          <div className="card-body h-64">
            {pieData.length === 0 ? (
              <EmptyState label="No alerts" hint="Inject a scenario to fire one." />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={pieData} dataKey="value" nameKey="name" outerRadius={70} innerRadius={40}>
                    {pieData.map((d) => (
                      <Cell key={d.name} fill={SEV_COLOR[d.name as Severity]} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={{ borderRadius: 8, fontSize: 12 }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>

      {/* Failure injection */}
      <FailureInjection
        scenarios={scenarios.data?.scenarios ?? []}
        loading={scenarios.loading && !scenarios.data}
        error={scenarios.error}
        busy={busy}
        onInject={inject}
        onReset={reset}
        onResetAll={resetAll}
        firingAlertNames={firingAlertNames}
        alertToColor={alertToColor}
      />
    </div>
  );
}

// ─── Failure injection — grouped by category ────────────────────────────────

interface InjectionProps {
  scenarios: import('@/types/api').Scenario[];
  loading: boolean;
  error: string | null;
  busy: string | null;
  onInject: (id: string) => void;
  onReset: (id: string) => void;
  onResetAll: () => void;
}

const CATEGORY_META: Record<string, { label: string; icon: JSX.Element; color: string }> = {
  errors:   { label: 'HTTP errors (5xx)',   icon: <AlertOctagon className="h-4 w-4" />, color: '#ef4444' },
  latency:  { label: 'Latency & slowdowns', icon: <Timer       className="h-4 w-4" />, color: '#f59e0b' },
  capacity: { label: 'Capacity & queue',    icon: <Gauge       className="h-4 w-4" />, color: '#3b82f6' },
  infra:    { label: 'Infra & saturation',  icon: <Server      className="h-4 w-4" />, color: '#8b95a8' },
  other:    { label: 'Other',               icon: <Zap         className="h-4 w-4" />, color: '#8b95a8' },
};

function FailureInjection({
  scenarios, loading, error, busy, onInject, onReset, onResetAll, firingAlertNames, alertToColor,
}: InjectionProps & { firingAlertNames: Set<string>; alertToColor: Record<string, string> }) {
  const grouped = useMemo(() => {
    const buckets: Record<string, import('@/types/api').Scenario[]> = {};
    for (const s of scenarios) {
      const cat = s.category ?? 'other';
      (buckets[cat] ||= []).push(s);
    }
    // Stable order: errors → latency → capacity → infra → other
    return ['errors', 'latency', 'capacity', 'infra', 'other']
      .filter((k) => buckets[k]?.length)
      .map((k) => ({ key: k, items: buckets[k] }));
  }, [scenarios]);

  const activeCount = scenarios.filter((s) => s.current_variant !== 'off').length;

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2 className="card-title">Failure injection · OpenTelemetry demo flags</h2>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Flip a flagd flag → wait 60–180 s → matching Prometheus alert fires → triage runs.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {activeCount > 0 && (
            <span className="chip !border-bad/40 !text-bad">
              <span className="h-1.5 w-1.5 rounded-full bg-bad animate-pulse-slow" />
              {activeCount} active
            </span>
          )}
          <button
            onClick={onResetAll}
            disabled={busy !== null || activeCount === 0}
            className="btn !py-1 !text-xs"
            title="Set every scenario flag back to 'off' in one atomic patch"
          >
            <RotateCcw className={clsx('h-3.5 w-3.5', busy === '__all__' && 'animate-spin')} />
            Reset all
          </button>
        </div>
      </div>
      <div className="card-body space-y-5">
        {loading ? (
          <LoadingState />
        ) : error ? (
          <ErrorState error={error} />
        ) : grouped.length === 0 ? (
          <EmptyState label="No scenarios configured" />
        ) : (
          grouped.map(({ key, items }) => {
            const meta = CATEGORY_META[key] ?? CATEGORY_META.other;
            const activeInCat = items.filter((s) => s.current_variant !== 'off').length;
            return (
              <section key={key}>
                <div className="mb-3 flex items-center gap-2.5">
                  <span
                    className="flex h-8 w-8 flex-none items-center justify-center rounded-lg"
                    style={{ background: `${meta.color}1f`, color: meta.color }}
                  >
                    {meta.icon}
                  </span>
                  <h3 className="text-sm font-semibold text-ink-900 dark:text-ink-50">{meta.label}</h3>
                  <span className="rounded-full bg-ink-100 px-2 py-0.5 text-[11px] font-medium text-ink-500 dark:bg-ink-800 dark:text-ink-400">
                    {items.length}
                  </span>
                  {activeInCat > 0 && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-bad/15 px-2 py-0.5 text-[11px] font-semibold text-bad">
                      <span className="h-1.5 w-1.5 rounded-full bg-bad animate-pulse-slow" />
                      {activeInCat} active
                    </span>
                  )}
                  <span className="ml-1 h-px flex-1 bg-ink-200 dark:bg-ink-800" />
                </div>
                <div
                  className="grid gap-4"
                  style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 280px), 1fr))' }}
                >
                  {items.map((s) => (
                    <ScenarioCard
                      key={s.scenario_id}
                      s={s}
                      busy={busy}
                      onInject={onInject}
                      onReset={onReset}
                      isFiring={firingAlertNames.has(s.alert)}
                      alertColor={alertToColor[s.alert]}
                    />
                  ))}
                </div>
              </section>
            );
          })
        )}
      </div>
    </div>
  );
}

type ScenarioState = 'off' | 'arming' | 'firing';

function StatusPill({ state }: { state: ScenarioState }) {
  if (state === 'firing') {
    return (
      <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-bad/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-bad">
        <span className="h-1.5 w-1.5 rounded-full bg-bad animate-pulse-slow" /> firing
      </span>
    );
  }
  if (state === 'arming') {
    return (
      <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-warn/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-warn">
        <span className="h-1.5 w-1.5 rounded-full bg-warn animate-pulse" /> arming
      </span>
    );
  }
  return (
    <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-ink-200/70 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-ink-500 dark:bg-ink-800 dark:text-ink-400">
      off
    </span>
  );
}

function ScenarioCard({
  s, busy, onInject, onReset, isFiring, alertColor,
}: {
  s: import('@/types/api').Scenario;
  busy: string | null;
  onInject: (id: string) => void;
  onReset: (id: string) => void;
  isFiring: boolean;
  alertColor?: string;
}) {
  const on = s.current_variant !== 'off';
  const isThisBusy = busy === s.scenario_id;
  const accent = alertColor ?? '#64748b';
  const state: ScenarioState = !on ? 'off' : isFiring ? 'firing' : 'arming';

  return (
    <div
      className={clsx(
        'group relative flex flex-col overflow-hidden rounded-xl border p-4 transition-all duration-200',
        'bg-ink-50/60 dark:bg-ink-900/40',
        on
          ? 'border-bad/50 ring-1 ring-bad/15'
          : 'border-ink-200 hover:-translate-y-0.5 hover:border-ink-300 hover:shadow-md dark:border-ink-700 dark:hover:border-ink-600',
      )}
    >
      {/* Alert-rule accent, fused to the top edge of the card (no detached bar). */}
      <span aria-hidden className="absolute inset-x-0 top-0 h-[3px]" style={{ background: accent }} />

      {/* Ambient glow while active — amber arming, red firing. */}
      {on && (
        <div
          aria-hidden
          className="pointer-events-none absolute -right-10 -top-10 h-28 w-28 rounded-full opacity-20 blur-2xl"
          style={{ background: isFiring ? '#ef4444' : '#f59e0b' }}
        />
      )}

      <div className="relative flex flex-1 flex-col">
        <div className="flex items-start justify-between gap-2">
          <h3 className="truncate text-sm font-semibold text-ink-900 dark:text-ink-50">{s.title}</h3>
          <StatusPill state={state} />
        </div>

        <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-ink-600 dark:text-ink-400">
          {s.description}
        </p>

        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="inline-flex items-center gap-1 rounded-md bg-ink-100 px-1.5 py-0.5 font-mono text-[10px] text-ink-700 dark:bg-ink-800 dark:text-ink-300">
            <span className="text-ink-400 dark:text-ink-500">flag</span> {s.flag}
          </span>
          <span
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[10px]"
            style={{ background: `${accent}1f`, color: accent }}
            title="Prometheus alert rule this scenario triggers"
          >
            <span className="opacity-70">alert</span> {s.alert}
          </span>
        </div>

        {/* Actions pinned to the bottom so cards in a row line up. */}
        <div className="mt-auto flex items-center gap-2 pt-3">
          <button
            onClick={() => onInject(s.scenario_id)}
            disabled={!!busy || on}
            className="btn btn-primary !py-1 !text-xs"
          >
            <PlayCircle className={clsx('h-3.5 w-3.5', isThisBusy && !on && 'animate-spin')} />
            Inject
          </button>
          <button
            onClick={() => onReset(s.scenario_id)}
            disabled={!!busy || !on}
            className={clsx('btn !py-1 !text-xs', on && '!border-bad/40 !text-bad hover:!bg-bad/10')}
          >
            <RotateCcw className={clsx('h-3.5 w-3.5', isThisBusy && on && 'animate-spin')} /> Reset
          </button>
          <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px] text-ink-500 dark:text-ink-400">
            <Timer className="h-3 w-3" /> ~{s.eta_seconds}s
          </span>
        </div>

        {/* Lifecycle feedback below the actions. */}
        {state === 'arming' && (
          <div className="mt-3">
            <div className="h-1 w-full overflow-hidden rounded-full bg-ink-200 dark:bg-ink-800">
              <span className="block h-full w-1/3 animate-progress-indeterminate rounded-full bg-warn" />
            </div>
            <p className="mt-1.5 text-[11px] text-warn">
              waiting ~{s.eta_seconds}s for <span className="font-mono">{s.alert}</span> to fire
            </p>
          </div>
        )}
        {state === 'firing' && (
          <p className="mt-3 inline-flex items-center gap-1.5 text-[11px] font-medium text-bad">
            <span className="h-1.5 w-1.5 rounded-full bg-bad animate-pulse-slow" />
            <span className="font-mono">{s.alert}</span> firing — triage running
          </p>
        )}
      </div>
    </div>
  );
}

function PageHeader({ status, lastUpdate }: { status: string; lastUpdate: string | null }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
          Operations overview
        </h1>
        <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
          Live state of the OpenTelemetry demo cluster and the RA-001 Alert Triage agent.
        </p>
      </div>
      <div className="flex items-center gap-2">
        <span className={clsx(
          'chip',
          status === 'open'  && '!border-ok/40 !text-ok',
          status === 'error' && '!border-bad/40 !text-bad',
        )}>
          <span className={clsx(
            'h-1.5 w-1.5 rounded-full',
            status === 'open' ? 'bg-ok animate-pulse-slow' : status === 'error' ? 'bg-bad' : 'bg-warn',
          )} />
          stream · {status}
        </span>
        {lastUpdate && <span className="chip">updated {timeAgo(lastUpdate)}</span>}
      </div>
    </div>
  );
}
