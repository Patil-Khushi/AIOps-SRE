import { useEffect, useState, useCallback, ReactNode } from 'react';
import {
  Layers,
  PlayCircle,
  RotateCcw,
  Cpu,
  ShieldAlert,
  Tags,
  ListChecks,
  Users,
  Gauge,
  Sparkles,
  AlertTriangle,
  Network,
} from 'lucide-react';
import { api } from './api';
import type {
  CombinedResult,
  Fixture,
  IncidentType,
  Severity,
  TriageVerdict,
  Classification,
} from './api';
import { clsx, fmtNum, timeAgo } from './utils';

const SEV_PILL: Record<Severity, string> = {
  'Sev-1': 'bg-rose-500/15    text-rose-300    ring-1 ring-inset ring-rose-400/40',
  'Sev-2': 'bg-amber-500/15   text-amber-300   ring-1 ring-inset ring-amber-400/40',
  'Sev-3': 'bg-sky-500/15     text-sky-300     ring-1 ring-inset ring-sky-400/40',
  'Sev-4': 'bg-slate-500/15   text-slate-300   ring-1 ring-inset ring-slate-400/40',
};

const TYPE_PILL: Record<IncidentType, string> = {
  infrastructure:      'bg-sky-500/15     text-sky-300     ring-1 ring-inset ring-sky-400/40',
  application:         'bg-violet-500/15  text-violet-300  ring-1 ring-inset ring-violet-400/40',
  network:             'bg-amber-500/15   text-amber-300   ring-1 ring-inset ring-amber-400/40',
  external_dependency: 'bg-rose-500/15    text-rose-300    ring-1 ring-inset ring-rose-400/40',
  change_related:      'bg-emerald-500/15 text-emerald-300 ring-1 ring-inset ring-emerald-400/40',
};

export default function App() {
  const [fixtures, setFixtures] = useState<Fixture[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [result, setResult] = useState<CombinedResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadFixtures = useCallback(async () => {
    try {
      const data = await api.fixtures();
      setFixtures(data.cases);
      if (data.cases.length > 0) {
        setSelected((prev) => prev || data.cases[0].id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    loadFixtures();
  }, [loadFixtures]);

  const run = async () => {
    const fixture = fixtures.find((f) => f.id === selected);
    if (!fixture) return;
    setRunning(true);
    setError(null);
    try {
      const r = await api.run(fixture.input);
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const activeFixture = fixtures.find((f) => f.id === selected);

  return (
    <div className="min-h-screen px-6 py-8 lg:px-12">
      <div className="mx-auto max-w-7xl space-y-6">
        <Header
          fixtures={fixtures}
          selected={selected}
          onSelect={setSelected}
          onRun={run}
          onRefresh={loadFixtures}
          running={running}
        />

        {activeFixture && (
          <AlertPreview fixture={activeFixture} />
        )}

        {error && (
          <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 p-4 text-sm text-rose-200">
            Run failed: <span className="font-mono">{error}</span>
          </div>
        )}

        {!result && !error && (
          <div className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-16 text-center text-sm text-slate-500">
            <Layers className="mx-auto mb-3 h-8 w-8 text-slate-600" />
            <p>Pick a fixture and click <span className="font-semibold text-slate-300">Run pipeline</span>.</p>
            <p className="mt-1 text-xs text-slate-600">
              The alert flows through the full RA-001 triage (8 steps) then the full RA-002 classification.
            </p>
          </div>
        )}

        {result && (
          <>
            <VerdictBanner result={result} />
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <TriagePanel verdict={result.verdict} />
              <ClassificationPanel classification={result.classification} />
            </div>
          </>
        )}

        <Footer checkedAt={result?.verdict.audit_metadata?.created_at} />
      </div>
    </div>
  );
}

// ─── header ────────────────────────────────────────────────────────────────

interface HeaderProps {
  fixtures: Fixture[];
  selected: string;
  onSelect: (id: string) => void;
  onRun: () => void;
  onRefresh: () => void;
  running: boolean;
}

function Header({ fixtures, selected, onSelect, onRun, onRefresh, running }: HeaderProps) {
  return (
    <div className="rounded-2xl border border-sky-500/30 bg-gradient-to-br from-sky-500/10 via-violet-500/5 to-fuchsia-500/10 p-6 shadow-2xl shadow-sky-950/20">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-sky-500/40">
            <Layers className="h-6 w-6 text-white" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="rounded-full border border-sky-400/40 bg-sky-500/15 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-sky-200">
                RA-001 + RA-002
              </span>
              <span className="text-[10px] uppercase tracking-widest text-slate-500">
                reactive-active · phase 1
              </span>
            </div>
            <h1 className="mt-1 bg-gradient-to-r from-sky-200 via-violet-200 to-fuchsia-200 bg-clip-text text-3xl font-bold tracking-tight text-transparent">
              Triage + Classifier
            </h1>
            <p className="mt-1 max-w-2xl text-sm text-slate-400">
              One alert, both workflows: the full 8-step Alert Triage runs first,
              then its verdict feeds the full Incident Classifier. Each half is
              identical to the standalone agent — this surface just runs them
              back-to-back.
            </p>
          </div>
        </div>

        <div className="flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <select
              value={selected}
              onChange={(e) => onSelect(e.target.value)}
              className="max-w-[16rem] truncate rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm text-slate-200 focus:border-sky-400/60 focus:outline-none"
            >
              {fixtures.length === 0 && <option value="">no fixtures</option>}
              {fixtures.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.id}
                </option>
              ))}
            </select>
            <button
              onClick={onRun}
              disabled={running || !selected}
              className={clsx(
                'inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-all',
                'bg-gradient-to-r from-sky-500 to-violet-500 text-white shadow-lg shadow-sky-500/30',
                'hover:from-sky-400 hover:to-violet-400 hover:shadow-sky-500/50',
                'disabled:cursor-not-allowed disabled:opacity-50',
              )}
            >
              <PlayCircle className={clsx('h-4 w-4', running && 'animate-spin')} />
              {running ? 'Running…' : 'Run pipeline'}
            </button>
            <button
              onClick={onRefresh}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-sm font-medium text-slate-200 transition-colors hover:border-sky-400/60 hover:text-sky-200"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Reload
            </button>
          </div>
          <span className="inline-flex items-center gap-1 rounded-full border border-slate-700 bg-slate-900/60 px-2.5 py-0.5 font-mono text-[11px] text-slate-400">
            <Cpu className="h-3 w-3" /> RA-001 → RA-002 chain
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── alert preview ───────────────────────────────────────────────────────────

function AlertPreview({ fixture }: { fixture: Fixture }) {
  const a = fixture.input as Record<string, unknown>;
  const ann = (a.annotations ?? {}) as Record<string, string>;
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
          input alert
        </span>
        <Field label="service" value={String(a.service ?? '—')} mono />
        <Field label="metric" value={String(a.metric ?? '—')} mono />
        <Field label="value" value={String(a.value ?? '—')} mono />
        {a.threshold != null && <Field label="threshold" value={String(a.threshold)} mono />}
        <Field label="source" value={String(a.source ?? '—')} mono />
      </div>
      {(ann.description || ann.summary) && (
        <p className="mt-2 text-[13px] text-slate-400">{ann.description || ann.summary}</p>
      )}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[11px] uppercase tracking-wider text-slate-600">{label}</span>
      <span className={clsx('text-slate-200', mono && 'font-mono text-[13px]')}>{value}</span>
    </span>
  );
}

// ─── verdict banner ───────────────────────────────────────────────────────────

function VerdictBanner({ result }: { result: CombinedResult }) {
  const v = result.verdict;
  const c = result.classification;
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <StatCard
        label="Severity"
        icon={<ShieldAlert className="h-4 w-4" />}
        value={<span className={clsx('rounded-md px-2 py-0.5 text-lg font-bold', SEV_PILL[v.severity])}>{v.severity}</span>}
        hint={v.customer_facing ? 'customer-facing' : 'internal'}
      />
      <StatCard
        label="Incident type"
        icon={<Tags className="h-4 w-4" />}
        value={<span className={clsx('rounded-md px-2 py-0.5 text-sm font-semibold', TYPE_PILL[c.incident_type])}>{c.incident_type}</span>}
        hint={`RA-002 conf ${fmtNum(c.confidence)}`}
      />
      <StatCard
        label="Assigned team"
        icon={<Users className="h-4 w-4" />}
        value={<span className="text-lg font-semibold text-slate-100">{v.assigned_team}</span>}
        hint={v.assigned_engineer ?? 'team channel'}
      />
      <StatCard
        label="Triage confidence"
        icon={<Gauge className="h-4 w-4" />}
        value={<span className="text-2xl font-bold tabular-nums text-slate-100">{fmtNum(v.confidence_score)}</span>}
        hint={v.status === 'Suppressed' ? 'duplicate — suppressed' : `${v.duplicate_alert_count}× seen`}
      />
    </div>
  );
}

function StatCard({ label, icon, value, hint }: { label: string; icon: ReactNode; value: ReactNode; hint: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 backdrop-blur">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">{label}</span>
        <span className="text-slate-500">{icon}</span>
      </div>
      <div className="mt-2">{value}</div>
      <p className="mt-1 truncate text-[11px] text-slate-500" title={hint}>{hint}</p>
    </div>
  );
}

// ─── triage panel ─────────────────────────────────────────────────────────────

function TriagePanel({ verdict }: { verdict: TriageVerdict }) {
  return (
    <section className="overflow-hidden rounded-xl border border-sky-500/25 bg-slate-900/40 backdrop-blur">
      <PanelHeader
        badge="RA-001"
        badgeClass="border-sky-400/40 bg-sky-500/15 text-sky-200"
        title="Alert Triage"
        subtitle="8-step workflow · verdict"
        icon={<AlertTriangle className="h-4 w-4 text-sky-300" />}
      />
      <div className="space-y-4 p-5">
        <div>
          <Label>Summary</Label>
          <p className="mt-1 text-sm text-slate-200">{verdict.alert_summary}</p>
        </div>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <KV k="Affected service" v={verdict.affected_service} mono />
          <KV k="Status" v={verdict.status} />
          <KV k="Assigned team" v={verdict.assigned_team} />
          <KV k="On-call" v={verdict.assigned_engineer ?? '—'} mono />
          {verdict.recommended_runbook && <KV k="Runbook" v={verdict.recommended_runbook} mono />}
          <KV k="Duplicates" v={String(verdict.duplicate_alert_count)} />
        </div>
        <DecisionTrace
          title="Decision trace"
          lines={verdict.audit_metadata?.decision_trace ?? []}
          icon={<ListChecks className="h-3.5 w-3.5" />}
        />
      </div>
    </section>
  );
}

// ─── classification panel ─────────────────────────────────────────────────────

function ClassificationPanel({ classification }: { classification: Classification }) {
  const hits = classification.audit_metadata?.similar_incidents ?? [];
  return (
    <section className="overflow-hidden rounded-xl border border-violet-500/25 bg-slate-900/40 backdrop-blur">
      <PanelHeader
        badge="RA-002"
        badgeClass="border-violet-400/40 bg-violet-500/15 text-violet-200"
        title="Incident Classifier"
        subtitle="type · root cause · routing"
        icon={<Tags className="h-4 w-4 text-violet-300" />}
      />
      <div className="space-y-4 p-5">
        <div>
          <Label>Probable root cause</Label>
          <p className="mt-1 text-sm text-slate-200">{classification.probable_root_cause || '—'}</p>
        </div>
        <div>
          <Label>Rationale</Label>
          <p className="mt-1 text-[13px] text-slate-400">{classification.rationale}</p>
        </div>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <KV k="Routing team" v={classification.routing_team} />
          <KV k="On-call" v={classification.on_call_engineer ?? '—'} mono />
        </div>
        {classification.tags.length > 0 && (
          <div>
            <Label>Tags</Label>
            <div className="mt-1 flex flex-wrap gap-1">
              {classification.tags.map((t) => (
                <span key={t} className="inline-flex items-center gap-0.5 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-400">
                  <Sparkles className="h-2.5 w-2.5" />{t}
                </span>
              ))}
            </div>
          </div>
        )}
        {classification.dependencies.length > 0 && (
          <div>
            <Label>Downstream dependencies</Label>
            <div className="mt-1 flex flex-wrap gap-1">
              {classification.dependencies.map((d) => (
                <span key={d} className="inline-flex items-center gap-1 rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-300">
                  <Network className="h-2.5 w-2.5" />{d}
                </span>
              ))}
            </div>
          </div>
        )}
        {hits.length > 0 && (
          <div>
            <Label>Similar past incidents</Label>
            <div className="mt-1 space-y-1">
              {hits.slice(0, 3).map((h, i) => (
                <div key={`${h.incident_key}-${i}`} className="flex items-baseline gap-1.5 text-[12px]">
                  <span className="font-mono tabular-nums text-emerald-300">{Math.round(h.similarity * 100)}%</span>
                  <span className={clsx('shrink-0 rounded px-1 py-px text-[9px] font-semibold', TYPE_PILL[h.incident_type as IncidentType] ?? 'bg-slate-700 text-slate-200')}>
                    {h.incident_type}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-slate-400" title={h.summary || h.incident_key}>
                    {h.summary || h.incident_key}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        <DecisionTrace
          title="Decision trace"
          lines={classification.audit_metadata?.decision_trace ?? []}
          icon={<ListChecks className="h-3.5 w-3.5" />}
        />
      </div>
    </section>
  );
}

// ─── shared bits ──────────────────────────────────────────────────────────────

function PanelHeader({ badge, badgeClass, title, subtitle, icon }: { badge: string; badgeClass: string; title: string; subtitle: string; icon: ReactNode }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
      <div className="flex items-center gap-2">
        {icon}
        <div>
          <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
          <p className="text-[11px] text-slate-500">{subtitle}</p>
        </div>
      </div>
      <span className={clsx('rounded-full border px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider', badgeClass)}>
        {badge}
      </span>
    </div>
  );
}

function Label({ children }: { children: ReactNode }) {
  return <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{children}</span>;
}

function KV({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-slate-600">{k}</div>
      <div className={clsx('mt-0.5 text-slate-200', mono && 'font-mono text-[13px]')}>{v}</div>
    </div>
  );
}

function DecisionTrace({ title, lines, icon }: { title: string; lines: string[]; icon: ReactNode }) {
  if (lines.length === 0) return null;
  return (
    <details className="rounded-lg border border-slate-800 bg-slate-950/50">
      <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400 hover:text-slate-200">
        {icon}
        {title} · {lines.length} steps
      </summary>
      <ol className="space-y-1 px-3 pb-3 pt-1">
        {lines.map((line, i) => (
          <li key={i} className="flex gap-2 text-[12px] leading-relaxed text-slate-400">
            <span className="select-none font-mono text-slate-600">{String(i + 1).padStart(2, '0')}</span>
            <span className="min-w-0 break-words">{line}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

function Footer({ checkedAt }: { checkedAt?: string | null }) {
  return (
    <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-800 pt-4 text-[11px] text-slate-600">
      <span className="font-mono">RA-001+002 · Combined Triage + Classifier · standalone surface</span>
      <span className="font-mono">{checkedAt ? `last run ${timeAgo(checkedAt)}` : 'no run yet'}</span>
    </footer>
  );
}
