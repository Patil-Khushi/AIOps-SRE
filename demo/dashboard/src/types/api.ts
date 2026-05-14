// Type contracts shared by the backend FastAPI service.
// Keep these in sync with `agents/alert_triage/models.py` and
// `demo/ui/server.py` route shapes.

export type Severity = 'Sev-1' | 'Sev-2' | 'Sev-3' | 'Sev-4';
export type Status = 'Active' | 'Suppressed';

export interface PrometheusAlert {
  alert_id: string;
  service: string;
  metric: string;
  value: number;
  timestamp: string;
  source: string;
  severity_hint?: string | null;
  labels: Record<string, string>;
  annotations: Record<string, string>;
}

export interface AuditMetadata {
  created_at: string;
  created_by: string;
  source_alerts: string[];
  decision_trace: string[];
}

export interface TriageVerdict {
  affected_service: string;
  severity: Severity;
  confidence_score: number;
  alert_summary: string;
  assigned_team: string;
  assigned_engineer?: string | null;
  recommended_runbook?: string | null;
  duplicate_alert_count: number;
  status: Status;
  audit_metadata: AuditMetadata;
}

// RA-003 Auto-Ticketing output (mirrors agents/auto_ticketing/models.py:TicketRecord).
export interface TicketRecord {
  created: boolean;
  ticket_id?: string | null;
  system: 'servicenow' | 'mock' | 'none';
  urgency?: number | null;
  short_description?: string | null;
  channel_notified?: string | null;
  notification_sent: boolean;
  audit_metadata: string[];
}

// Combined response from POST /api/triage and each entry in POST /api/triage/live.
export interface TriageResult {
  verdict: TriageVerdict;
  ticket: TicketRecord;
}

export interface HealthResponse {
  status: string;
  llm_provider: string | null;       // probed provider (= request that succeeded), null on failure
  llm_model: string | null;          // probed model id, null on failure
  llm_ok: boolean;                   // true if the cached ping returned successfully
  llm_error: string | null;          // exception message when llm_ok=false
  llm_latency_ms: number | null;     // round-trip on success
  llm_cached: boolean;               // true if this is a cached probe (60s success / 10s failure TTL)
  registered_capabilities: string[];
  prometheus_reachable: boolean;
  jaeger_reachable: boolean;
  checked_at: string;
}

export interface LiveAlertsResponse {
  count: number;
  alerts: PrometheusAlert[];
  raw_count: number;
}

// ─── Chatops (RA-005 sink) ─────────────────────────────────────────────────
// Wire shape produced by aiops/tools/chatops/models.py::to_record and
// streamed over WS /ws/chatops.

export type ChatSeverity = 'info' | 'p3' | 'p2' | 'p1' | 'p0';

export interface ChatNotification {
  timestamp: string;
  channel: string;
  severity: ChatSeverity;
  title: string;
  body: string;
  incident_id: string | null;
  service: string | null;
  mentions: string[];
}

export type ScenarioCategory = 'errors' | 'latency' | 'capacity' | 'infra';

export interface Scenario {
  scenario_id: string;
  flag: string;
  alert: string;
  service: string;
  title: string;
  description: string;
  eta_seconds: number;
  category?: ScenarioCategory;
  variant_on?: string;
  current_variant: string;  // not just on/off — e.g. "100%", "10sec"
}

export interface ScenariosResponse {
  scenarios: Scenario[];
}

export interface TopologyNode {
  id: string;
  label: string;
}

export interface TopologyEdge {
  source: string;
  target: string;
  call_count?: number;
}

export interface TopologyResponse {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  source: 'jaeger' | 'unknown';
}

export interface PodStatus {
  name: string;
  ready: string;          // "2/2"
  status: string;         // "Running" | "Pending" | ...
  restarts: number;
  age: string;            // "3h12m"
}

export interface SystemPodsResponse {
  namespace: string;
  pods: PodStatus[];
  total: number;
  ready_count: number;
  not_ready_count: number;
}

