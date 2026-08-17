import type { Investigation, ServiceImpact } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';
import { groupBlastRadius } from '@/lib/rcaDerive';
import { clsx } from '@/lib/format';
import { BlastRadiusGraph } from './BlastRadiusGraph';

const GROUPS: { key: 'directly_affected' | 'indirectly_affected' | 'observed_healthy' | 'not_observed' | 'unknown'; label: string; tone: 'bad' | 'warn' | 'ok' | 'unknown' }[] = [
  { key: 'directly_affected', label: 'Directly affected', tone: 'bad' },
  { key: 'indirectly_affected', label: 'Indirectly affected', tone: 'warn' },
  { key: 'observed_healthy', label: 'Observed healthy', tone: 'ok' },
  // These two are the load-bearing rows: an unexamined dependent must be
  // shown, never silently omitted, and never rendered as healthy.
  { key: 'not_observed', label: 'Not observed', tone: 'unknown' },
  { key: 'unknown', label: 'Unknown', tone: 'unknown' },
];

function ToneDot({ tone }: { tone: 'bad' | 'warn' | 'ok' | 'unknown' }) {
  const color =
    tone === 'bad' ? 'var(--icc-bad)' : tone === 'warn' ? 'var(--icc-warn)' : tone === 'ok' ? 'var(--icc-ok)' : 'var(--icc-unknown)';
  return <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} aria-hidden />;
}

function ImpactRow({ impact, tone }: { impact: ServiceImpact; tone: 'bad' | 'warn' | 'ok' | 'unknown' }) {
  return (
    <li className={clsx('flex items-center gap-2 py-1 text-xs', tone === 'unknown' && 'icc-dashed border-b border-[var(--icc-unknown)]/30')}>
      <ToneDot tone={tone} />
      <span className="font-medium text-[var(--icc-fg)]">{impact.service}</span>
      {impact.hops != null && <span className="text-[var(--icc-fg-faint)]">· {impact.hops} hop(s)</span>}
      <span className="min-w-0 flex-1 truncate text-[var(--icc-fg-muted)]">{impact.rationale}</span>
    </li>
  );
}

export function BlastRadiusTab({ investigation }: { investigation: Investigation | null }) {
  const derived = groupBlastRadius(investigation?.blast_radius ?? null);
  if (!derived.ok) {
    return (
      <SectionShell state="error" message={derived.reason}>
        <div />
      </SectionShell>
    );
  }
  const g = derived.value;

  const root = g.directly_affected.find((i) => i.hops === 0) ?? g.directly_affected[0];
  const children = investigation?.blast_radius?.impacts.filter((i) => i.service !== root?.service) ?? [];

  if (!g.ran) {
    return (
      <SectionShell
        state="unavailable"
        message="Blast radius was not examined"
        reason="No blast-radius stage ran for this incident — this is different from 'nothing affected'."
      >
        <div />
      </SectionShell>
    );
  }

  return (
    <div className="space-y-3">
      {!g.topologyAvailable && (
        <p className="icc-dashed rounded-md border border-[var(--icc-unknown)]/40 px-3 py-2 text-[11px] text-[var(--icc-unknown)]">
          Topology was unavailable — every "unknown" below is a coverage gap, not a finding.
        </p>
      )}
      {root && <BlastRadiusGraph root={root} children={children} />}
      {GROUPS.map((section) => {
        const impacts = g[section.key];
        return (
          <div key={section.key} className="rounded-lg border border-[var(--icc-border)] p-3">
            <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">
              <ToneDot tone={section.tone} /> {section.label} ({impacts.length})
            </p>
            {impacts.length === 0 ? (
              <p className="mt-1 text-[11px] text-[var(--icc-fg-faint)]">none</p>
            ) : (
              <ul className="mt-1 divide-y divide-[var(--icc-border)]">
                {impacts.map((i) => (
                  <ImpactRow key={i.service} impact={i} tone={section.tone} />
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}
