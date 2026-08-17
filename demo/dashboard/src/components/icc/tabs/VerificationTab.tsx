import type { Investigation } from '@/types/rca';
import { SectionShell } from '@/components/icc/SectionShell';

// Verification is a distinct state from execution — this plan renders even
// before anything has been applied; that's the point: success criteria are
// committed up front, not decided after the fact. There is no per-incident
// live verification-outcome endpoint today (documented backend gap), so this
// renders the plan only, honestly labeled rather than faking a result.
export function VerificationTab({ investigation }: { investigation: Investigation | null }) {
  if (!investigation) {
    return (
      <SectionShell state="unavailable" message="No verification plan available">
        <div />
      </SectionShell>
    );
  }
  const plan = investigation.verification;
  if (!plan) {
    return (
      <SectionShell state="empty" message="No verification plan was produced" reason="This investigation did not reach a stage that could commit to success criteria.">
        <div />
      </SectionShell>
    );
  }

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-[var(--icc-border)] p-3">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">
          Checks — re-verifies the exact signals that established the cause
        </p>
        <ul className="mt-1.5 space-y-1 text-xs text-[var(--icc-fg)]">
          {plan.checks.map((c, i) => (
            <li key={i}>· {c}</li>
          ))}
        </ul>
      </div>
      <div className="rounded-lg border border-[var(--icc-border)] p-3">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--icc-fg-muted)]">Success criteria</p>
        <ul className="mt-1.5 space-y-1 text-xs text-[var(--icc-fg)]">
          {plan.success_criteria.map((c, i) => (
            <li key={i}>· {c}</li>
          ))}
        </ul>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-[var(--icc-fg-muted)]">
        <span>windows: {plan.window_seconds.map((s) => `${s}s`).join(', ')}</span>
      </div>
      <p className="rounded-md border border-[var(--icc-warn)]/30 bg-[var(--icc-warn)]/10 px-3 py-2 text-xs text-[var(--icc-warn)]">
        If not resolved: {plan.if_not_resolved}
      </p>
      <p className="text-[11px] text-[var(--icc-fg-faint)]">
        "Action executed" and "system recovered" are tracked separately — this plan states the criteria; the
        remediation panel's status reflects whether a fix has been applied, not whether it worked.
      </p>
    </div>
  );
}
