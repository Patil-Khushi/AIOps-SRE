import { CheckCircle2, HelpCircle, EyeOff, CircleDot } from 'lucide-react';
import type { RootCauseStatus } from '@/types/rca';

const CONFIG: Record<RootCauseStatus, { label: string; icon: typeof CheckCircle2; color: string }> = {
  confirmed: { label: 'Root cause confirmed', icon: CheckCircle2, color: 'var(--icc-ok)' },
  probable: { label: 'Root cause probable', icon: CircleDot, color: 'var(--icc-accent)' },
  // Uncertain/insufficient-evidence are visually distinct from confirmed/probable —
  // both use --icc-unknown, never a "confident" color, because the product rule
  // is that UNCERTAIN must never look like a single confident root cause.
  uncertain: { label: 'Uncertain — evidence does not discriminate', icon: HelpCircle, color: 'var(--icc-unknown)' },
  insufficient_evidence: { label: 'Insufficient evidence', icon: EyeOff, color: 'var(--icc-unknown)' },
};

export function StatusVerdictChip({ status }: { status: RootCauseStatus }) {
  const cfg = CONFIG[status];
  const Icon = cfg.icon;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium"
      style={{ borderColor: `${cfg.color}66`, color: cfg.color, background: `${cfg.color}14` }}
    >
      <Icon className="h-3.5 w-3.5" /> {cfg.label}
    </span>
  );
}
