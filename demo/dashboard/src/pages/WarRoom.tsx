import { useState } from 'react';
import {
  AlertTriangle, Radio, Users, ListChecks, ClipboardList,
  CheckCircle2, XCircle, Inbox, Video,
} from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, ErrorState } from '@/components/states';
import { clsx, timeAgo } from '@/lib/format';
import type { WarRoomAssembly, WarRoomFeedRow } from '@/lib/api';

// ─── RA-006 War-Room Assembler console ──────────────────────────────────────
//
// RA-006's own surface: a live board of every war room the /api/triage pipeline
// has assembled (polled every 5s). Each incident shows its lifecycle status
// (open → in_call → resolved); expand one to see the invited SMEs, the Slack
// join link, the context pack, and the timeline. Data comes from
// /api/war-room/* on the demo server; the agent lives in
// agents/war_room_assembler.

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

const ATT_STYLE: Record<string, string> = {
  joined: 'border-ok/40 text-ok',
  declined: 'border-bad/40 text-bad',
  invited: 'border-ink-300/40 text-ink-400',
};

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

      {(a.meeting_url || a.bridge_url) && (
        <div className="flex flex-wrap items-center gap-2">
          {a.meeting_url && (
            <a
              href={a.meeting_url}
              target="_blank"
              rel="noreferrer"
              className="btn btn-primary !py-1.5"
            >
              <Video className="h-4 w-4" /> Join meeting
            </a>
          )}
          {a.bridge_url && (
            <a
              href={a.bridge_url}
              target="_blank"
              rel="noreferrer"
              className="btn btn-ghost !py-1.5"
            >
              <Radio className="h-4 w-4" /> Slack channel
            </a>
          )}
          <Chip
            className={
              a.bridge_status === 'created'
                ? 'border-ok/40 text-ok'
                : 'border-warn/40 text-warn'
            }
          >
            {a.bridge_status === 'created'
              ? 'Slack channel created'
              : `bridge: ${a.bridge_status}`}
          </Chip>
        </div>
      )}

      {a.invited.length > 0 && (
        <div className="rounded-lg border border-ink-200 p-3 dark:border-ink-700">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Users className="h-4 w-4 text-bad" /> Invited SMEs
          </div>
          <ul className="space-y-2">
            {a.invited.map((s, i) => (
              <li key={i} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="flex items-center gap-2">
                  <span className="font-mono">{s.name || s.handle}</span>
                  <Chip className={ATT_STYLE[s.attendance ?? 'invited'] ?? ''}>
                    {s.attendance ?? 'invited'}
                  </Chip>
                </span>
                <span className="text-xs text-ink-400">{s.team ?? s.reason}</span>
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

const STATUS_STYLE: Record<string, string> = {
  open: 'border-warn/40 text-warn',
  in_call: 'border-accent/40 text-accent',
  call_ended: 'border-ink-300/40 text-ink-300',
  resolved: 'border-ok/40 text-ok',
  no_room: 'border-ink-300/40 text-ink-400',
};
const STATUS_LABEL: Record<string, string> = {
  open: 'Open',
  in_call: 'In call',
  call_ended: 'Call ended',
  resolved: 'Resolved',
  no_room: 'No room',
};

function LiveFeed() {
  const feed = useFetch(() => api.warRoomRecent(50), { intervalMs: 5000 });
  const [open, setOpen] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const rows: WarRoomFeedRow[] = feed.data?.war_rooms ?? [];

  const setStatus = async (id: string, status: string) => {
    setBusyId(id);
    try {
      await api.warRoomSetStatus(id, status);
      await feed.refetch();
    } catch {
      /* surfaced on the next 5s poll */
    } finally {
      setBusyId(null);
    }
  };


  return (
    <div className="card">
      <div className="card-body">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
          <Radio className="h-4 w-4 text-ok" /> Live feed — incidents ({rows.length})
        </div>
        {feed.error && <ErrorState error={feed.error} />}
        {!feed.error && rows.length === 0 && (
          <EmptyState
            icon={<Inbox className="h-5 w-5" />}
            label="No war rooms yet"
            hint="Assemble one on the left, or trigger a Sev-1/Sev-2 through the pipeline."
          />
        )}
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.id} className="rounded-lg border border-ink-200 dark:border-ink-700">
              <button
                onClick={() => setOpen(open === r.id ? null : r.id)}
                className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm"
              >
                <span className="flex flex-wrap items-center gap-2">
                  <Chip className={chatSevStyle(r.chat_severity)}>{r.severity}</Chip>
                  <Chip className={STATUS_STYLE[r.status] ?? ''}>
                    {STATUS_LABEL[r.status] ?? r.status}
                  </Chip>
                  <span className="font-mono text-accent">{r.channel}</span>
                </span>
                <span className="flex items-center gap-2 text-xs text-ink-400">
                  <Users className="h-3 w-3" /> {r.sme_count}
                  <span>{timeAgo(r.assembled_at)}</span>
                </span>
              </button>
              {open === r.id && (
                <div className="space-y-3 border-t border-ink-200 p-3 dark:border-ink-700">
                  <AssemblyView a={r.assembly} />
                  {r.status !== 'no_room' && (
                    <div className="flex flex-wrap gap-2">
                      {r.status === 'open' && (
                        <button
                          disabled={busyId === r.id}
                          onClick={() => setStatus(r.id, 'in_call')}
                          className="btn btn-ghost !py-1 text-xs"
                        >
                          Mark in-call
                        </button>
                      )}
                      {r.status === 'in_call' && (
                        <button
                          disabled={busyId === r.id}
                          onClick={() => setStatus(r.id, 'call_ended')}
                          className="btn btn-ghost !py-1 text-xs"
                        >
                          End call
                        </button>
                      )}
                      {r.status !== 'resolved' && (
                        <button
                          disabled={busyId === r.id}
                          onClick={() => setStatus(r.id, 'resolved')}
                          className="btn btn-ghost !py-1 text-xs"
                        >
                          Mark resolved
                        </button>
                      )}
                    </div>
                  )}
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
            <MetricChip label="Incidents" value={m.total_seen} />
            <MetricChip label="Open" value={m.open} />
            <MetricChip label="Resolved" value={m.resolved} />
            <MetricChip label="Avg SMEs" value={m.avg_smes ?? '—'} />
          </div>
        )}
      </div>

      <LiveFeed />
    </div>
  );
}
