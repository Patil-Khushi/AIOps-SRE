import { CheckCircle2, XCircle, Info } from 'lucide-react';

export interface ToastVM {
  id: string;
  text: string;
  tone: 'good' | 'bad' | 'info';
}

// Re-themed port of demo/hitl-ui/src/App.tsx's ToastStack — dashboard had no
// toast/snackbar system at all before this. role="status" + aria-live so a
// screen reader announces it (SREs drive this with the keyboard).
export function ToastStack({ toasts }: { toasts: ToastVM[] }) {
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2" role="status" aria-live="polite">
      {toasts.map((t) => {
        const Icon = t.tone === 'good' ? CheckCircle2 : t.tone === 'bad' ? XCircle : Info;
        const color = t.tone === 'good' ? 'var(--icc-ok)' : t.tone === 'bad' ? 'var(--icc-bad)' : 'var(--icc-accent)';
        return (
          <div
            key={t.id}
            className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm shadow-lg"
            style={{ borderColor: `${color}66`, background: 'var(--icc-surface)', color: 'var(--icc-fg)' }}
          >
            <Icon className="h-4 w-4 flex-shrink-0" style={{ color }} />
            {t.text}
          </div>
        );
      })}
    </div>
  );
}
