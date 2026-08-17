import { Sparkles, X } from 'lucide-react';
import type { IncidentRowVM } from '@/lib/incidentVm';

export function ChatHeader({ row: _row, onClose }: { row: IncidentRowVM | null; onClose: () => void }) {
  return (
    <div className="border-b border-[var(--icc-border)]">
      <div className="flex items-center justify-between gap-2 p-3 pb-2">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-[var(--icc-fg)]">
            <Sparkles className="h-4 w-4 text-[var(--icc-accent)]" /> RCA agent
          </p>
          <p className="mt-0.5 text-[11px] text-[var(--icc-fg-muted)]">Grounded in the investigation record</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close chat"
          className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-[var(--icc-border)] text-[var(--icc-fg-muted)] transition-colors hover:bg-[var(--icc-surface-2)] hover:text-[var(--icc-fg)]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
