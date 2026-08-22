// Adapters between the production Runbook Executor contract and the shapes the
// existing Runbook Executor page already renders.
//
// The page's six-stage tracker, RunDetail and step table were written against the
// legacy RunbookRunResponse / RunbookOutcome shapes and work well. Rather than
// rewriting them for the new candidates → plan → execute flow, the new responses are
// projected onto those shapes here. One small module to keep the mapping in one place
// (and to keep the page from growing a second rendering path that can drift).
import type {
  RunbookExecutionRecord,
  RunbookExecutorStatus,
  RunbookIncidentPayload,
  RunbookOutcome,
  RunbookPlanResponse,
  RunbookResolutionStatus,
  RunbookRunResponse,
  RunbookStepRecord,
  VerdictRecord,
} from '@/types/api';

/** Severity token the backend matcher expects: 'Sev-2' → 'sev2'. */
export function sevToken(severity: string): string {
  return severity.toLowerCase().replace(/[^a-z0-9]/g, '');
}

/**
 * The Prometheus alertname behind a verdict, or '' when there isn't one.
 *
 * Triage records its source alert ids as `PROM-<alertname>-na` (see
 * `server.py::_synthetic_alerts_for_active_scenarios` and the canonical adapter), so the
 * alert name is recoverable even though `VerdictRecord` has no field for it. It is worth
 * recovering: without it the backend cannot compare the failure-category or alert facets,
 * and every runbook for the service ties on score — the operator gets a list with no
 * ranking signal, which is the opposite of the point.
 *
 * A fixture- or CloudWatch-shaped id that does not match the pattern yields '', and the
 * backend then treats the alert as unknown (a warning, not a mismatch).
 */
export function alertNameFrom(v: VerdictRecord): string {
  for (const id of v.audit_metadata?.source_alerts ?? []) {
    const match = /^PROM-(.+)-na$/.exec(id);
    if (match) return match[1];
  }
  return '';
}

/**
 * The incident facts the API needs, taken from a triaged verdict.
 *
 * `incident_status` matters: a Suppressed verdict is a *deduplicated* alert, not a
 * resolved one, so it is reported as such rather than as inactive — otherwise the
 * backend's stale-incident guard would refuse to remediate every duplicate.
 *
 * `detected_at` is the verdict's own creation time, not "now": the backend's
 * aged-out-incident guard can only fire if it is told when the incident was detected,
 * and defaulting to now would quietly disable it.
 */
export function incidentPayload(v: VerdictRecord): RunbookIncidentPayload {
  return {
    incident_id: v.incident_id || `verdict-${v.id}`,
    service: v.affected_service,
    severity: sevToken(v.severity),
    alert_name: alertNameFrom(v),
    summary: v.alert_summary,
    incident_status: v.status === 'Suppressed' ? 'suppressed' : 'active',
    detected_at: v.audit_metadata?.created_at || undefined,
  };
}

/**
 * Legacy execution status for a durable execution state, for the stage tracker.
 *
 * A completed execution is 'resolved' ONLY once the Resolution Verifier has said so
 * (`record.verification_status === 'pass'`) — never from `state==='completed'` alone.
 * That used to collapse to 'resolved' unconditionally, which lit the tracker's Verify
 * stage green and showed a "resolved" header chip before the verifier had run at all;
 * the executor is not the authority on recovery (§26).
 */
function resolutionStatus(record: RunbookExecutionRecord): RunbookResolutionStatus {
  switch (record.state) {
    case 'completed': {
      const verdict = (record.verification_status || '').trim().toLowerCase();
      if (verdict === 'pass' || verdict === 'passed') return 'resolved';
      if (verdict === 'fail' || verdict === 'failed') return 'failed';
      return 'awaiting_verification';
    }
    case 'rolled_back':
      return 'rolled_back';
    case 'failed':
      return 'failed';
    case 'aborted':
      return 'denied';
    default:
      return 'pending';
  }
}

/** Project a plan response onto the shape the page's stage tracker consumes. */
export function planToRun(
  plan: RunbookPlanResponse,
  payload: RunbookIncidentPayload,
): RunbookRunResponse {
  return {
    approval_id: plan.execution_id ?? '',
    status: plan.selected_runbook_id ? 'pending' : 'no_runbook',
    service: payload.service,
    incident_id: payload.incident_id ?? '',
    selected_runbook: plan.selected_runbook_id,
    runbook_title: plan.dry_run?.runbook_title ?? null,
    matched_on: {
      service: payload.service,
      severity: payload.severity ?? null,
      tags: payload.tags ?? [],
    },
    overridden: plan.selected_by !== 'auto',
    planned_steps: (plan.dry_run?.steps ?? []).map((step) => ({
      name: step.step_id,
      action: step.action_id,
      destructive: step.destructive,
      simulate: {
        preview: step.simulation?.summary,
        changes: [],
        predicted_actions: step.simulation?.predicted_actions,
        warnings: step.warnings,
        estimated_duration_ms: step.simulation?.estimated_duration_ms ?? null,
        predicted_side_effects: step.simulation?.predicted_side_effects,
        summary: step.simulation?.summary,
      },
    })),
    timeout_seconds: 0,
  };
}

/**
 * Project a persisted execution onto the legacy outcome shape.
 *
 * `verification` is intentionally NOT synthesised from the execution: whether the
 * incident recovered is the Resolution Verifier's verdict, and the record carries it
 * separately (`verification_status`). Faking a "verified" here is exactly the
 * conflation the executor is built to avoid.
 */
export function executionToOutcome(record: RunbookExecutionRecord): RunbookOutcome {
  const steps = (record.steps ?? []) as RunbookStepRecord[];
  return {
    status: resolutionStatus(record),
    approval_id: record.approval_id ?? undefined,
    selected_runbook: record.runbook_id,
    runbook_title: record.runbook_id,
    steps,
    reason: record.reason,
    steps_total: steps.length,
    steps_executed: steps.filter((s) => s.status === 'executed').length,
    destructive_steps: steps.filter((s) => s.destructive).length,
    audit_events: record.audit_events,
  };
}

/** What to tell the operator happens next, in the executor's own words. */
export function nextActionLabel(status: RunbookExecutorStatus | string): string {
  switch (status) {
    case 'EXECUTED':
      return 'Execution completed — waiting for resolution verification';
    case 'NO_RUNBOOK':
      return 'No applicable runbook — routing to RCA';
    case 'NOT_APPLICABLE':
      return 'No runbook applies to this incident — routing to RCA';
    case 'AMBIGUOUS':
      return 'Applicability undetermined — an SRE or RCA must decide';
    case 'BLOCKED':
      return 'Blocked by a safety, policy or prerequisite check';
    case 'FAILED':
      return 'Execution failed — escalating to RCA';
    case 'ROLLED_BACK':
      return 'Execution rolled back — escalating to RCA';
    default:
      return '';
  }
}
