import { useMemo, useState } from 'react';
import { Bell, Filter, Search } from 'lucide-react';
import { useChatopsSocket } from '@/lib/ws';
import { EmptyState } from '@/components/states';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { timeAgo, clsx } from '@/lib/format';
import type {
  ChatNotification,
  ChatSeverity,
  PersistedNotification,
  Severity,
} from '@/types/api';
import { SeverityBadge } from '@/components/SeverityBadge';

// Map the chatops Severity (p0..p3, info) to the dashboard's Sev-1..Sev-4
// badge so notifications visually align with the alert stream.
const CHAT_TO_DISPLAY: Record<ChatSeverity, Severity | null> = {
  p0: 'Sev-1',
  p1: 'Sev-1',
  p2: 'Sev-2',
  p3: 'Sev-3',
  info: 'Sev-4',
};

const SEV_OPTIONS: { value: ChatSeverity | 'all'; label: string }[] = [
  { value: 'all', label: 'All severities' },
  { value: 'p0', label: 'P0 — critical' },
  { value: 'p1', label: 'P1 — urgent' },
  { value: 'p2', label: 'P2 — important' },
  { value: 'p3', label: 'P3 — minor' },
  { value: 'info', label: 'Info' },
];

const KNOWN_SEVERITIES: ReadonlySet<string> = new Set(['p0', 'p1', 'p2', 'p3', 'info']);

// Response mode shown as a badge so an operator sees at a glance whether a
// human is being woken (PAGE) or it's an async heads-up (NOTIFY) or just
// recorded (LOG).
type ResponseMode = 'PAGE' | 'NOTIFY' | 'LOG';

// Prefer the authoritative response_mode RA-005 decided (carried on the live
// WS frame and persisted on the row): it also factors in business hours, so a
// Sev-2 paged after-hours is "page" — a severity-only guess would render the
// opposite of what the bot did. Fall back to a severity heuristic only for
// rows written before response_mode existed.
function responseFor(n: ChatNotification): ResponseMode {
  switch ((n.response_mode ?? '').toLowerCase()) {
    case 'page':
      return 'PAGE';
    case 'notify':
      return 'NOTIFY';
    case 'log':
      return 'LOG';
  }
  if (n.severity === 'p0' || n.severity === 'p1') return 'PAGE';
  if (n.severity === 'p2' || n.severity === 'p3') return 'NOTIFY';
  return 'LOG';
}

const RESPONSE_STYLE: Record<ResponseMode, string> = {
  PAGE: '!border-bad/50 !text-bad',
  NOTIFY: '!border-warn/50 !text-warn',
  LOG: '!border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400',
};

const RESPONSE_HINT: Record<ResponseMode, string> = {
  PAGE: 'On-call paged now',
  NOTIFY: 'Assigned — review when free',
  LOG: 'Recorded — no page',
};

// Render one persisted DB row in the live feed's ChatNotification shape.
// Mentions/incident_id aren't stored as structured columns (they live inside
// the body text), so backfilled rows show without mention chips — acceptable
// for a history view.
function fromPersisted(row: PersistedNotification): ChatNotification {
  const sev = row.chat_severity.toLowerCase();
  return {
    timestamp: row.routed_at ?? '',
    channel: row.channel,
    severity: (KNOWN_SEVERITIES.has(sev) ? sev : 'info') as ChatSeverity,
    response_mode: row.response_mode ?? undefined,
    title: row.title,
    body: row.body,
    incident_id: null,
    service: row.service,
    mentions: [],
  };
}

export default function Notifications() {
  const { notes, status } = useChatopsSocket();
  // Poll the persisted history every 5s so injected alerts surface even when
  // the live WebSocket frame is missed (e.g. page opened after the alert
  // fired, or the WS reconnecting).
  const history = useFetch(() => api.notifications(200), { intervalMs: 5_000 });
  const [q, setQ] = useState('');
  const [sevFilter, setSevFilter] = useState<ChatSeverity | 'all'>('all');

  // Live WS frames + persisted history, deduped. The WS replay ring is
  // in-memory and empties on every server restart; the DB backfill keeps
  // earlier notifications visible. Rows with channel='suppressed' are
  // routing no-ops (RA-001 deduped the alert) — they were never sent
  // anywhere, so they don't belong in this feed.
  const merged = useMemo(() => {
    const out = [...notes];
    const seen = new Set(notes.map((n) => `${Date.parse(n.timestamp)}|${n.channel}|${n.title}`));
    for (const row of history.data?.notifications ?? []) {
      if (row.channel === 'suppressed') continue;
      const key = `${Date.parse(row.routed_at ?? '')}|${row.channel}|${row.title}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(fromPersisted(row));
    }
    return out.sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
  }, [notes, history.data]);

  const filtered = useMemo(() => {
    const lc = q.toLowerCase();
    return merged.filter((n) => {
      if (sevFilter !== 'all' && n.severity !== sevFilter) return false;
      if (!lc) return true;
      return (
        n.channel.toLowerCase().includes(lc) ||
        n.title.toLowerCase().includes(lc) ||
        n.body.toLowerCase().includes(lc) ||
        (n.service ?? '').toLowerCase().includes(lc) ||
        (n.incident_id ?? '').toLowerCase().includes(lc)
      );
    });
  }, [merged, q, sevFilter]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            Notifications
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            Live chatops feed · {merged.length} shown ({notes.length} live) · stream {status}
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-body flex flex-wrap items-center gap-3">
          <div className="relative min-w-[240px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search by channel, service, incident, body…"
              className="input pl-9"
            />
          </div>
          <div className="flex items-center gap-2">
            <Filter className="h-4 w-4 text-ink-400" />
            <select
              value={sevFilter}
              onChange={(e) => setSevFilter(e.target.value as ChatSeverity | 'all')}
              className="input !w-auto !py-1"
            >
              {SEV_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No notifications yet"
            hint={
              merged.length === 0
                ? 'Trigger a scenario to make an agent fire a notification — it lands here within a second.'
                : 'No notifications match the current filter.'
            }
            icon={<Bell className="h-7 w-7" />}
          />
        </div>
      ) : (
        <ul className="space-y-2">
          {filtered.map((n, i) => (
            <NotificationRow key={`${n.timestamp}-${i}`} n={n} />
          ))}
        </ul>
      )}
    </div>
  );
}

function NotificationRow({ n }: { n: ChatNotification }) {
  const displaySev = CHAT_TO_DISPLAY[n.severity];
  const response = responseFor(n);
  return (
    <li className="card">
      <div className="card-body !py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {displaySev && <SeverityBadge severity={displaySev} />}
              <span
                className={clsx('chip font-mono !text-[10px]', RESPONSE_STYLE[response])}
                title={RESPONSE_HINT[response]}
              >
                {response}
              </span>
              <span className="font-mono text-[10px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
                #{n.channel}
              </span>
              {n.service && (
                <span className="truncate font-mono text-xs text-ink-500 dark:text-ink-400">
                  {n.service}
                </span>
              )}
            </div>
            <h3 className="mt-1.5 truncate text-sm font-semibold text-ink-900 dark:text-ink-50">
              {n.title}
            </h3>
            {n.body && (
              <p className="mt-0.5 text-xs text-ink-500 dark:text-ink-400">
                {n.body}
              </p>
            )}
            {(n.incident_id || n.mentions.length > 0) && (
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
                {n.incident_id && (
                  <span className="chip font-mono">{n.incident_id}</span>
                )}
                {n.mentions.map((m) => (
                  <span
                    key={m}
                    className={clsx(
                      'rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5',
                      'font-mono text-[10px] text-accent',
                    )}
                  >
                    {m}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="flex-shrink-0 text-right font-mono text-[11px] text-ink-500 dark:text-ink-400">
            {timeAgo(n.timestamp)}
          </div>
        </div>
      </div>
    </li>
  );
}
