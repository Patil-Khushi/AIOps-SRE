import { useState } from 'react';
import {
  AlertTriangle, Radio, Users, ListChecks, ClipboardList, RefreshCw, Beaker,
  CheckCircle2, XCircle, Inbox,
} from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, ErrorState } from '@/components/states';
import { clsx, timeAgo } from '@/lib/format';
import type { WarRoomAssembly, WarRoomTryRequest, WarRoomFeedRow } from '@/lib/api';

// ─── RA-006 War-Room Assembler console ──────────────────────────────────────
//
// RA-006's own surface. Left: a "try it" inspector that runs the agent's pure
// `decide` (no chatops emit) so you can see how it reacts to any severity /
// status. Right: the live feed of war rooms the real /api/triage pipeline has
// assembled (polled every 5s). Data comes from /api/war-room/* on the demo
// server; the agent itself lives in agents/war_room_assembler.

const SEVERITIES = ['Sev-1', 'Sev-2', 'Sev-3', 'Sev-4'];
const STATUSES = ['Active', 'Suppressed'];

function chatSevStyle(sev: string): string {
  switch (sev) {
    case 'p0':
    case 'p1':
      return 'border-bad/40 text-bad';
    case 'p2':
      return 'border-warn/40 text-warn';
    case 'p3':
      return 'border-yellow-500/40 text-yellow-500';
    default:
      return 'border-ink-300/40 text-ink-400';
  }
}

function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={clsx('chip border', className)}>{children}</span>
  );
}

function AssemblyView({ a }: { a: WarRoomAssembly }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {a.assembled ? (
          <Chip className="border-ok/40 text-ok">
            <CheckCircle2 className="h-3.5 w-3.5" /> assembled
          </Chip>
        ) : (
          <Chip className="border-ink-300/40 text-ink-400">
            <XCircle className="h-3.5 w-3.5" /> no war room
          </Chip>
        )}
        <Chip className={chatSevStyle(a.chat_severity)}>{a.chat_severity.toUpperCase()}</Chip>
        <span className="font-mono text-sm text-accent">{a.channel}</span>
      </div>

      <p className="text-sm text-ink-500 dark:text-ink-400">{a.reason}</p>

      {a.invited.length > 0 && (
        <div className="rounded-lg border border-ink-200 p-3 dark:border-ink-700">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Users className="h-4 w-4 text-bad" /> Invited SMEs
          </div>
          <ul className="space-y-1.5">
            {a.invited.map((s, i) => (
              <li key={i} className="flex items-center justify-between text-sm">
                <span className="font-mono">{s.handle}</span>
                <span className="text-xs text-ink-400">{s.reason} · {s.source}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {a.context_pack.length > 0 && (
        <div className="rounded-lg border border-ink-200 p-3 dark:border-ink-700">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <ClipboardList className="h-4 w-4 text-accent" /> Context pack
          </div>
          <dl className="space-y-1.5">
            {a.context_pack.map((c, i) => (
              <div key={i} className="grid grid-cols-[150px_1fr] gap-2 text-sm">
                <dt className="text-ink-500 dark:text-ink-400">{c.label}</dt>
                <dd
                  className={clsx(
                    'truncate font-mono text-xs',
                    c.value === 'unavailable' ? 'text-ink-400/60' : '',
                  )}
                  title={c.value}
                >
                  {c.value}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {a.timeline.length > 0 && (
        <div className="rounded-lg border border-ink-200 p-3 dark:border-ink-700">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <ListChecks className="h-4 w-4 text-warn" /> Timeline
          </div>
          <ol className="space-y-1.5">
            {a.timeline.map((e, i) => (
              <li key={i} className="flex gap-2 text-sm">
                <span className="font-mono text-xs text-ink-400">{timeAgo(e.at)}</span>
                <span>{e.event}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      <details className="rounded-lg border border-ink-200 p-3 dark:border-ink-700">
        <summary className="cursor-pointer text-xs font-medium text-ink-500 dark:text-ink-400">
          audit trace ({a.audit_trace.length})
        </summary>
        <ul className="mt-2 space-y-1 font-mono text-xs text-ink-400">
          {a.audit_trace.map((t, i) => <li key={i}>{t}</li>)}
        </ul>
      </details>
    </div>
  );
}

function TryIt() {
  const [form, setForm] = useState<WarRoomTryRequest>({
    affected_service: 'payment',
    severity: 'Sev-1',
    assigned_team: 'Payments Team',
    assigned_engineer: 'oncall@payments.example.com',
    alert_summary: '',
    recommended_runbook: '',
    status: 'Active',
    incident_id: '',
  });
  const [result, setResult] = useState<WarRoomAssembly | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (k: keyof WarRoomTryRequest, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.warRoomAssemble(form));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="card">
        <div className="card-body space-y-4">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Beaker className="h-4 w-4 text-bad" /> Try it — inspect RA-006 behaviour
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs font-medium text-ink-500 dark:text-ink-400">
              Severity
              <select className="input mt-1" value={form.severity} onChange={(e) => set('severity', e.target.value)}>
                {SEVERITIES.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
            <label className="block text-xs font-medium text-ink-500 dark:text-ink-400">
              Status
              <select className="input mt-1" value={form.status} onChange={(e) => set('status', e.target.value)}>
                {STATUSES.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
            <label className="block text-xs font-medium text-ink-500 dark:text-ink-400">
              Affected service
              <input className="input mt-1" value={form.affected_service} onChange={(e) => set('affected_service', e.target.value)} />
            </label>
            <label className="block text-xs font-medium text-ink-500 dark:text-ink-400">
              Owning team
              <input className="input mt-1" value={form.assigned_team} onChange={(e) => set('assigned_team', e.target.value)} />
            </label>
            <label className="col-span-2 block text-xs font-medium text-ink-500 dark:text-ink-400">
              On-call engineer (optional)
              <input className="input mt-1" value={form.assigned_engineer ?? ''} onChange={(e) => set('assigned_engineer', e.target.value)} />
            </label>
            <label className="col-span-2 block text-xs font-medium text-ink-500 dark:text-ink-400">
              Alert summary (optional)
              <input className="input mt-1" value={form.alert_summary ?? ''} placeholder="auto-generated if blank" onChange={(e) => set('alert_summary', e.target.value)} />
            </label>
          </div>
          <button onClick={submit} disabled={busy} className="btn btn-primary">
            {busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Radio className="h-4 w-4" />}
            Assemble (dry-run)
          </button>
          {error && <ErrorState error={error} />}
        </div>
      </div>

      {result && (
        <div className="card">
          <div className="card-body">
            <AssemblyView a={result} />
          </div>
        </div>
      )}
    </div>
  );
}

function LiveFeed() {
  const feed = useFetch(() => api.warRoomRecent(50), { intervalMs: 5000 });
  const [open, setOpen] = useState<number | null>(null);
  const rows: WarRoomFeedRow[] = feed.data?.war_rooms ?? [];

  return (
    <div className="card">
      <div className="card-body">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
          <Radio className="h-4 w-4 text-ok" /> Live feed — from the pipeline ({rows.length})
        </div>
        {feed.error && <ErrorState error={feed.error} />}
        {!feed.error && rows.length === 0 && (
          <EmptyState
            icon={<Inbox className="h-5 w-5" />}
            label="No war rooms yet"
            hint="Trigger one via POST /api/triage or a failure scenario."
          />
        )}
        <ul className="space-y-2">
          {rows.map((r, i) => (
            <li key={i} className="rounded-lg border border-ink-200 dark:border-ink-700">
              <button
                onClick={() => setOpen(open === i ? null : i)}
                className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm"
              >
                <span className="flex items-center gap-2">
                  <Chip className={chatSevStyle(r.chat_severity)}>{r.severity}</Chip>
                  <span className="font-mono text-accent">{r.channel}</span>
                </span>
                <span className="flex items-center gap-2 text-xs text-ink-400">
                  <Users className="h-3 w-3" /> {r.sme_count}
                  <span>{timeAgo(r.assembled_at)}</span>
                </span>
              </button>
              {open === i && (
                <div className="border-t border-ink-200 p-3 dark:border-ink-700">
                  <AssemblyView a={r.assembly} />
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function MetricChip({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-ink-200 px-4 py-2 dark:border-ink-700">
      <div className="text-xs text-ink-400">{label}</div>
      <div className="text-lg font-semibold">{value}</div>
    </div>
  );
}

export default function WarRoom() {
  const metrics = useFetch(api.warRoomMetrics, { intervalMs: 5000 });
  const m = metrics.data;

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-3">
          <AlertTriangle className="h-6 w-6 text-bad" />
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            War-Room Assembler
          </h1>
          <span className="chip">RA-006</span>
        </div>
        <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
          On Sev-1/Sev-2, stands up the incident war room — channel, on-call SME, live context
          pack, and a seed timeline for RCA.
        </p>
        {m && (
          <div className="mt-4 flex flex-wrap gap-3">
            <MetricChip label="Seen" value={m.total_seen} />
            <MetricChip label="Assembled" value={m.assembled} />
            <MetricChip label="Suppressed / minor" value={m.suppressed_or_minor} />
            <MetricChip label="Avg SMEs" value={m.avg_smes ?? '—'} />
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <TryIt />
        <LiveFeed />
      </div>
    </div>
  );
}
