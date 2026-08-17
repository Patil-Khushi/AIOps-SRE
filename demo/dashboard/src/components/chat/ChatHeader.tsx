import { Sparkles, X } from 'lucide-react';
import type { IncidentRowVM } from '@/lib/incidentVm';
import { SeverityBadge } from '@/components/SeverityBadge';

export function ChatHeader({ row, onClose }: { row: IncidentRowVM | null; onClose: () => void }) {
  return (
    <div className="border-b border-[var(--icc-border)]">
      <div className="flex items-center justify-between gap-2 p-3 pb-2">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-[var(--icc-fg)]">
            <Sparkles className="h-4 w-4 text-[var(--icc-accent)]" /> RCA agent
          </p>
          <p className="mt-0.5 text-[11px] text-[var(--icc-fg-muted)]">Grounded in the investigation record</p>
        </div>
        <button type="button" onClick={onClose} aria-label="Close chat" className="btn btn-ghost !p-1.5">
          <X className="h-4 w-4" />
        </button>
      </div>
      {row && (
        <div className="flex items-center gap-1.5 px-3 pb-2.5 text-[11px] text-[var(--icc-fg-muted)]">
          <SeverityBadge severity={row.severity} />
          <span className="truncate">{row.summary || `${row.incidentId ?? row.id} · ${row.service}`}</span>
        </div>
      )}
    </div>
  );
}
