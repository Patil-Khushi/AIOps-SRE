import axios from 'axios';

export type IncidentType =
  | 'infrastructure'
  | 'application'
  | 'network'
  | 'external_dependency'
  | 'change_related';

export interface ClassifierAuditMetadata {
  created_at?: string | null;
  created_by: string;
  decision_trace: string[];
  similar_incidents: {
    incident_key: string;
    incident_type: string;
    similarity: number;
    summary?: string;
  }[];
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

export interface PersistedClassification extends Classification {
  id: number;
  verdict_id?: number | null;
}

export interface EvalCheck {
  check: string;
  passed: boolean;
  detail: string;
}

export interface EvalCase {
  case_id: string;
  passed: boolean;
  incident_type_ok: boolean;
  duration_ms: number;
  checks: EvalCheck[];
}

export interface EvalResult {
  total_cases: number;
  passed_cases: number;
  accuracy_pct: number;
  misroute_cases: number;
  misroute_pct: number;
  ran_at: string;
  per_case: EvalCase[];
}

export interface MetricsResponse {
  eval: EvalResult | null;
  live: {
    total_classifications: number;
    avg_confidence: number | null;
  };
  running: boolean;
  llm_provider: string | null;
  checked_at: string;
}

export interface ClassificationsList {
  count: number;
  classifications: PersistedClassification[];
}

const http = axios.create({ timeout: 90_000 });

export const api = {
  metrics: () =>
    http.get<MetricsResponse>('/api/classifier/metrics').then((r) => r.data),
  classifications: (limit = 50, incident_type?: string) =>
    http
      .get<ClassificationsList>('/api/classifier/classifications', {
        params: { limit, incident_type },
      })
      .then((r) => r.data),
  // Eval runs ~5 LLM calls back-to-back. Override axios default 90 s timeout —
  // Azure GPT-5 cold-start can push the full run past it.
  evaluate: () =>
    http
      .post<MetricsResponse>('/api/classifier/evaluate', null, { timeout: 300_000 })
      .then((r) => r.data),
};
