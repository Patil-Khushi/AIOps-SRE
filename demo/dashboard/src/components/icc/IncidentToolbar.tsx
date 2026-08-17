import { RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import type { Severity } from '@/types/api';
import { clsx } from '@/lib/format';

const ALL_SEVERITIES: Severity[] = ['Sev-1', 'Sev-2', 'Sev-3', 'Sev-4'];

export function IncidentToolbar({
  query,
  onQuery,
  severityFilter,
  onToggleSeverity,
  checkedCount,
  onBulkDebug,
  onRefresh,
  refreshing,
}: {
  query: string;
  onQuery: (q: string) => void;
  severityFilter: Set<Severity>;
  onToggleSeverity: (s: Severity) => void;
  checkedCount: number;
  onBulkDebug: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative min-w-[220px] flex-1">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--icc-fg-faint)]" />
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search incidents…"
          className="input !pl-8"
          aria-label="Search incidents"
        />
      </div>

      <div className="flex items-center gap-1 rounded-lg border border-[var(--icc-border)] bg-[var(--icc-surface-2)] p-1">
        <SlidersHorizontal className="ml-1 h-3.5 w-3.5 text-[var(--icc-fg-faint)]" />
        {ALL_SEVERITIES.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onToggleSeverity(s)}
            className={clsx(
              'rounded-md px-2 py-1 text-[11px] font-medium transition-colors',
              severityFilter.has(s)
                ? 'bg-[var(--icc-accent)] text-white'
                : 'text-[var(--icc-fg-muted)] hover:text-[var(--icc-fg)]',
            )}
          >
            {s}
          </button>
        ))}
      </div>

      {checkedCount > 0 && (
        <button type="button" onClick={onBulkDebug} className="btn btn-primary !py-1.5 !text-xs">
          Debug {checkedCount} selected
        </button>
      )}

      <button type="button" onClick={onRefresh} className="btn btn-ghost !py-1.5 !text-xs">
        <RefreshCw className={clsx('h-3.5 w-3.5', refreshing && 'animate-spin')} /> Refresh
      </button>
    </div>
  );
}
