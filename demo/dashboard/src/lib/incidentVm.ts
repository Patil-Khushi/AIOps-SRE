import type { PrometheusAlert, Severity, Status, TriageVerdict, VerdictRecord } from '@/types/api';
import { deriveLifecycle, type LifecycleStage } from './lifecycle';

export interface IncidentRowVM {
  id: string;
  incidentId: string | null;
  service: string;
  severity: Severity;
  summary: string;
  team: string;
  status: Status;
  createdAt: string;
  firing: boolean;
  lifecycle: LifecycleStage;
  lifecycleUnknown: boolean;
  // Needed verbatim by api.rca() — the incident row IS the triage verdict,
  // reconstructed from the persisted VerdictRecord row.
  triageVerdict: TriageVerdict;
}

// Alert labels and triage verdicts name the same service two different ways
// — "order-service" (bare, from scenario YAMLs/truth files) and
// "ecommerce-order-service" (OTEL_SERVICE_NAME, carried into Prometheus/Loki/
// Jaeger labels and some synthetic alerts) — see evidence.py's
// service_pod_prefix() for the backend's side of this same normalization.
// Comparing raw strings here let the two forms of one real service dodge
// dedup and dodge the firing-alert match below.
function normalizeServiceKey(service: string): string {
  const prefix = 'ecommerce-';
  return (service.startsWith(prefix) ? service.slice(prefix.length) : service).toLowerCase();
}

// One row per affected service, not one row per persisted verdict. Each test
// cycle (inject → triage → recover → re-inject) writes a fresh VerdictRecord
// for the same underlying fault, so without this the list accumulates one
// duplicate-looking row per cycle instead of tracking a single open incident
// per service. ``verdicts`` is already newest-first (GET /api/verdicts), so
// keeping the first occurrence per service keeps the most recent one.
function latestPerService(verdicts: VerdictRecord[]): VerdictRecord[] {
  const seen = new Set<string>();
  const out: VerdictRecord[] = [];
  for (const v of verdicts) {
    const key = normalizeServiceKey(v.affected_service);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(v);
  }
  return out;
}

export function toIncidentRows(verdicts: VerdictRecord[], firingAlerts: PrometheusAlert[]): IncidentRowVM[] {
  const firingServices = new Set(firingAlerts.map((a) => normalizeServiceKey(a.service)));
  // Only services with a CURRENTLY firing alert are "open incidents" — a
  // persisted verdict from an earlier, since-recovered fault is history, not
  // an active incident, and must not linger in this list forever.
  return latestPerService(verdicts)
    .filter((v) => firingServices.has(normalizeServiceKey(v.affected_service)))
    .map((v, idx) => {
    const lc = deriveLifecycle({ hasVerdict: true });
    return {
      id: v.incident_id ?? `verdict-${v.id ?? idx}`,
      incidentId: v.incident_id,
      service: v.affected_service,
      severity: v.severity,
      summary: v.alert_summary,
      team: v.assigned_team,
      status: v.status,
      createdAt: v.audit_metadata.created_at,
      firing: firingServices.has(normalizeServiceKey(v.affected_service)),
      lifecycle: lc.stage,
      lifecycleUnknown: lc.unknown,
      triageVerdict: {
        affected_service: v.affected_service,
        severity: v.severity,
        confidence_score: v.confidence_score,
        alert_summary: v.alert_summary,
        assigned_team: v.assigned_team,
        assigned_engineer: v.assigned_engineer,
        recommended_runbook: v.recommended_runbook,
        duplicate_alert_count: v.duplicate_alert_count,
        status: v.status,
        audit_metadata: v.audit_metadata,
        // Re-sent to POST /api/rca so a Suppressed verdict (no real
        // ServiceNow incident_id) still has a stable identity for RCA
        // persistence — see TriageVerdict.cluster_key's docstring.
        cluster_key: v.cluster_key,
      },
    };
  });
}

// The Debug button's auto-typed investigation prompt (D9 / the design spec's
// "Debug opens the RCA panel and auto-types the incident's investigation
// prompt" behavior). One editable place.
export function buildInvestigationPrompt(row: IncidentRowVM): string {
  return `Investigate the ${row.severity} incident on ${row.service}: ${row.summary}`;
}
