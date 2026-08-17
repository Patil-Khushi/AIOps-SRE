import type { ComponentType } from 'react';
import { AlertTriangle, Tags, Search, Brain, ShieldAlert, Wrench, Activity, CheckCircle2 } from 'lucide-react';
import type { LifecycleStage } from '@/lib/lifecycle';
import { clsx } from '@/lib/format';

const STAGES: { stage: LifecycleStage; label: string; icon: ComponentType<{ className?: string }> }[] = [
  { stage: 'detected', label: 'Detected', icon: AlertTriangle },
  { stage: 'triaged', label: 'Triaged', icon: Tags },
  { stage: 'investigating', label: 'Investigating', icon: Search },
  { stage: 'rca_ready', label: 'RCA ready', icon: Brain },
  { stage: 'approval', label: 'Approval', icon: ShieldAlert },
  { stage: 'remediating', label: 'Remediating', icon: Wrench },
  { stage: 'verifying', label: 'Verifying', icon: Activity },
  { stage: 'resolved', label: 'Resolved', icon: CheckCircle2 },
];

// Detected -> ... -> Resolved. `unknown` (from deriveLifecycle, D8) renders
// every un-reached node in --icc-unknown rather than guessing forward — an
// un-reached stage is never shown as reached just because we can't tell.
export function LifecycleBar({
  stage,
  reached,
  unknown,
}: {
  stage: LifecycleStage;
  reached: Set<LifecycleStage>;
  unknown: boolean;
}) {
  return (
    <div className="flex items-center overflow-x-auto">
      {STAGES.map((s, i) => {
        const isReached = reached.has(s.stage) && !unknown;
        const isCurrent = s.stage === stage;
        const Icon = s.icon;
        return (
          <div key={s.stage} className="flex flex-1 items-center last:flex-none">
            <div className="flex flex-col items-center gap-1">
              <span
                className={clsx(
                  'flex h-8 w-8 flex-none items-center justify-center rounded-full border-2 transition-colors',
                  isCurrent
                    ? 'border-[var(--icc-accent)] bg-[var(--icc-accent-soft)] text-[var(--icc-accent)]'
                    : isReached
                      ? 'border-[var(--icc-ok)]/50 text-[var(--icc-ok)]'
                      : 'border-[var(--icc-unknown)]/40 text-[var(--icc-unknown)]',
                )}
                title={unknown && i > 0 ? 'Could not determine whether this stage was reached' : undefined}
              >
                <Icon className="h-4 w-4" />
              </span>
              <span
                className={clsx(
                  'whitespace-nowrap text-[10px] font-medium uppercase tracking-wide',
                  isCurrent ? 'text-[var(--icc-accent)]' : 'text-[var(--icc-fg-muted)]',
                )}
              >
                {s.label}
              </span>
            </div>
            {i < STAGES.length - 1 && (
              <span
                className={clsx(
                  'mx-1 h-px flex-1 min-w-[12px]',
                  isReached ? 'bg-[var(--icc-ok)]/40' : 'bg-[var(--icc-border)]',
                )}
                aria-hidden
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
