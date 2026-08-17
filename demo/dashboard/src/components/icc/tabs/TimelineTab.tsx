import type { ComponentType } from 'react';
import { AlertTriangle, Activity, ScrollText, Radio, Boxes, Rocket, Settings, GitBranch, ShieldCheck, CircleDot } from 'lucide-react';
import type { Investigation, RcaTimelineEvent, TimelineSource } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';
import { formatClock, formatDuration } from '@/lib/format';

// Generalizes IncidentCommander.tsx's vertical timeline pattern
// (STAGE_ICON map + connector-line) for RCA's richer TimelineSource union.
const SOURCE_ICON: Record<TimelineSource, ComponentType<{ className?: string }>> = {
  alert: AlertTriangle,
  metrics: Activity,
  logs: ScrollText,
  traces: Radio,
  k8s_events: Boxes,
  deployment: Rocket,
  configuration: Settings,
  dependency: GitBranch,
  remediation: ShieldCheck,
  verification: ShieldCheck,
};

const RELATION_LABEL: Record<RcaTimelineEvent['temporal_relation'], string> = {
  precedes_onset: 'before onset',
  at_onset: 'at onset',
  follows_onset: 'after onset',
  unknown: 'timing unknown',
};

export function TimelineTab({ investigation }: { investigation: Investigation | null }) {
  if (!investigation) {
    return (
      <SectionShell state="unavailable" message="No timeline available">
        <div />
      </SectionShell>
    );
  }
  const tl = investigation.timeline;
  if (tl.events.length === 0) {
    return (
      <SectionShell state="empty" message="No timeline events recorded">
        <div />
      </SectionShell>
    );
  }

  const t0 = tl.onset_at ? new Date(tl.onset_at).getTime() : new Date(tl.events[0].timestamp).getTime();

  return (
    <div className="space-y-3">
      {tl.sources_unavailable.length > 0 && (
        <p className="icc-dashed rounded-md border border-[var(--icc-unknown)]/40 px-3 py-2 text-[11px] text-[var(--icc-unknown)]">
          Sources not examined: {tl.sources_unavailable.join(', ')}
        </p>
      )}
      {tl.truncated && (
        <p className="text-[11px] text-[var(--icc-fg-faint)]">{tl.coverage_note ?? 'Timeline truncated.'}</p>
      )}
      <ol className="relative">
        {tl.events.map((e, i) => {
          const Icon = SOURCE_ICON[e.source] ?? CircleDot;
          const last = i === tl.events.length - 1;
          const offsetSecs = (new Date(e.timestamp).getTime() - t0) / 1000;
          return (
            <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
              {!last && (
                <span className="absolute left-[13px] top-7 -bottom-0.5 w-px bg-[var(--icc-border)]" aria-hidden />
              )}
              <span
                className={
                  'relative z-10 flex h-[26px] w-[26px] flex-none items-center justify-center rounded-full border bg-[var(--icc-surface)] ' +
                  (e.is_change
                    ? 'border-[var(--icc-warn)]/60 text-[var(--icc-warn)]'
                    : 'border-[var(--icc-border)] text-[var(--icc-fg-muted)]')
                }
              >
                <Icon className="h-3.5 w-3.5" />
              </span>
              <div className="min-w-0 pt-0.5">
                <p className="flex flex-wrap items-baseline gap-x-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg)]">
                  {e.service}
                  <span className="font-normal normal-case tabular-nums text-[var(--icc-fg-faint)]">
                    {formatClock(e.timestamp)} · T+{formatDuration(offsetSecs)}
                  </span>
                  {e.is_change && (
                    <span className="rounded bg-[var(--icc-warn)]/15 px-1.5 py-0.5 text-[10px] font-medium text-[var(--icc-warn)]">
                      change · temporal correlation
                    </span>
                  )}
                  <span className="text-[10px] text-[var(--icc-fg-faint)]">{RELATION_LABEL[e.temporal_relation]}</span>
                </p>
                <p className="mt-0.5 text-[11px] leading-snug text-[var(--icc-fg-muted)]">{e.event}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
