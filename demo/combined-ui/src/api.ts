import axios from 'axios';

// ─── shared contracts (mirror the agents' pydantic models) ──────────────────

export type Severity = 'Sev-1' | 'Sev-2' | 'Sev-3' | 'Sev-4';

export type IncidentType =
  | 'infrastructure'
  | 'application'
  | 'network'
  | 'external_dependency'
  | 'change_related';

export interface TriageAuditMetadata {
  created_at?: string | null;
  created_by: string;
  source_alerts: string[];
  decision_trace: string[];
}

export interface TriageVerdict {
  incident_id?: string | null;
  affected_service: string;
  customer_facing: boolean;
  severity: Severity;
  confidence_score: number;
  alert_summary: string;
  assigned_team: string;
  assigned_engineer?: string | null;
  recommended_runbook?: string | null;
  duplicate_alert_count: number;
  status: 'Active' | 'Suppressed';
  audit_metadata: TriageAuditMetadata;
}

export interface SimilarIncident {
  incident_key: string;
  incident_type: string;
  similarity: number;
  summary?: string;
}

export interface ClassifierAuditMetadata {
  created_at?: string | null;
  created_by: string;
  decision_trace: string[];
  similar_incidents: SimilarIncident[];
}

export interface Classification {
  incident_type: IncidentType;
  confidence: number;
  rationale: string;
  tags: string[];
  probable_root_cause: string;
  routing_team: string;
  on_call_engineer?: string | null;
  recommended_runbook?: string | null;
  dependencies: string[];
  similar_incident_ids: string[];
  audit_metadata: ClassifierAuditMetadata;
}

export interface CombinedResult {
  alert_id: string;
  affected_service: string;
  verdict: TriageVerdict;
  classification: Classification;
  verdict_id?: number | null;
}

// ─── fixtures ───────────────────────────────────────────────────────────────

export interface Fixture {
  id: string;
  description: string;
  input: Record<string, unknown>;
}

export interface FixturesResponse {
  agent?: string;
  version?: string;
  description?: string;
  cases: Fixture[];
}

const http = axios.create({ timeout: 90_000 });

export const api = {
  fixtures: () =>
    http.get<FixturesResponse>('/api/combined/fixtures').then((r) => r.data),
  // A run does an embedding search + up to two LLM calls (RA-001 severity/summary
  // + RA-002 classify). With a real Azure/Anthropic backend a cold start can be
  // slow, so we lift the timeout above the axios default.
  run: (alert: Record<string, unknown>) =>
    http
      .post<CombinedResult>('/api/combined/run', { alert }, { timeout: 180_000 })
      .then((r) => r.data),
};
