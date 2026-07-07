import { useState } from 'react';
import {
  Tags, Target, Compass, Database, Gauge, PlayCircle, RotateCcw,
  CheckCircle2, XCircle, Sparkles, Cpu,
} from 'lucide-react';
import StatCard from '@/components/StatCard';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import type {
  ClassifierEvalCase,
  IncidentType,
  PersistedClassification,
} from '@/lib/api';
import { timeAgo, clsx } from '@/lib/format';

// Incident-type pill colours — same palette as the standalone classifier UI,
// tuned to read on both the light and dark console themes.
const TYPE_PILL: Record<IncidentType, string> = {
  infrastructure:      'bg-sky-500/15     text-sky-600     dark:text-sky-300     ring-1 ring-inset ring-sky-400/40',
  application:         'bg-violet-500/15  text-violet-600  dark:text-violet-300  ring-1 ring-inset ring-violet-400/40',
  network:             'bg-amber-500/15   text-amber-600   dark:text-amber-300   ring-1 ring-inset ring-amber-400/40',
  external_dependency: 'bg-rose-500/15    text-rose-600    dark:text-rose-300    ring-1 ring-inset ring-rose-400/40',
  change_related:      'bg-emerald-500/15 text-emerald-600 dark:text-emerald-300 ring-1 ring-inset ring-emerald-400/40',
};

const pct = (n?: number | null) => (n == null ? '—' : `${n.toFixed(1)}%`);
const num = (n?: number | null) => (n == null ? '—' : n.toFixed(2));

function accuracyIntent(p?: number | null): 'ok' | 'warn' | 'bad' | 'default' {
  if (p == null) return 'default';
  return p >= 85 ? 'ok' : p >= 60 ? 'warn' : 'bad';
}
function misrouteIntent(p?: number | null): 'ok' | 'warn' | 'bad' | 'default' {
  if (p == null) return 'default';
  return p <= 15 ? 'ok' : p <= 40 ? 'warn' : 'bad';
}

export default function Classifier() {
  const metrics = useFetch(api.classifierMetrics, { intervalMs: 5_000, cacheKey: 'classifier-metrics' });
  const classifications = useFetch(() => api.classifications(50), {
    intervalMs: 10_000,
    cacheKey: 'classifier-rows',
  });
  const [running, setRunning] = useState(false);
  const [evalError, setEvalError] = useState<string | null>(null);

  const runEval = async () => {
    setRunning(true);
    setEvalError(null);
    try {
      await api.classifierEvaluate();
      await Promise.all([metrics.refetch(), classifications.refetch()]);
    } catch (e) {
      setEvalError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const m = metrics.data;
  const rows = classifications.data?.classifications ?? [];
  const busy = running || m?.running === true;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-accent/15">
            <Tags className="h-5 w-5 text-accent" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-ink-900 dark:text-ink-50">Incident Classifier</h1>
            <p className="mt-0.5 max-w-2xl text-sm text-ink-500 dark:text-ink-400">
              The classification step of Alert Triage: it embeds the incident, retrieves similar past
              cases, and assigns one of five incident types — infrastructure, application, network,
              external dependency, or change-related.
            </p>
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <button onClick={runEval} disabled={busy} className="btn btn-primary">
              <PlayCircle className={clsx('h-4 w-4', busy && 'animate-spin')} />
              {busy ? 'Running eval…' : 'Run eval'}
            </button>
            <button
              onClick={() => { metrics.refetch(); classifications.refetch(); }}
              className="btn btn-ghost"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
          {m?.llm_provider && (
            <span className="chip font-mono"><Cpu className="h-3 w-3" /> llm · {m.llm_provider}</span>
          )}
        </div>
      </div>

      {evalError && <ErrorState error={evalError} />}

      {/* Metrics */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Classification accuracy"
          icon={<Target className="h-4 w-4" />}
          value={pct(m?.eval?.accuracy_pct)}
          intent={accuracyIntent(m?.eval?.accuracy_pct)}
          hint={m?.eval ? `${m.eval.passed_cases}/${m.eval.total_cases} cases · ran ${timeAgo(m.eval.ran_at)}` : 'no eval run yet — click "Run eval"'}
        />
        <StatCard
          label="Misroute rate"
          icon={<Compass className="h-4 w-4" />}
          value={pct(m?.eval?.misroute_pct)}
          intent={misrouteIntent(m?.eval?.misroute_pct)}
          hint={m?.eval ? `${m.eval.misroute_cases} of ${m.eval.total_cases} had the wrong type` : 'wrong incident_type vs. golden truth'}
        />
        <StatCard
          label="Total classifications"
          icon={<Database className="h-4 w-4" />}
          value={m?.live ? String(m.live.total_classifications) : '—'}
          hint="rows persisted from the live triage pipeline"
        />
        <StatCard
          label="Avg confidence (live)"
          icon={<Gauge className="h-4 w-4" />}
          value={num(m?.live?.avg_confidence)}
          hint="mean across all persisted classifications"
        />
      </div>

      {/* Golden eval */}
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Golden eval · last run</h2>
            <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
              Classification cases from <span className="font-mono">agents/alert_triage/evals/golden.json</span>.
            </p>
          </div>
          {m?.eval && (
            <span
              className={clsx(
                'rounded-full border px-2.5 py-0.5 font-mono text-[11px] font-semibold',
                m.eval.accuracy_pct >= 85 && 'border-ok/40 bg-ok/10 text-ok',
                m.eval.accuracy_pct < 85 && m.eval.accuracy_pct >= 60 && 'border-warn/40 bg-warn/10 text-warn',
                m.eval.accuracy_pct < 60 && 'border-bad/40 bg-bad/10 text-bad',
              )}
            >
              {m.eval.passed_cases}/{m.eval.total_cases} passed
            </span>
          )}
        </div>
        <div className="card-body">
          {!m?.eval ? (
            <EmptyState
              label="No eval has run in this server's lifetime."
              hint='Click "Run eval" above — with a real LLM this takes ~1–2 min; the stub is near-instant.'
              icon={<Target className="h-7 w-7" />}
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {m.eval.per_case.map((c) => <EvalCaseCard key={c.case_id} c={c} />)}
            </div>
          )}
        </div>
      </section>

      {/* Recent classifications */}
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Recent classifications</h2>
            <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
              Persisted from the live triage pipeline (<span className="font-mono">POST /api/triage</span>). Newest first.
            </p>
          </div>
          <span className="chip font-mono">{rows.length} rows</span>
        </div>
        <div>
          {classifications.loading && !classifications.data ? (
            <LoadingState label="Loading classifications…" />
          ) : classifications.error ? (
            <ErrorState error={classifications.error} />
          ) : rows.length === 0 ? (
            <EmptyState
              label="No classifications yet."
              hint="Inject a scenario or triage an alert from the console — each triaged incident is classified and lands here."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-ink-200 text-left text-[11px] uppercase tracking-wider text-ink-500 dark:border-ink-700 dark:text-ink-400">
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
                <tbody className="divide-y divide-ink-100 dark:divide-ink-800">
                  {rows.map((c) => <ClassificationRow key={c.id} c={c} />)}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function EvalCaseCard({ c }: { c: ClassifierEvalCase }) {
  const failed = c.checks.filter((x) => !x.passed);
  return (
    <div
      className={clsx(
        'rounded-lg border p-3',
        c.passed
          ? 'border-ok/30 bg-ok/5'
          : 'border-bad/40 bg-bad/5 ring-1 ring-bad/20',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink-900 dark:text-ink-100">{c.case_id}</h3>
          <p className="mt-1 flex items-center gap-1 font-mono text-[11px] text-ink-500 dark:text-ink-400">
            {c.incident_type_ok
              ? <CheckCircle2 className="h-3 w-3 text-ok" />
              : <XCircle className="h-3 w-3 text-bad" />}
            type {c.incident_type_ok ? 'ok' : 'wrong'} · {c.duration_ms} ms
          </p>
        </div>
        {c.passed
          ? <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-ok" />
          : <XCircle className="h-5 w-5 flex-shrink-0 text-bad" />}
      </div>
      {failed.length > 0 && (
        <div className="mt-2 space-y-1 border-t border-ink-200 pt-2 dark:border-ink-700">
          {failed.map((chk, i) => (
            <div key={i} className="text-[11px] text-ink-500 dark:text-ink-400">
              <span className="font-mono text-bad">{chk.check}</span>{' '}
              <span>{chk.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ClassificationRow({ c }: { c: PersistedClassification }) {
  const when = c.audit_metadata?.created_at;
  const pill = TYPE_PILL[c.incident_type] ?? 'bg-ink-200 text-ink-700 dark:bg-ink-800 dark:text-ink-200';
  const hits = c.audit_metadata?.similar_incidents ?? [];
  return (
    <tr className="hover:bg-ink-50 dark:hover:bg-ink-800/40">
      <td className="px-4 py-2 align-top font-mono text-[11px] text-ink-500 dark:text-ink-400">
        {when ? timeAgo(when) : '—'}
      </td>
      <td className="px-4 py-2 align-top font-mono text-[11px] text-ink-500 dark:text-ink-400">
        {c.verdict_id != null ? `v#${c.verdict_id}` : '—'}
      </td>
      <td className="px-4 py-2 align-top">
        <span className={clsx('rounded-md px-2 py-0.5 text-[11px] font-semibold', pill)}>
          {c.incident_type}
        </span>
      </td>
      <td className="px-4 py-2 text-right align-top font-mono text-ink-900 dark:text-ink-100">
        {c.confidence.toFixed(2)}
      </td>
      <td className="px-4 py-2 align-top">
        <div className="text-ink-900 dark:text-ink-100">{c.routing_team || '—'}</div>
        {c.on_call_engineer && (
          <div className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{c.on_call_engineer}</div>
        )}
      </td>
      <td className="max-w-[28ch] px-4 py-2 align-top text-ink-700 dark:text-ink-300">
        <div className="truncate" title={c.probable_root_cause}>{c.probable_root_cause || '—'}</div>
        {c.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {c.tags.slice(0, 4).map((t) => (
              <span key={t} className="chip font-mono"><Sparkles className="h-2.5 w-2.5" />{t}</span>
            ))}
          </div>
        )}
      </td>
      <td className="min-w-[26ch] px-4 py-2 align-top text-[11px] text-ink-500 dark:text-ink-400">
        {hits.length === 0 ? (
          <span className="text-ink-400 dark:text-ink-600">—</span>
        ) : (
          <div className="space-y-1">
            {hits.slice(0, 3).map((h, i) => (
              <div key={`${h.incident_key}-${i}`} className="flex items-baseline gap-1.5">
                <span className="font-mono tabular-nums text-ok">{Math.round(h.similarity * 100)}%</span>
                <span className={clsx('shrink-0 rounded px-1 py-px text-[9px] font-semibold', TYPE_PILL[h.incident_type as IncidentType] ?? 'bg-ink-200 text-ink-700 dark:bg-ink-800 dark:text-ink-200')}>
                  {h.incident_type}
                </span>
                <span className="min-w-0 flex-1 break-words" title={h.summary || h.incident_key}>
                  {h.summary || h.incident_key}
                </span>
              </div>
            ))}
            {hits.length > 3 && <div className="text-ink-400 dark:text-ink-600">+{hits.length - 3} more</div>}
          </div>
        )}
      </td>
    </tr>
  );
}
