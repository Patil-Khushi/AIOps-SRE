import { Check, X } from 'lucide-react';
import { Modal } from '@/components/Modal';
import { useCountdown } from '@/hooks/useCountdown';
import type { DisplayOption } from '@/hooks/useHitlApply';

// The confirming step for "Approve" — separate from the inline apply flow
// RcaView already has, per the design spec's "Approve / Review / Reject"
// remediation block. Reject maps onto the same api.deny() call as Deny.
export function ApprovalModal({
  open,
  option,
  expiresAt,
  onApprove,
  onReject,
  onClose,
}: {
  open: boolean;
  option: DisplayOption | null;
  expiresAt?: string | null;
  onApprove: () => void;
  onReject: () => void;
  onClose: () => void;
}) {
  const countdown = useCountdown(expiresAt ?? new Date(Date.now() + 600_000).toISOString());
  if (!option) return null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Approve — ${option.title}`}
      footer={
        <>
          <button
            type="button"
            onClick={onReject}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-bad)]/40 bg-[var(--icc-bad)]/10 px-3 py-1.5 text-xs font-medium text-[var(--icc-bad)] transition hover:bg-[var(--icc-bad)]/20"
          >
            <X className="h-3.5 w-3.5" /> Reject
          </button>
          <button
            type="button"
            onClick={onApprove}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--icc-ok)]/40 bg-[var(--icc-ok)]/10 px-3 py-1.5 text-xs font-medium text-[var(--icc-ok)] transition hover:bg-[var(--icc-ok)]/20"
          >
            <Check className="h-3.5 w-3.5" /> Approve
          </button>
        </>
      }
    >
      <p className="text-sm text-[var(--icc-fg)]">{option.description}</p>
      <dl className="mt-3 space-y-1.5 text-xs">
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--icc-fg-muted)]">Blast radius</dt>
          <dd className="font-medium text-[var(--icc-fg)]">{option.blast_radius}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--icc-fg-muted)]">Rollback</dt>
          <dd className="text-right font-mono text-[11px] text-[var(--icc-fg)]">{option.rollback}</dd>
        </div>
        {expiresAt && (
          <div className="flex justify-between gap-2">
            <dt className="text-[var(--icc-fg-muted)]">Expires</dt>
            <dd className="font-medium text-[var(--icc-warn)]">{countdown}</dd>
          </div>
        )}
      </dl>
      <p className="mt-3 text-[11px] text-[var(--icc-fg-faint)]">
        Nothing changes until you approve. This is the same HITL gate as the RCA console — approving here
        applies the fix; rejecting cancels the request with no change made.
      </p>
    </Modal>
  );
}
