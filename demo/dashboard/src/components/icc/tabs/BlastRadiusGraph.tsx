import type { ServiceImpact } from '@/types/rca';
import { clsx } from '@/lib/format';

// A tree diagram, not a text list. Root -> one row of children, because
// aiops/tools/topology/resolver.py only ever resolves DIRECT dependencies
// today (no recursive walk) — every impact this investigation can report is
// at most 1 hop from the root, so a single-level tree is the honest shape of
// the real data, not a simplification of a deeper graph we don't have.
type Tone = 'bad' | 'warn' | 'ok' | 'unknown';

const TONE_STYLE: Record<Tone, { border: string; text: string; bg: string; dashed: boolean }> = {
  bad: { border: 'border-[var(--icc-bad)]/60', text: 'text-[var(--icc-bad)]', bg: 'bg-[var(--icc-bad)]/5', dashed: false },
  warn: { border: 'border-[var(--icc-warn)]/60', text: 'text-[var(--icc-warn)]', bg: 'bg-[var(--icc-warn)]/5', dashed: false },
  ok: { border: 'border-[var(--icc-ok)]/50', text: 'text-[var(--icc-ok)]', bg: 'bg-[var(--icc-ok)]/5', dashed: false },
  unknown: { border: 'border-[var(--icc-unknown)]/50', text: 'text-[var(--icc-unknown)]', bg: 'bg-[var(--icc-unknown)]/5', dashed: true },
};

function toneFor(state: ServiceImpact['state']): Tone {
  if (state === 'directly_affected') return 'bad';
  if (state === 'indirectly_affected') return 'warn';
  if (state === 'observed_healthy') return 'ok';
  return 'unknown'; // not_observed | unknown
}

function summaryFor(impact: ServiceImpact): string {
  if (impact.state === 'observed_healthy') return 'reachable · healthy';
  if (impact.state === 'not_observed') return 'no telemetry';
  if (impact.state === 'unknown') return 'unknown';
  if (impact.state === 'directly_affected') return 'directly affected · root';
  return 'indirectly affected';
}

function Node({ impact, root }: { impact: ServiceImpact; root?: boolean }) {
  const tone = toneFor(impact.state);
  const style = TONE_STYLE[tone];
  return (
    <div
      title={impact.rationale}
      className={clsx(
        'w-[132px] rounded-lg border px-2.5 py-2 text-center',
        style.border,
        style.bg,
        style.dashed && 'icc-dashed',
        root && 'shadow-sm',
      )}
    >
      <p className={clsx('truncate text-[12px] font-semibold', root ? style.text : 'text-[var(--icc-fg)]')}>
        {impact.service}
      </p>
      <p className={clsx('mt-0.5 text-[10px]', style.text)}>{summaryFor(impact)}</p>
    </div>
  );
}

export function BlastRadiusGraph({
  root,
  children,
}: {
  root: ServiceImpact;
  children: ServiceImpact[];
}) {
  if (children.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 py-4">
        <Node impact={root} root />
        <p className="text-[11px] text-[var(--icc-fg-faint)]">no dependencies were resolved to check</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto py-4">
      <div className="flex min-w-max flex-col items-center">
        <Node impact={root} root />
        {/* Stem from root down to the bus line. */}
        <div className="h-5 w-px bg-[var(--icc-border)]" />
        {/* Bus line + one stem per child, all in a single row so the line
            widths never need to be measured against real DOM layout. */}
        <div className="relative flex items-start justify-center gap-4">
          <div className="absolute left-0 right-0 top-0 h-px bg-[var(--icc-border)]" />
          {children.map((c) => (
            <div key={c.service} className="flex flex-col items-center">
              <div className="h-5 w-px bg-[var(--icc-border)]" />
              <Node impact={c} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
