import { useState, type ComponentType } from 'react';
import {
  Siren, RefreshCw, Inbox, UserCheck, Search, Tags, GitMerge, Ticket, Bell,
  Brain, MessageSquare, CircleDot, ClipboardList, AlertTriangle, Timer,
} from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, LoadingState, ErrorState } from '@/components/states';
import { SeverityBadge } from '@/components/SeverityBadge';
import { RcaView } from '@/components/RcaView';
import type {
  Severity, PrometheusAlert, IncidentCommandResult, PostmortemSeed,
  IcTimelineEntry, IncidentMetrics,
} from '@/types/api';
import { clsx, timeAgo, formatClock, formatDuration } from '@/lib/format';

// ─── Incident Commander console (RA-008, SRE) ───────────────────────────────
//
// The SRE coordination surface. Unlike the RCA console (which works off triage
// verdicts), the Incident Commander runs the WHOLE flow from a firing alert —
// so this console is alert-sourced: pick a firing alert, run RA-008, and watch
// it scribe the timeline, run RCA (reusing RcaView, incl. the HITL apply box),
// seed a postmortem, and request a human-IC handoff. Coordination engages only
// for Sev-1/Sev-2; lower severities come back "not engaged" (the reactive
// pipeline still ran).

function inferSeverity(hint: string | null | undefined): Severity {
  const s = (hint || '').toLowerCase();
  if (s.includes('critical') || s === 'p1') return 'Sev-1';
  if (s.includes('high')     || s === 'p2') return 'Sev-2';
  if (s.includes('warning')  || s === 'p3') return 'Sev-3';
  return 'Sev-4';
}

// Severity → soft tint for the engaged banner.
const SEV_TINT: Record<Severity, string> = {
  'Sev-1': 'border-bad/30 bg-bad/5 text-bad',
  'Sev-2': 'border-warn/30 bg-warn/5 text-warn',
  'Sev-3': 'border-accent/30 bg-accent/5 text-accent',
  'Sev-4': 'border-ink-300/50 bg-ink-100/60 text-ink-500 dark:border-ink-700 dark:bg-ink-800/40 dark:text-ink-400',
};

// Stage → icon for the vertical timeline. Stage labels come from the backend
// (agents/incident_commander/agent.py); unknown stages fall back to a dot.
const STAGE_ICON: Record<string, ComponentType<{ className?: string }>> = {
  detected: AlertTriangle,
  triage: Search,
  classify: Tags,
  correlate: GitMerge,
  ticket: Ticket,
  notify: Bell,
  rca: Brain,
  comms: MessageSquare,
  handoff: UserCheck,
};

export default function IncidentCommander() {
  const alerts = useFetch(api.liveAlerts, { intervalMs: 0, cacheKey: 'live-alerts' });
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [result, setResult] = useState<IncidentCommandResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const list = alerts.data?.alerts ?? [];
  const picked = list.find((a) => a.alert_id === pickedId) ?? null;

  // Selecting an alert clears any prior coordination so a stale result never
  // lingers on a different alert.
  const select = (alert: PrometheusAlert) => {
    setPickedId(alert.alert_id);
    setResult(null);
    setError(null);
  };

  const run = async () => {
    if (!picked) return;
    setResult(null);
    setError(null);
    setBusy(true);
    try {
      setResult(await api.incidentCommander(picked));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <Siren className="h-6 w-6 text-accent" /> Incident Commander
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            RA-008 (SRE) · coordinates Sev-1/Sev-2 response — timeline, root cause, postmortem, and human-IC handoff.
          </p>
        </div>
        <button onClick={alerts.refetch} className="btn btn-ghost">
          <RefreshCw className={clsx('h-4 w-4', alerts.loading && 'animate-spin')} /> Refresh alerts
        </button>
      </div>

      {alerts.loading && !alerts.data ? (
        <div className="card"><LoadingState label="Loading firing alerts…" /></div>
      ) : alerts.error ? (
        <div className="card"><ErrorState error={alerts.error} /></div>
      ) : list.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No firing alerts"
            hint="Inject a scenario on the Overview page — once an alert fires, pick it here and run the Incident Commander."
            icon={<Inbox className="h-7 w-7" />}
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          {/* Alert picker */}
          <div className="space-y-2 lg:col-span-2">
            <p className="px-1 text-[11px] font-semibold uppercase tracking-wider text-ink-400 dark:text-ink-500">
              Firing alerts · {list.length}
            </p>
            <ul className="space-y-2">
              {list.map((a) => {
                const sev = inferSeverity(a.severity_hint);
                const isPicked = a.alert_id === pickedId;
                return (
                  <li key={a.alert_id}>
                    <button
                      onClick={() => select(a)}
                      className={clsx(
                        'card w-full text-left transition-all hover:-translate-y-0.5 hover:border-accent hover:shadow-md',
                        isPicked && '!border-accent ring-1 ring-accent/30',
                      )}
                    >
                      <div className="card-body !py-3">
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
                        <p className="mt-0.5 font-mono text-[11px] text-ink-400 dark:text-ink-500">
                          {timeAgo(a.timestamp)}
                        </p>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>

          {/* Coordination panel */}
          <aside className="lg:col-span-3">
            <div className="card sticky top-20 overflow-hidden">
              <div className="flex items-center justify-between gap-3 border-b border-ink-200 px-5 py-3 dark:border-ink-700">
                <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
                  <Siren className="h-3.5 w-3.5 text-accent" /> Incident coordination
                </p>
                <button
                  type="button"
                  onClick={run}
                  // No re-run: coordinating the same alert twice would re-triage
                  // it and hit RA-001 dedup/idempotency. Pick another alert (which
                  // clears the result) to coordinate again.
                  disabled={busy || !picked || !!result}
                  className="btn btn-primary !py-1.5 !text-xs"
                >
                  {busy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Siren className="h-3.5 w-3.5" />}
                  {result ? 'Coordinated' : 'Run Incident Commander'}
                </button>
              </div>
              <div className="card-body">
                {!picked && !result && !busy && (
                  <EmptyState
                    label="Select an alert"
                    hint="Pick a firing alert on the left, then run the Incident Commander to coordinate the response."
                    icon={<Siren className="h-7 w-7" />}
                  />
                )}
                {picked && !result && !busy && !error && (
                  <div className="rounded-lg border border-dashed border-ink-300 p-4 text-center dark:border-ink-700">
                    <p className="text-sm text-ink-600 dark:text-ink-300">
                      Ready to coordinate <span className="font-semibold text-ink-900 dark:text-ink-50">{picked.service}</span>
                    </p>
                    <p className="mt-1 text-xs text-ink-400 dark:text-ink-500">
                      Press <span className="font-medium">Run Incident Commander</span> to chain triage → RCA → handoff.
                    </p>
                  </div>
                )}
                {busy && <CoordinatingSkeleton service={picked?.service} />}
                {error && <p className="text-sm text-bad">{error}</p>}
                {result && !busy && <CommandResult result={result} />}
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function CoordinatingSkeleton({ service }: { service?: string }) {
  return (
    <div className="space-y-4">
      <p className="flex items-center gap-2 text-sm text-ink-500 dark:text-ink-400">
        <RefreshCw className="h-4 w-4 animate-spin text-accent" />
        Coordinating {service ? <span className="font-medium text-ink-700 dark:text-ink-200">{service}</span> : 'incident'}…
      </p>
      <div className="space-y-2 animate-pulse">
        <div className="h-3 w-2/3 rounded bg-ink-200 dark:bg-ink-700" />
        <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
        <div className="h-20 w-full rounded bg-ink-200 dark:bg-ink-700" />
        <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
      </div>
    </div>
  );
}

function CommandResult({ result }: { result: IncidentCommandResult }) {
  const tint = SEV_TINT[result.severity];
  return (
    <div className="space-y-5">
      {/* Status banner */}
      <div className={clsx('flex items-start gap-3 rounded-xl border p-4', tint)}>
        <Siren className="mt-0.5 h-5 w-5 flex-none" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">
              {result.engaged ? 'Incident Commander engaged' : 'Coordination not engaged'}
            </span>
            <SeverityBadge severity={result.severity} />
          </div>
          <p className="mt-1 truncate text-xs opacity-80">
            {result.affected_service}
            {result.handoff_requested && ' · human IC handoff requested'}
          </p>
        </div>
        {result.handoff_requested && (
          <span className="flex flex-none items-center gap-1 rounded-md border border-current px-2 py-1 text-[11px] font-medium opacity-90">
            <UserCheck className="h-3 w-3" /> handoff
          </span>
        )}
      </div>

      {!result.engaged && (
        <p className="text-xs leading-relaxed text-ink-500 dark:text-ink-400">
          Severity is below Sev-2, so the Incident Commander does not coordinate this incident. The
          reactive triage pipeline still ran — see the timeline below.
        </p>
      )}

      {/* Response metrics (MTTA/MTTR-style, measured from detection) */}
      {result.metrics && (
        <Section icon={<Timer className="h-3.5 w-3.5" />} title="Response metrics">
          <MetricsStrip metrics={result.metrics} />
        </Section>
      )}

      {/* Vertical timeline */}
      <Section icon={<ClipboardList className="h-3.5 w-3.5" />} title="Incident timeline" count={result.timeline.length}>
        <Timeline entries={result.timeline} />
      </Section>

      {/* RCA (shared renderer — includes the HITL apply box) */}
      {result.rca && (
        <Section icon={<Brain className="h-3.5 w-3.5" />} title="Root-cause analysis">
          <RcaView v={result.rca} incidentId={result.reactive.ticket.ticket_id ?? null} />
        </Section>
      )}

      {/* Postmortem seed */}
      {result.postmortem_seed && <PostmortemView seed={result.postmortem_seed} />}
    </div>
  );
}

function Section({
  icon, title, count, children,
}: {
  icon: React.ReactNode;
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section className="border-t border-ink-200 pt-4 first:border-0 first:pt-0 dark:border-ink-700">
      <p className="mb-3 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
        {icon}
        {title}
        {count !== undefined && <span className="text-ink-400 dark:text-ink-500">· {count}</span>}
      </p>
      {children}
    </section>
  );
}

function Timeline({ entries }: { entries: IcTimelineEntry[] }) {
  // T0 is detection (when the alert fired) so every offset reads from there.
  // The backend stamps a "detected" beat at the alert time and sorts the
  // timeline, so it's normally entries[0]; find it explicitly rather than
  // assume position, falling back to the first entry if it's ever absent.
  const anchor = entries.find((e) => e.stage === 'detected') ?? entries[0];
  const t0 = anchor ? new Date(anchor.ts).getTime() : 0;
  return (
    <ol className="relative">
      {entries.map((e, i) => {
        const Icon = STAGE_ICON[e.stage] ?? CircleDot;
        const last = i === entries.length - 1;
        const offsetSecs = (new Date(e.ts).getTime() - t0) / 1000;
        return (
          <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
            {!last && (
              <span className="absolute left-[13px] top-7 -bottom-0.5 w-px bg-ink-200 dark:bg-ink-700" aria-hidden />
            )}
            <span className="relative z-10 flex h-[26px] w-[26px] flex-none items-center justify-center rounded-full border border-ink-200 bg-white text-ink-500 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-400">
              <Icon className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0 pt-0.5">
              <p className="flex flex-wrap items-baseline gap-x-2 text-[11px] font-semibold uppercase tracking-wide text-ink-800 dark:text-ink-100">
                {e.stage}
                <span className="font-normal normal-case tabular-nums text-ink-400 dark:text-ink-500">
                  {formatClock(e.ts)} · T+{formatDuration(offsetSecs)}
                </span>
              </p>
              <p className="mt-0.5 text-[11px] leading-snug text-ink-500 dark:text-ink-400">{e.detail}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

// Compact MTTA/MTTR-style chips. Only stages that ran are shown (a null metric
// means the stage didn't happen on this incident — e.g. handoff when not engaged).
function MetricsStrip({ metrics }: { metrics: IncidentMetrics }) {
  const items: Array<[string, number | null | undefined]> = [
    ['Detect → Triage', metrics.time_to_triage_seconds],
    ['Detect → Page (MTTA)', metrics.time_to_notify_seconds],
    ['Detect → Handoff', metrics.time_to_handoff_seconds],
    ['Total', metrics.total_coordination_seconds],
  ];
  const shown = items.filter(([, v]) => v !== null && v !== undefined);
  if (!shown.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {shown.map(([label, v]) => (
        <div
          key={label}
          className="rounded-md border border-ink-200 bg-ink-50 px-2.5 py-1.5 dark:border-ink-700 dark:bg-ink-800/40"
        >
          <p className="text-[10px] uppercase tracking-wide text-ink-400 dark:text-ink-500">{label}</p>
          <p className="text-sm font-semibold tabular-nums text-ink-800 dark:text-ink-100">
            {formatDuration(v as number)}
          </p>
        </div>
      ))}
    </div>
  );
}

function PostmortemView({ seed }: { seed: PostmortemSeed }) {
  return (
    <details className="group border-t border-ink-200 pt-4 dark:border-ink-700">
      <summary className="flex cursor-pointer items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-500 transition-colors hover:text-accent dark:text-ink-400">
        <ClipboardList className="h-3.5 w-3.5" />
        Postmortem draft
        <span className="font-normal normal-case text-ink-400 dark:text-ink-500">· auto-seeded</span>
      </summary>
      <div className="mt-3 space-y-2 rounded-lg bg-ink-50 p-3 text-[11px] leading-relaxed text-ink-700 dark:bg-ink-800/40 dark:text-ink-200">
        <p>
          <span className="text-ink-500 dark:text-ink-400">Summary: </span>
          {seed.incident_summary}
        </p>
        {seed.ticket_id && (
          <p>
            <span className="text-ink-500 dark:text-ink-400">Ticket: </span>
            <span className="font-mono">{seed.ticket_id}</span>
          </p>
        )}
        {seed.root_cause && (
          <p>
            <span className="text-ink-500 dark:text-ink-400">Root cause: </span>
            {seed.root_cause}
          </p>
        )}
        <div>
          <p className="text-ink-500 dark:text-ink-400">
            Contributing signals · {seed.contributing_signals.length}
          </p>
          <ol className="mt-1 space-y-0.5 border-l border-ink-200 pl-3 font-mono dark:border-ink-700">
            {seed.contributing_signals.map((s, i) => (
              <li key={i} className="leading-relaxed">
                {i + 1}. {s}
              </li>
            ))}
          </ol>
        </div>
      </div>
    </details>
  );
}
