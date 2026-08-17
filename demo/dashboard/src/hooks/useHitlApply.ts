import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import type { RCAVerdict, BlastRadius, RankedFixStep, RemediationOption } from '@/types/api';

// ─── The HITL apply/approve state machine, shared by RcaView and the ───────
// Incident Command Center's remediation panel.
//
// Extracted out of RcaView.tsx (zero behavior change) so there is exactly ONE
// place in the app that drives api.applyRcaFix / executeOption / approve /
// deny / hitlOutcome — a second, independently-written HITL flow is exactly
// the kind of drift the platform's "HITL is enforced at the registry
// boundary, not in agent/UI logic" rule exists to prevent.
//
// Flow per option:
//   1. apply()  → opens the REQUIRED-HITL gate (api.applyRcaFix / executeOption)
//   2. decide() → api.approve / api.deny on the SAME approval id, inline
//   3. On approve: flag-flip options really set the flag off (+ resolution
//      verifier); other actions run through the gated execute seam.
//   4. onResolved() fires when a real flag flip executes so the caller can
//      drop the now-resolved incident from its list.

const OPERATOR = 'rca-console';

const SERVICE_FLAG: Record<string, string> = {
  payment: 'paymentFailure',
  paymentservice: 'paymentFailure',
  productcatalog: 'productCatalogFailure',
  'product-catalog': 'productCatalogFailure',
  productcatalogservice: 'productCatalogFailure',
  cart: 'cartFailure',
  cartservice: 'cartFailure',
  ad: 'adFailure',
  adservice: 'adFailure',
  recommendation: 'recommendationCacheFailure',
  recommendationservice: 'recommendationCacheFailure',
  email: 'emailMemoryLeak',
  emailservice: 'emailMemoryLeak',
  checkout: 'kafkaQueueProblems',
  checkoutservice: 'kafkaQueueProblems',
  frontend: 'imageSlowLoad',
  'frontend-proxy': 'imageSlowLoad',
};

function flagForService(service: string): string | null {
  return SERVICE_FLAG[service.toLowerCase().trim()] ?? null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

// A single option, normalized from either a RemediationOption (POST /api/rca) or
// a RankedFixStep (Incident Commander fallback). ``flag`` is set only when the
// option is a one-click flag flip; ``raw`` is the original RemediationOption used
// to drive the gated execute seam for non-flag actions.
export interface DisplayOption {
  id: string;
  title: string;
  description: string;
  blast_radius: BlastRadius;
  action_type: string;
  rollback: string;
  mttrMinutes: number | null;
  toolCapability: string | null;
  recommended: boolean;
  flag: string | null;
  variant: string;
  raw: RemediationOption | null;
}

// The active failure flag for THIS incident — the flag whose flip to 'off'
// actually clears the failure. Prefer a set_flag option's OWN flag (grounded
// against the firing scenario, so it's the right flag even for a service with
// several scenarios, e.g. payment → paymentFailure vs paymentUnreachable), then
// a set_flag step's flag, then the per-service default. EVERY option applies
// this flag, so approving any of them really remediates — no silent dry-runs.
function primaryFlagFor(v: RCAVerdict): string | null {
  for (const o of v.remediation_options ?? []) {
    if (o.action_type === 'set_flag') {
      const f = str(o.tool_args?.flag);
      if (f) return f;
    }
  }
  for (const s of v.ranked_fix_steps) {
    if (s.action_type === 'set_flag' && s.flag) return s.flag;
  }
  return flagForService(v.affected_service);
}

function optionsFromVerdict(v: RCAVerdict): DisplayOption[] {
  // Every option resolves to the incident's real failure flag (variant → off),
  // so approving ANY option flips it off — the actual, safe remediation in this
  // demo. Only options on a service we can't map to a flag fall back to the
  // gated execute seam (opt.raw + flag=null).
  const primaryFlag = primaryFlagFor(v);
  if (v.remediation_options && v.remediation_options.length > 0) {
    return v.remediation_options.map((o) => ({
      id: o.option_id,
      title: o.title,
      description: o.description,
      blast_radius: o.blast_radius,
      action_type: o.action_type,
      rollback: o.rollback,
      mttrMinutes: o.estimated_mttr_minutes,
      toolCapability: o.tool_capability,
      recommended: o.option_id === v.recommended_option_id,
      flag: primaryFlag,
      variant: 'off',
      raw: o,
    }));
  }
  return v.ranked_fix_steps.map((s: RankedFixStep, i: number) => ({
    id: `step-${i}`,
    title: `Fix step ${i + 1}`,
    description: s.description,
    blast_radius: s.blast_radius,
    action_type: s.action_type,
    rollback: s.rollback,
    mttrMinutes: null,
    toolCapability: s.action_type === 'set_flag' ? 'feature_flags.set_variant' : null,
    recommended: i === 0,
    flag: primaryFlag,
    variant: 'off',
    raw: null,
  }));
}

// Local status (not just backend outcome). 'opening' = firing the apply request;
// 'awaiting' = HITL gate open, showing inline Approve/Deny; 'deciding' = the
// approve/deny call + executor is running. The rest come from the outcome store.
export type RawStatus =
  | 'idle' | 'opening' | 'awaiting' | 'deciding'
  | 'executed' | 'dry_run_ok' | 'approved'
  | 'denied' | 'expired' | 'blocked' | 'refused' | 'pending_approval'
  | 'execution_failed' | 'unsupported' | 'error' | 'pending';
export type Phase = 'idle' | 'opening' | 'awaiting' | 'deciding' | 'success' | 'denied' | 'expired' | 'blocked' | 'error';

export function phaseOf(status: RawStatus): Phase {
  switch (status) {
    case 'idle': return 'idle';
    case 'opening': return 'opening';
    case 'awaiting': return 'awaiting';
    case 'deciding':
    case 'pending':
    case 'pending_approval': return 'deciding';
    case 'executed':
    case 'dry_run_ok':
    case 'approved': return 'success';
    case 'denied': return 'denied';
    case 'expired': return 'expired';
    case 'blocked':
    case 'refused': return 'blocked';
    default: return 'error'; // execution_failed | unsupported | error
  }
}

export interface ApplyState {
  status: RawStatus;
  error: string | null;
  approver: string | null;
  approvalId: string | null;
  dryRun: boolean;
}
const IDLE: ApplyState = { status: 'idle', error: null, approver: null, approvalId: null, dryRun: false };

export function useHitlApply(v: RCAVerdict, incidentId: string | null, onResolved?: () => void) {
  const options = optionsFromVerdict(v);
  const [applyById, setApplyById] = useState<Record<string, ApplyState>>({});

  useEffect(() => {
    setApplyById({});
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps

  const stateFor = (id: string): ApplyState => applyById[id] ?? IDLE;
  const patch = (id: string, next: Partial<ApplyState>) =>
    setApplyById((prev) => ({ ...prev, [id]: { ...(prev[id] ?? IDLE), ...next } }));

  // Poll every option whose gate is open or executing. Once terminal, stop; if a
  // real flag flip executed, tell the caller so it can drop the resolved incident.
  useEffect(() => {
    const active = Object.entries(applyById).filter(
      ([, s]) => s.approvalId && (s.status === 'awaiting' || s.status === 'deciding'),
    );
    if (active.length === 0) return;
    let alive = true;
    const timer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      active.forEach(async ([id, s]) => {
        if (!s.approvalId) return;
        try {
          const o = await api.hitlOutcome(s.approvalId);
          if (!alive || !o.status || o.status === 'pending') return;
          patch(id, { status: o.status as RawStatus, error: o.error ?? null, approver: o.approver ?? null });
          const opt = options.find((x) => x.id === id);
          if (opt?.flag && phaseOf(o.status as RawStatus) === 'success') onResolved?.();
        } catch {
          /* transient — keep polling */
        }
      });
    }, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [applyById]); // eslint-disable-line react-hooks/exhaustive-deps

  // Step 1 — fire the gated request; move to 'awaiting' so the inline Approve/Deny appears.
  const apply = async (opt: DisplayOption) => {
    patch(opt.id, { status: 'opening', error: null, approver: null });
    try {
      if (opt.flag) {
        const context: Record<string, unknown> = {
          service: v.affected_service,
          rca_verdict: v,
          timeout_seconds: 600,
        };
        if (incidentId) context.incident_id = incidentId;
        const res = await api.applyRcaFix(opt.flag, opt.variant, 'set_flag', undefined, context);
        patch(opt.id, { approvalId: res.approval_id, status: 'awaiting', dryRun: false });
      } else if (opt.raw) {
        const res = await api.executeOption(opt.raw, v.affected_service, {
          incidentId: incidentId ?? undefined,
          dryRun: true,
        });
        patch(opt.id, { approvalId: res.approval_id, status: 'awaiting', dryRun: true });
      } else {
        patch(opt.id, { status: 'error', error: 'This option has no automated executor — perform it manually.' });
      }
    } catch (e) {
      patch(opt.id, { status: 'error', error: e instanceof Error ? e.message : String(e) });
    }
  };

  // Step 2 — the HITL decision, INLINE (no separate Approvals page). Approving
  // unblocks the gate; the poller then reports executed → the flag is off.
  const decide = async (opt: DisplayOption, kind: 'approve' | 'deny') => {
    const s = stateFor(opt.id);
    if (!s.approvalId) return;
    patch(opt.id, { status: 'deciding', error: null });
    try {
      if (kind === 'approve') await api.approve(s.approvalId, OPERATOR, 'Approved from RCA console');
      else await api.deny(s.approvalId, OPERATOR, 'Denied from RCA console');
      // Leave it in 'deciding' — the poller flips it to the real outcome.
    } catch (e) {
      // Approval registry may not have the request yet (race) — let them retry.
      patch(opt.id, { status: 'awaiting', error: e instanceof Error ? e.message : String(e) });
    }
  };

  return { options, stateFor, apply, decide };
}
