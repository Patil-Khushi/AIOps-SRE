// Lifecycle stage (Detected -> ... -> Resolved) is derived client-side from
// whatever signals are actually available (D8 in the implementation plan) —
// no backend field for this exists yet. `unknown: true` marks every
// un-reached stage explicitly rather than guessing forward; LifecycleBar
// renders those in --icc-unknown, never as reached.

import type { Phase } from '@/hooks/useHitlApply';

export type LifecycleStage =
  | 'detected'
  | 'triaged'
  | 'investigating'
  | 'rca_ready'
  | 'approval'
  | 'remediating'
  | 'verifying'
  | 'resolved';

const ORDER: LifecycleStage[] = [
  'detected',
  'triaged',
  'investigating',
  'rca_ready',
  'approval',
  'remediating',
  'verifying',
  'resolved',
];

export interface LifecycleInputs {
  hasVerdict: boolean;
  rcaBusy?: boolean;
  hasRcaVerdict?: boolean;
  rcaActionable?: boolean;
  hitlPhase?: Phase;
  verifying?: boolean;
  resolved?: boolean;
}

export interface LifecycleResult {
  stage: LifecycleStage;
  reached: Set<LifecycleStage>;
  unknown: boolean;
}

export function deriveLifecycle(input: LifecycleInputs): LifecycleResult {
  let stage: LifecycleStage = 'detected';
  // True once a signal exists that could NOT settle whether a later stage was
  // reached (e.g. an RCA verdict with no `investigation`, so actionability
  // can't be read) — distinct from simply not having reached that stage yet.
  let unknown = false;

  if (input.hasVerdict) stage = 'triaged';
  if (input.rcaBusy) stage = 'investigating';
  if (input.hasRcaVerdict) {
    if (input.rcaActionable === undefined) unknown = true;
    stage = input.rcaActionable ? 'rca_ready' : 'triaged';
  }
  if (input.hitlPhase === 'awaiting') stage = 'approval';
  if (input.hitlPhase === 'deciding') stage = 'remediating';
  if (input.hitlPhase === 'success' && input.verifying) stage = 'verifying';
  if (input.resolved) stage = 'resolved';

  const idx = Math.max(0, ORDER.indexOf(stage));
  const reached = new Set(ORDER.slice(0, idx + 1));
  return { stage, reached, unknown };
}
