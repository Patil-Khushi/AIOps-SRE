import { useEffect, useRef, useState } from 'react';
import type { RCAVerdict } from '@/types/api';
import type { Investigation } from '@/types/rca';
import { useHitlApply, phaseOf, type DisplayOption, type Phase } from '@/hooks/useHitlApply';
import { useToast } from '@/hooks/useToast';
import { isActionable } from '@/lib/rcaDerive';
import { RemediationCard } from './RemediationCard';
import { ApprovalModal } from './ApprovalModal';
import { SectionShell } from './SectionShell';

const TERMINAL_TOAST: Record<string, { text: (title: string) => string; tone: 'good' | 'bad' }> = {
  success: { text: (t) => `${t}: applied.`, tone: 'good' },
  denied: { text: (t) => `${t}: denied — no change made.`, tone: 'bad' },
  expired: { text: (t) => `${t}: approval expired — no change made.`, tone: 'bad' },
  blocked: { text: (t) => `${t}: blocked by policy.`, tone: 'bad' },
  error: { text: (t) => `${t}: failed to apply.`, tone: 'bad' },
};

// The UNCERTAIN rule is enforced HERE, once — not per card: when the
// investigation didn't settle on an actionable cause, no Approve affordance
// exists at all, and the two tied candidates (already shown in the
// RootCauseHero / Hypotheses tab) are what the operator gets instead of a fix
// button.
// Priority order for "the one phase that best represents this incident's
// remediation state" — used only to feed the lifecycle bar a single hint via
// onPhaseHint. Picking the most-advanced in-flight phase across options.
const PHASE_PRIORITY: Phase[] = ['deciding', 'awaiting', 'opening', 'success', 'error', 'blocked', 'denied', 'expired', 'idle'];

export function RemediationPanel({
  verdict,
  incidentId,
  investigation,
  onResolved,
  onPhaseHint,
}: {
  verdict: RCAVerdict;
  incidentId: string | null;
  investigation: Investigation | null;
  onResolved?: () => void;
  onPhaseHint?: (phase: Phase | undefined) => void;
}) {
  const { options, stateFor, apply, decide } = useHitlApply(verdict, incidentId, onResolved);
  const { push } = useToast();
  const [reviewing, setReviewing] = useState<DisplayOption | null>(null);
  const prevPhase = useRef<Record<string, Phase>>({});

  useEffect(() => {
    const phases = options.map((opt) => phaseOf(stateFor(opt.id).status));
    for (const [i, opt] of options.entries()) {
      const phase = phases[i];
      const prev = prevPhase.current[opt.id];
      if (prev !== phase && prev !== undefined) {
        const toast = TERMINAL_TOAST[phase];
        if (toast) push(toast.text(opt.title), toast.tone);
      }
      prevPhase.current[opt.id] = phase;
    }
    onPhaseHint?.(PHASE_PRIORITY.find((p) => phases.includes(p)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options.map((o) => stateFor(o.id).status).join(',')]);

  if (!isActionable(verdict.root_cause_status)) {
    return (
      <SectionShell
        state="empty"
        message="Evidence does not settle this — the next step is a human looking, not a fix executing."
        reason="No remediation is offered while the root cause is uncertain or evidence is insufficient. See the Hypotheses tab for the tied candidates."
      >
        <div />
      </SectionShell>
    );
  }

  const recoveryFor = (opt: DisplayOption) =>
    investigation?.recovery_options.find((r) => r.option_id === opt.id);

  return (
    <div>
      <ul className="space-y-2">
        {options.map((opt) => (
          <RemediationCard
            key={opt.id}
            option={opt}
            recovery={recoveryFor(opt)}
            phase={phaseOf(stateFor(opt.id).status)}
            error={stateFor(opt.id).error}
            onApply={() => apply(opt)}
            onReview={() => setReviewing(opt)}
          />
        ))}
      </ul>
      <ApprovalModal
        open={reviewing !== null}
        option={reviewing}
        onApprove={() => {
          if (reviewing) void decide(reviewing, 'approve');
          setReviewing(null);
        }}
        onReject={() => {
          if (reviewing) void decide(reviewing, 'deny');
          setReviewing(null);
        }}
        onClose={() => setReviewing(null)}
      />
    </div>
  );
}
