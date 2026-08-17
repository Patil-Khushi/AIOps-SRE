import { Bug, Info } from 'lucide-react';
import type { IncidentRowVM } from '@/lib/incidentVm';
import { timeAgo } from '@/lib/format';
import { SeverityBadge } from '@/components/SeverityBadge';
import { clsx } from '@/lib/format';
import { DragHandle } from './DragHandle';
import { FiringDot } from './FiringDot';

const LIFECYCLE_LABEL: Record<IncidentRowVM['lifecycle'], string> = {
  detected: 'Detected',
  triaged: 'Triaged',
  investigating: 'Investigating',
  rca_ready: 'RCA ready',
  approval: 'Awaiting approval',
  remediating: 'Remediating',
  verifying: 'Verifying',
  resolved: 'Resolved',
};

export function IncidentRowItem({
  row,
  selected,
  checked,
  draggable,
  onSelect,
  onToggleCheck,
  onDebug,
  onOpenWorkspace,
  onKeyReorder,
  onDragStart,
  onDragOver,
  onDrop,
}: {
  row: IncidentRowVM;
  selected: boolean;
  checked: boolean;
  draggable: boolean;
  onSelect: () => void;
  onToggleCheck: () => void;
  onDebug: () => void;
  onOpenWorkspace: () => void;
  onKeyReorder: (dir: 'up' | 'down') => void;
  onDragStart: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
}) {
  return (
    <li
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
      className={clsx(
        'flex items-center gap-3 border-b border-[var(--icc-border)] px-3 py-2.5 transition-colors last:border-0',
        selected ? 'bg-[var(--icc-accent-soft)]' : 'hover:bg-[var(--icc-surface-2)]',
      )}
    >
      <DragHandle onKeyReorder={onKeyReorder} />
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggleCheck}
        aria-label={`Select incident ${row.service}`}
        className="h-3.5 w-3.5 accent-[var(--icc-accent)]"
      />

      <button type="button" onClick={onSelect} className="min-w-0 flex-1 text-left">
        <p className="truncate text-sm font-medium text-[var(--icc-fg)]">{row.summary}</p>
        <p className="mt-0.5 flex items-center gap-1.5 truncate font-mono text-[11px] text-[var(--icc-fg-muted)]">
          {row.service} · {row.team} · {timeAgo(row.createdAt)}
        </p>
      </button>

      <SeverityBadge severity={row.severity} />

      <FiringDot firing={row.firing} label={row.firing ? `${row.service} is firing` : `${row.service} is not currently firing`} />

      <span
        className={clsx(
          'hidden whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-medium sm:inline-block',
          row.lifecycleUnknown
            ? 'border-[var(--icc-unknown)]/50 text-[var(--icc-unknown)]'
            : 'border-[var(--icc-border-strong)] text-[var(--icc-fg-muted)]',
        )}
        title={row.lifecycleUnknown ? 'Lifecycle stage could not be determined from available signals' : undefined}
      >
        {LIFECYCLE_LABEL[row.lifecycle]}
      </span>

      <button
        type="button"
        onClick={onDebug}
        className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border border-[var(--icc-accent)]/40 bg-[var(--icc-accent-soft)] px-2.5 py-1 text-xs font-medium text-[var(--icc-accent)] transition hover:bg-[var(--icc-accent)]/20"
      >
        <Bug className="h-3.5 w-3.5" /> Debug
      </button>
      <button
        type="button"
        onClick={onOpenWorkspace}
        title="Open incident view"
        aria-label={`Open the full incident view for ${row.service}`}
        className="flex-shrink-0 rounded p-1 text-[var(--icc-fg-faint)] transition-colors hover:bg-[var(--icc-surface-2)] hover:text-[var(--icc-accent)]"
      >
        <Info className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}
