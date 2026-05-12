import { AlertTriangle, Loader2, Inbox } from 'lucide-react';

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

export function EmptyState({ label, hint, icon }: { label: string; hint?: string; icon?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 p-10 text-center">
      <div className="text-ink-400 dark:text-ink-500">
        {icon ?? <Inbox className="h-7 w-7" />}
      </div>
      <p className="text-sm font-medium text-ink-700 dark:text-ink-200">{label}</p>
      {hint && <p className="max-w-sm text-xs text-ink-500 dark:text-ink-400">{hint}</p>}
    </div>
  );
}
