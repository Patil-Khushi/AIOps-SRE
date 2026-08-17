import { AlertTriangle, Loader2, Inbox, EyeOff, CheckCircle2 } from 'lucide-react';

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 p-12 text-ink-500 dark:text-ink-400">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span className="text-sm">{label}</span>
    </div>
  );
}

export function ErrorState({ error }: { error: string }) {
  return (
    <div className="card animate-fade-in border-bad/40">
      <div className="card-body flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-bad" />
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-bad">Could not load data</h3>
          <p className="mt-1 break-words font-mono text-xs text-ink-600 dark:text-ink-400">{error}</p>
        </div>
      </div>
    </div>
  );
}

// `tone='checked'` is for a list that is empty BECAUSE something was checked
// and found absent — checked_absent is a finding, not a void, and must not
// read the same as "we never looked" (that's UnavailableState, below).
export function EmptyState({
  label,
  hint,
  icon,
  tone = 'default',
}: {
  label: string;
  hint?: string;
  icon?: React.ReactNode;
  tone?: 'default' | 'checked';
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 p-10 text-center">
      <div className={tone === 'checked' ? 'text-ok' : 'text-ink-400 dark:text-ink-500'}>
        {icon ?? (tone === 'checked' ? <CheckCircle2 className="h-7 w-7" /> : <Inbox className="h-7 w-7" />)}
      </div>
      <p className="text-sm font-medium text-ink-700 dark:text-ink-200">{label}</p>
      {hint && <p className="max-w-sm text-xs text-ink-500 dark:text-ink-400">{hint}</p>}
    </div>
  );
}

// A stage that did not run / a signal that could not be checked. Visually
// UNLIKE EmptyState (which could be misread as "we looked, all clear") —
// dashed border, --icc-unknown, EyeOff icon. Never render this as green.
export function UnavailableState({
  label,
  reason,
  sources,
}: {
  label: string;
  reason?: string;
  sources?: string[];
}) {
  return (
    <div className="icc-dashed flex flex-col items-center justify-center gap-2 rounded-lg border p-8 text-center border-[var(--icc-unknown)]/40">
      <EyeOff className="h-6 w-6 text-[var(--icc-unknown)]" />
      <p className="text-sm font-medium text-[var(--icc-unknown)]">{label}</p>
      {reason && <p className="max-w-sm text-xs text-[var(--icc-fg-muted)]">{reason}</p>}
      {sources && sources.length > 0 && (
        <p className="max-w-sm font-mono text-[11px] text-[var(--icc-fg-faint)]">
          not examined: {sources.join(', ')}
        </p>
      )}
    </div>
  );
}

// Real data is shown alongside this — it must never look like the section
// finished cleanly when some of its sources didn't answer.
export function PartialDataNotice({
  present,
  missing,
  note,
}: {
  present: string[];
  missing: string[];
  note?: string;
}) {
  if (missing.length === 0) return null;
  return (
    <div className="icc-dashed mb-2 rounded-md border px-2.5 py-1.5 text-[11px] border-[var(--icc-warn)]/40 text-[var(--icc-warn)]">
      Partial data — have {present.join(', ') || 'nothing'}; missing {missing.join(', ')}.
      {note ? ` ${note}` : ''}
    </div>
  );
}
