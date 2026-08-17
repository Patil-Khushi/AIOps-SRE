import { useState } from 'react';
import type { EvidenceItem, Investigation } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';
import { selectedMatrix } from '@/lib/rcaDerive';
import { clsx } from '@/lib/format';

// Four buckets, and they must look different:
//  - supports / contradicts: real findings, --icc-ok / --icc-bad.
//  - checked_absent: looked, condition not present — real evidence against a
//    rival, rendered as a distinct neutral-outlined "finding", never green
//    (it isn't a health signal) and never merged with gaps.
//  - gaps: could not be checked — a blind spot, --icc-gap, always dashed.
// A gap must never render as healthy; checked_absent must never render the
// same as "never checked".
function Bucket({
  title,
  items,
  tone,
}: {
  title: string;
  items: EvidenceItem[];
  tone: 'ok' | 'bad' | 'neutral' | 'gap';
}) {
  const toneClass =
    tone === 'ok'
      ? 'border-[var(--icc-ok)]/40'
      : tone === 'bad'
        ? 'border-[var(--icc-bad)]/40'
        : tone === 'gap'
          ? 'icc-dashed border-[var(--icc-gap)]/50'
          : 'border-[var(--icc-border-strong)]';
  const textClass =
    tone === 'ok' ? 'text-[var(--icc-ok)]' : tone === 'bad' ? 'text-[var(--icc-bad)]' : tone === 'gap' ? 'text-[var(--icc-gap)]' : 'text-[var(--icc-fg-muted)]';

  return (
    <div className={clsx('rounded-lg border p-3', toneClass)}>
      <p className={clsx('text-[11px] font-semibold uppercase tracking-wide', textClass)}>
        {title} ({items.length})
      </p>
      {items.length === 0 ? (
        <p className="mt-1 text-[11px] text-[var(--icc-fg-faint)]">none</p>
      ) : (
        <ul className="mt-1.5 space-y-1.5">
          {items.map((item) => (
            <li key={item.evidence_id} className="text-xs text-[var(--icc-fg)]">
              <span className="font-mono text-[10px] text-[var(--icc-fg-faint)]">{item.evidence_id}</span>{' '}
              {item.statement}
              <span className="ml-1 text-[10px] text-[var(--icc-fg-faint)]">({item.source})</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function EvidenceTab({ investigation }: { investigation: Investigation | null }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  if (!investigation || investigation.matrices.length === 0) {
    return (
      <SectionShell state="unavailable" message="No evidence matrix available" reason="The deterministic investigation did not produce a scored hypothesis for this verdict.">
        <div />
      </SectionShell>
    );
  }

  const matrix =
    investigation.matrices.find((m) => m.hypothesis.hypothesis_id === selectedId) ??
    selectedMatrix(investigation) ??
    investigation.matrices[0];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {investigation.matrices.map((m) => {
          const active = m.hypothesis.hypothesis_id === matrix.hypothesis.hypothesis_id;
          return (
            <button
              key={m.hypothesis.hypothesis_id}
              type="button"
              onClick={() => setSelectedId(m.hypothesis.hypothesis_id)}
              className={clsx(
                'rounded-md border px-2.5 py-1 text-xs font-medium transition-colors',
                active
                  ? 'border-[var(--icc-accent)] bg-[var(--icc-accent-soft)] text-[var(--icc-accent)]'
                  : 'border-[var(--icc-border)] text-[var(--icc-fg-muted)] hover:text-[var(--icc-fg)]',
              )}
            >
              {m.hypothesis.category}
            </button>
          );
        })}
      </div>

      {!matrix.contradiction_search_performed && (
        <p className="text-[11px] text-[var(--icc-fg-faint)]">
          A contradiction search was not performed for this hypothesis.
        </p>
      )}

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Bucket title="Supports" items={matrix.supporting} tone="ok" />
        <Bucket title="Contradicts" items={matrix.contradicting} tone="bad" />
        <Bucket title="Checked — absent" items={matrix.checked_absent} tone="neutral" />
        <Bucket title="Gaps — could not check" items={matrix.gaps} tone="gap" />
      </div>
    </div>
  );
}
