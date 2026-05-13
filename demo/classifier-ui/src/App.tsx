import { useEffect, useState, useCallback, ReactNode } from 'react';
import {
  Tags,
  PlayCircle,
  RotateCcw,
  Target,
  Compass,
  Database,
  Gauge,
  CheckCircle2,
  XCircle,
  Sparkles,
  Cpu,
} from 'lucide-react';
import { api } from './api';
import type {
  EvalCase,
  IncidentType,
  MetricsResponse,
  PersistedClassification,
} from './api';
import { clsx, fmtNum, fmtPct, timeAgo } from './utils';

const TYPE_PILL: Record<IncidentType, string> = {
  infrastructure:      'bg-sky-500/15     text-sky-300     ring-1 ring-inset ring-sky-400/40',
  application:         'bg-violet-500/15  text-violet-300  ring-1 ring-inset ring-violet-400/40',
  network:             'bg-amber-500/15   text-amber-300   ring-1 ring-inset ring-amber-400/40',
  external_dependency: 'bg-rose-500/15    text-rose-300    ring-1 ring-inset ring-rose-400/40',
  change_related:      'bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-400/40',
};

function pollingFetch<T>(fetcher: () => Promise<T>, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const run = useCallback(async () => {
    try {
      const d = await fetcher();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let alive = true;
    run();
    let timer: ReturnType<typeof setInterval> | null = null;
    if (intervalMs && intervalMs > 0) {
      timer = setInterval(() => alive && run(), intervalMs);
    }
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [intervalMs, run]);

  return { data, error, loading, refetch: run };
}

export default function App() {
  const metrics = pollingFetch(api.metrics, 5_000);
  const classifications = pollingFetch(() => api.classifications(50), 10_000);
  const [running, setRunning] = useState(false);
  const [evalError, setEvalError] = useState<string | null>(null);

  const runEval = async () => {
    setRunning(true);
    setEvalError(null);
    try {
      await api.evaluate();
      await metrics.refetch();
      await classifications.refetch();
    } catch (e) {
      setEvalError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="min-h-screen px-6 py-8 lg:px-12">
      <div className="mx-auto max-w-7xl space-y-6">
        <Header
          running={running || metrics.data?.running === true}
          provider={metrics.data?.llm_provider ?? null}
          onRunEval={runEval}
          onRefresh={() => {
            metrics.refetch();
            classifications.refetch();
          }}
        />

        {evalError && (
          <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200">
            Eval run failed: <span className="font-mono">{evalError}</span>
          </div>
        )}

        <MetricRow data={metrics.data} />

        <EvalPanel data={metrics.data} />

        <ClassificationsPanel
          rows={classifications.data?.classifications ?? []}
          loading={classifications.loading && !classifications.data}
          error={classifications.error}
        />

        <Footer checked={metrics.data?.checked_at} />
      </div>
    </div>
  );
}

// ─── header ────────────────────────────────────────────────────────────────

interface HeaderProps {
  running: boolean;
  provider: string | null;
  onRunEval: () => void;
  onRefresh: () => void;
}

function Header({ running, provider, onRunEval, onRefresh }: HeaderProps) {
  return (
    <div className="rounded-2xl border border-violet-500/30 bg-gradient-to-br from-violet-500/10 via-fuchsia-500/5 to-cyan-500/10 p-6 shadow-2xl shadow-violet-950/20">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-500 shadow-lg shadow-violet-500/40">
            <Tags className="h-6 w-6 text-white" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="rounded-full border border-violet-400/40 bg-violet-500/15 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-violet-200">
                RA-002
              </span>
              <span className="text-[10px] uppercase tracking-widest text-slate-500">
                reactive-active · phase 1
              </span>
            </div>
            <h1 className="mt-1 bg-gradient-to-r from-violet-200 via-fuchsia-200 to-cyan-200 bg-clip-text text-3xl font-bold tracking-tight text-transparent">
              Incident Classifier
            </h1>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              Embeds the incident text, retrieves similar past cases from the
              vector store, and classifies the incident into one of five types
              — independent of upstream CMDB enrichment.
            </p>
          </div>
        </div>

        <div className="flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <button
              onClick={onRunEval}
              disabled={running}
              className={clsx(
                'inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-all',
                'bg-gradient-to-r from-violet-500 to-fuchsia-500 text-white shadow-lg shadow-violet-500/30',
                'hover:from-violet-400 hover:to-fuchsia-400 hover:shadow-violet-500/50',
                'disabled:cursor-not-allowed disabled:opacity-50',
              )}
            >
              <PlayCircle className={clsx('h-4 w-4', running && 'animate-spin')} />
              {running ? 'Running eval…' : 'Run eval'}
            </button>
            <button
              onClick={onRefresh}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm font-medium text-slate-200 transition-colors hover:border-violet-400/60 hover:text-violet-200"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
          {provider && (
            <span className="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900/60 px-2.5 py-0.5 font-mono text-[11px] text-slate-400">
              <Cpu className="h-3 w-3" /> llm · {provider}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── metric row ────────────────────────────────────────────────────────────

function intentColor(intent: 'ok' | 'warn' | 'bad' | 'neutral'): string {
  return {
    ok:      'text-emerald-300',
    warn:    'text-amber-300',
    bad:     'text-rose-300',
    neutral: 'text-violet-200',
  }[intent];
}
function intentRing(intent: 'ok' | 'warn' | 'bad' | 'neutral'): string {
  return {
    ok:      'border-emerald-500/40',
    warn:    'border-amber-500/40',
    bad:     'border-rose-500/40',
    neutral: 'border-slate-800',
  }[intent];
}

function intentFromAccuracy(pct?: number | null): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (pct === null || pct === undefined) return 'neutral';
  if (pct >= 85) return 'ok';
  if (pct >= 60) return 'warn';
  return 'bad';
}
function intentFromMisroute(pct?: number | null): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (pct === null || pct === undefined) return 'neutral';
  if (pct <= 15) return 'ok';
  if (pct <= 40) return 'warn';
  return 'bad';
}

function MetricRow({ data }: { data: MetricsResponse | null }) {
  const ev = data?.eval ?? null;
  const live = data?.live ?? null;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <MetricCard
        label="Classification accuracy"
        icon={<Target className="h-4 w-4" />}
        value={ev ? fmtPct(ev.accuracy_pct) : '—'}
        intent={intentFromAccuracy(ev?.accuracy_pct)}
        hint={
          ev
            ? `${ev.passed_cases}/${ev.total_cases} cases · ran ${timeAgo(ev.ran_at)}`
            : 'no eval run yet — click "Run eval"'
        }
      />
      <MetricCard
        label="Misroute rate"
        icon={<Compass className="h-4 w-4" />}
        value={ev ? fmtPct(ev.misroute_pct) : '—'}
        intent={intentFromMisroute(ev?.misroute_pct)}
        hint={
          ev
            ? `${ev.misroute_cases} of ${ev.total_cases} cases had the wrong incident_type`
            : 'wrong incident_type vs. golden truth'
        }
      />
      <MetricCard
        label="Total classifications"
        icon={<Database className="h-4 w-4" />}
        value={live ? String(live.total_classifications) : '—'}
        intent="neutral"
        hint="rows persisted via save_classification"
      />
      <MetricCard
        label="Avg confidence (live)"
        icon={<Gauge className="h-4 w-4" />}
        value={live ? fmtNum(live.avg_confidence) : '—'}
        intent="neutral"
        hint="mean across all persisted classifications"
      />
    </div>
  );
}

interface MetricProps {
  label: string;
  icon: ReactNode;
  value: string;
  intent: 'ok' | 'warn' | 'bad' | 'neutral';
  hint: string;
}

function MetricCard({ label, icon, value, intent, hint }: MetricProps) {
  return (
    <div
      className={clsx(
        'rounded-xl border bg-slate-900/60 p-5 backdrop-blur transition-colors',
        intentRing(intent),
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          {label}
        </span>
        <span className="text-slate-500">{icon}</span>
      </div>
      <div
        className={clsx(
          'mt-3 font-semibold tracking-tight',
          'text-3xl tabular-nums',
          intentColor(intent),
        )}
      >
        {value}
      </div>
      <p className="mt-1 text-[11px] text-slate-500">{hint}</p>
    </div>
  );
}

// ─── eval cases ────────────────────────────────────────────────────────────

function EvalPanel({ data }: { data: MetricsResponse | null }) {
  return (
    <section className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 backdrop-blur">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
            Golden eval · last run
          </h2>
          {data?.eval && (
            <p className="mt-0.5 text-[11px] text-slate-500">
              Each card is one case from{' '}
              <span className="font-mono text-slate-400">
                agents/incident_classifier/evals/golden.json
              </span>
              .
            </p>
          )}
        </div>
        {data?.eval && (
          <span
            className={clsx(
              'rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold',
              data.eval.accuracy_pct >= 85 && 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
              data.eval.accuracy_pct < 85 && data.eval.accuracy_pct >= 60 && 'border-amber-500/40 bg-amber-500/10 text-amber-200',
              data.eval.accuracy_pct < 60 && 'border-rose-500/40 bg-rose-500/10 text-rose-200',
            )}
          >
            {data.eval.passed_cases}/{data.eval.total_cases} passed
          </span>
        )}
      </div>
      <div className="p-5">
        {!data?.eval ? (
          <div className="py-10 text-center text-sm text-slate-500">
            <p>No eval has run in this server's lifetime.</p>
            <p className="mt-1 text-xs text-slate-600">
              Click <span className="font-semibold text-slate-400">Run eval</span> above
              — Azure GPT-5 × 5 cases takes ~1–2 min.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {data.eval.per_case.map((c) => (
              <EvalCaseCard key={c.case_id} c={c} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function EvalCaseCard({ c }: { c: EvalCase }) {
  const failed = c.checks.filter((x) => !x.passed);
  return (
    <div
      className={clsx(
        'rounded-lg border bg-slate-950/60 p-3',
        c.passed
          ? 'border-emerald-500/30'
          : 'border-rose-500/40 ring-1 ring-rose-500/20',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-100">{c.case_id}</h3>
          <p className="mt-1 flex items-center gap-1 font-mono text-[11px] text-slate-500">
            {c.incident_type_ok ? (
              <CheckCircle2 className="h-3 w-3 text-emerald-400" />
            ) : (
              <XCircle className="h-3 w-3 text-rose-400" />
            )}
            type {c.incident_type_ok ? 'ok' : 'wrong'} · {c.duration_ms} ms
          </p>
        </div>
        {c.passed ? (
          <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-emerald-400" />
        ) : (
          <XCircle className="h-5 w-5 flex-shrink-0 text-rose-400" />
        )}
      </div>
      {failed.length > 0 && (
        <div className="mt-2 space-y-1 border-t border-slate-800 pt-2">
          {failed.map((chk, i) => (
            <div key={i} className="text-[11px] text-slate-400">
              <span className="font-mono text-rose-300">{chk.check}</span>{' '}
              <span className="text-slate-500">{chk.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── classifications table ────────────────────────────────────────────────

interface TableProps {
  rows: PersistedClassification[];
  loading: boolean;
  error: string | null;
}

function ClassificationsPanel({ rows, loading, error }: TableProps) {
  return (
    <section className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 backdrop-blur">
      <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
            Recent classifications
          </h2>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Rows persisted via{' '}
            <span className="font-mono text-slate-400">save_classification</span> from{' '}
            <span className="font-mono text-slate-400">POST /api/triage</span>. Newest first.
          </p>
        </div>
        <span className="rounded-full border border-slate-700 bg-slate-900/60 px-2.5 py-0.5 font-mono text-[11px] text-slate-300">
          {rows.length} rows
        </span>
      </div>
      <div>
        {loading ? (
          <div className="px-5 py-8 text-center text-sm text-slate-500">loading…</div>
        ) : error ? (
          <div className="px-5 py-8 text-center text-sm text-rose-300">{error}</div>
        ) : rows.length === 0 ? (
          <div className="px-5 py-10 text-center text-sm text-slate-500">
            <p>No classifications yet.</p>
            <p className="mt-1 text-xs text-slate-600">
              POST to /api/triage to create one — or inject a scenario from the main dashboard.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-950/60 text-left text-[11px] uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="px-4 py-2 font-semibold">When</th>
                  <th className="px-4 py-2 font-semibold">Verdict</th>
                  <th className="px-4 py-2 font-semibold">Type</th>
                  <th className="px-4 py-2 text-right font-semibold">Conf</th>
                  <th className="px-4 py-2 font-semibold">Routing</th>
                  <th className="px-4 py-2 font-semibold">Probable root cause</th>
                  <th className="px-4 py-2 font-semibold">Similar hits</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {rows.map((c) => (
                  <ClassificationRow key={c.id} c={c} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function ClassificationRow({ c }: { c: PersistedClassification }) {
  const when = c.audit_metadata?.created_at;
  const pill = TYPE_PILL[c.incident_type] ?? 'bg-slate-700 text-slate-200';
  return (
    <tr className="hover:bg-slate-800/40">
      <td className="px-4 py-2 align-top font-mono text-[11px] text-slate-500">
        {timeAgo(when)}
      </td>
      <td className="px-4 py-2 align-top font-mono text-[11px] text-slate-500">
        {c.verdict_id != null ? `v#${c.verdict_id}` : '—'}
      </td>
      <td className="px-4 py-2 align-top">
        <span className={clsx('rounded-md px-2 py-0.5 text-[11px] font-semibold', pill)}>
          {c.incident_type}
        </span>
      </td>
      <td className="px-4 py-2 text-right align-top font-mono text-slate-200">
        {c.confidence.toFixed(2)}
      </td>
      <td className="px-4 py-2 align-top">
        <div className="text-slate-100">{c.routing_team || '—'}</div>
        {c.on_call_engineer && (
          <div className="font-mono text-[11px] text-slate-500">{c.on_call_engineer}</div>
        )}
      </td>
      <td className="max-w-[28ch] px-4 py-2 align-top text-slate-300">
        <div className="truncate" title={c.probable_root_cause}>
          {c.probable_root_cause || '—'}
        </div>
        {c.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {c.tags.slice(0, 4).map((t) => (
              <span
                key={t}
                className="inline-flex items-center gap-0.5 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-400"
              >
                <Sparkles className="h-2.5 w-2.5" />
                {t}
              </span>
            ))}
          </div>
        )}
      </td>
      <td className="px-4 py-2 align-top font-mono text-[11px] text-slate-500">
        {c.similar_incident_ids.length > 0 ? (
          <div className="space-y-0.5">
            {c.similar_incident_ids.slice(0, 3).map((id) => (
              <div key={id}>{id}</div>
            ))}
            {c.similar_incident_ids.length > 3 && (
              <div className="text-slate-600">+{c.similar_incident_ids.length - 3} more</div>
            )}
          </div>
        ) : (
          '—'
        )}
      </td>
    </tr>
  );
}

// ─── footer ───────────────────────────────────────────────────────────────

function Footer({ checked }: { checked?: string | null }) {
  return (
    <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 pt-4 text-[11px] text-slate-600">
      <span className="font-mono">
        RA-002 · Incident Classifier · standalone surface
      </span>
      <span className="font-mono">
        metrics polled {checked ? timeAgo(checked) : '—'}
      </span>
    </footer>
  );
}
