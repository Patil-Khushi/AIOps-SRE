import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ShieldAlert, CheckCircle2, Undo2, HeartPulse, ArrowRight } from 'lucide-react';
import { setConsoleAgent } from '@/lib/consoleScope';
import type { RCAVerdict, BlastRadius, RankedFixStep, RemediationOption } from '@/types/api';
import { clsx } from '@/lib/format';

// ─── Shared RCA result renderer ─────────────────────────────────────────────
//
// Draws a root-cause verdict and lets a human CHOOSE which fix step to run:
// root cause + confidence, a single-select list of ranked fix steps (each with
// a tested rollback), and a "Send to Auto-Healer" hand-off for the chosen step.
//
// RCA does NOT execute the fix. Analysis + human selection live here; the actual
// resolution — dry-run, then the REQUIRED-HITL live execution — is the
// Auto-Healer's job (PRS-002). Selecting a step and sending it hands a
// RemediationOption to the Auto-Healer console, which owns the single approval
// and flips the flag. Imported by both the RCA Agent console (PRS-008 ★) and the
// Incident Commander console (RA-008) so the two never drift.
//
// ``incidentId`` is the ServiceNow incident number for the verdict; it rides
// along on the hand-off so the Auto-Healer can forward it to the resolution
// verifier after a live execution.

const BLAST_RADIUS_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

// 1..5 blast-radius score the Auto-Healer / gate reason strings expect.
const BLAST_SCORE: Record<BlastRadius, number> = { low: 1, medium: 3, high: 5 };

// Maps an affected service to the flagd failure flag whose flip is the real,
// reversible remediation. Only services with a known flag get an executable
// hand-off — everything else stays advisory-only.
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
};

function flagForService(service: string): string | null {
  return SERVICE_FLAG[service.toLowerCase().trim()] ?? null;
}

// A step is executable when it flips a known feature flag. Everything else is
// advisory — the operator carries it out manually.
function stepFlag(step: RankedFixStep): string | null {
  return step.action_type === 'set_flag' && step.flag ? step.flag : null;
}

export function RcaView({ v, incidentId }: { v: RCAVerdict; incidentId: string | null }) {
  const navigate = useNavigate();
  const steps = v.ranked_fix_steps;
  // Default the selection to the first executable step (so the safest remediable
  // action is pre-highlighted); fall back to the first step.
  const firstExecutable = steps.findIndex((s) => stepFlag(s));
  const [selectedStep, setSelectedStep] = useState(firstExecutable >= 0 ? firstExecutable : 0);

  const chosen: RankedFixStep | undefined = steps[selectedStep];
  // The flag the chosen step would flip. Fall back to the service's known flag
  // so a set_flag step with a missing flag field still resolves to something.
  const flag = chosen
    ? (stepFlag(chosen) ?? (chosen.action_type === 'set_flag' ? flagForService(v.affected_service) : null))
    : null;
  const fixVariant = chosen?.variant ?? 'off';

  // New verdict → reset the selection to its safest executable step.
  useEffect(() => {
    const idx = steps.findIndex((s) => stepFlag(s));
    setSelectedStep(idx >= 0 ? idx : 0);
  }, [v]); // eslint-disable-line react-hooks/exhaustive-deps

  // Hand the chosen fix step to the Auto-Healer as a RemediationOption. The
  // Auto-Healer console shows it with dry-run/live and runs the single
  // HITL-gated execution — RCA never flips the flag itself.
  const sendToHealer = () => {
    if (!chosen || !flag) return;
    const option: RemediationOption = {
      option_id: `rca-${v.affected_service}-${selectedStep}`,
      title: `Disable failure flag ${flag}`,
      description: chosen.description,
      action_type: 'set_flag',
      blast_radius: chosen.blast_radius,
      blast_radius_score: BLAST_SCORE[chosen.blast_radius],
      rollback: chosen.rollback,
      rollback_tested: true,
      confidence: v.confidence_score,
      estimated_mttr_minutes: 2,
      requires_hitl: true,
      rationale: v.root_cause,
      tool_capability: 'feature_flags.set_variant',
      tool_args: { flag, variant: fixVariant },
      source: 'rca_fix_step',
    };
    setConsoleAgent('auto-healer');
    navigate('/agents/auto-healer', {
      state: {
        option,
        affectedService: v.affected_service,
        incidentId,
        rootCause: v.root_cause,
      },
    });
  };

  return (
    <div className="space-y-3 text-sm">
      <div>
        <div className="flex items-baseline justify-between gap-2">
          <p className="card-title !text-[10px]">Root cause</p>
          <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">
            confidence {(v.confidence_score * 100).toFixed(0)}%
          </span>
        </div>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-900 dark:text-ink-50">{v.root_cause}</p>
      </div>

      <div>
        <div className="flex items-baseline justify-between gap-2">
          <p className="card-title !text-[10px]">Ranked fix steps ({steps.length})</p>
          <span className="text-[10px] text-ink-500 dark:text-ink-400">select a step to send</span>
        </div>
        <ol className="mt-2 space-y-2">
          {steps.map((step, i) => {
            const executable = !!stepFlag(step);
            const isSelected = i === selectedStep;
            return (
              <li key={i}>
                <button
                  type="button"
                  onClick={() => setSelectedStep(i)}
                  aria-pressed={isSelected}
                  className={clsx(
                    'w-full rounded-md border p-2.5 text-left transition-colors',
                    isSelected
                      ? '!border-accent bg-accent/5 ring-1 ring-accent/30'
                      : 'border-ink-200 bg-ink-50/50 hover:border-accent/50 dark:border-ink-700 dark:bg-ink-800/30',
                  )}
                >
                  <div className="flex items-start gap-2">
                    {/* Radio indicator — makes the single-select nature obvious. */}
                    <span
                      className={clsx(
                        'mt-0.5 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full border',
                        isSelected ? 'border-accent' : 'border-ink-300 dark:border-ink-600',
                      )}
                    >
                      {isSelected && <span className="h-2 w-2 rounded-full bg-accent" />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-1.5">
                        <span className="flex-shrink-0 rounded bg-ink-200 px-1.5 text-[10px] font-bold text-ink-700 dark:bg-ink-700 dark:text-ink-200">
                          {i + 1}
                        </span>
                        <p className="text-sm leading-snug text-ink-900 dark:text-ink-50">{step.description}</p>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <span className={clsx('chip', BLAST_RADIUS_STYLE[step.blast_radius])}>blast: {step.blast_radius}</span>
                        <span className="chip !border-accent/40 !text-accent">
                          <ShieldAlert className="mr-1 inline h-3 w-3" /> HITL required
                        </span>
                        {executable ? (
                          <span className="chip !border-ok/40 !text-ok" title="Executable by the Auto-Healer">
                            <CheckCircle2 className="mr-1 inline h-3 w-3" /> auto: set {step.flag}→{step.variant}
                          </span>
                        ) : (
                          <span className="chip !border-ink-300/60 !text-ink-500 dark:!border-ink-600 dark:!text-ink-400" title="No automated executor — perform manually">
                            manual
                          </span>
                        )}
                      </div>
                      <div className="mt-1.5 flex items-start gap-1 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
                        <Undo2 className="mt-0.5 h-3 w-3 flex-shrink-0 text-ink-500 dark:text-ink-400" />
                        <span><span className="text-ink-500 dark:text-ink-400">rollback:</span> {step.rollback}</span>
                      </div>

                      {/* The hand-off renders inline under the SELECTED step. */}
                      {isSelected && (
                        flag ? (
                          <div className="mt-2 rounded-md border border-accent/40 bg-accent/5 p-2.5">
                            <div className="flex items-center justify-between gap-3">
                              <div className="min-w-0">
                                <p className="card-title !text-[10px]">Resolve this step</p>
                                <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
                                  Auto-Healer runs it (dry-run → live) through the HITL gate: set{' '}
                                  <span className="font-mono text-ink-700 dark:text-ink-200">{flag}</span> →{' '}
                                  <span className="font-mono text-ink-700 dark:text-ink-200">{fixVariant}</span>.
                                </p>
                              </div>
                              {/* Stop the parent step-select button from also firing. */}
                              <span
                                role="button"
                                tabIndex={0}
                                onClick={(e) => { e.stopPropagation(); sendToHealer(); }}
                                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); sendToHealer(); } }}
                                className={clsx(
                                  'inline-flex flex-shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition',
                                  'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20',
                                )}
                              >
                                <HeartPulse className="h-3.5 w-3.5" /> Send to Auto-Healer <ArrowRight className="h-3.5 w-3.5" />
                              </span>
                            </div>
                          </div>
                        ) : (
                          <p className="mt-2 rounded-md border border-ink-200 bg-ink-50/50 p-2 text-[11px] text-ink-500 dark:border-ink-700 dark:bg-ink-800/30 dark:text-ink-400">
                            This step has no automated executor — perform it manually, then verify recovery.
                          </p>
                        )
                      )}
                    </div>
                  </div>
                </button>
              </li>
            );
          })}
        </ol>
      </div>

      <details>
        <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
          RCA decision trace ({v.audit_metadata.decision_trace.length} steps)
        </summary>
        <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
          {v.audit_metadata.decision_trace.map((line, i) => (
            <li key={i} className="leading-relaxed">{i + 1}. {line}</li>
          ))}
        </ol>
      </details>
    </div>
  );
}
