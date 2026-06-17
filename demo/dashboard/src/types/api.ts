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

// RCA Agent (PRS-008 ★) — mirrors agents/rca_agent/models.py.
export type BlastRadius = 'low' | 'medium' | 'high';

export type FixActionType = 'set_flag' | 'rollback_deploy' | 'manual';

export interface RankedFixStep {
  description: string;
  blast_radius: BlastRadius;
  rollback: string;
  // Schema-enforced True by pydantic Literal[True]; included for completeness.
  requires_hitl: true;
  // Machine-readable action the platform executor follows. 'set_flag' steps
  // carry the flagd flag to flip; everything else is advisory-only in v0.
  action_type: FixActionType;
  flag: string | null;
  variant: string;
}

export interface RCAAuditMetadata {
  created_at: string;
  created_by: string;
  decision_trace: string[];
}

export interface RCAVerdict {
  affected_service: string;
  root_cause: string;
  ranked_fix_steps: RankedFixStep[];
  confidence_score: number;
  audit_metadata: RCAAuditMetadata;
}

// RA-008 Incident Commander — mirrors agents/incident_commander/models.py.
// Chains the reactive flow + RCA into one coordinated Sev-1/Sev-2 response.
export interface IcTimelineEntry {
  ts: string;
  stage: string;   // triage | classify | correlate | ticket | notify | rca | comms | handoff
  detail: string;
}

export interface PostmortemSeed {
  affected_service: string;
  severity: string;
  incident_summary: string;
  incident_type?: string | null;
  ticket_id?: string | null;
  root_cause?: string | null;
  confidence_score?: number | null;
  ranked_fix_steps: RankedFixStep[];
  contributing_signals: string[];
  timeline: IcTimelineEntry[];
}

export interface IncidentCommandResult {
  engaged: boolean;                       // true only for Sev-1/Sev-2
  severity: Severity;
  affected_service: string;
  reactive: TriageResult;                 // the /api/triage bundle (verdict + ticket + …)
  rca: RCAVerdict | null;                 // null below Sev-2
  timeline: IcTimelineEntry[];
  postmortem_seed: PostmortemSeed | null; // null below Sev-2
  handoff_requested: boolean;
  audit_metadata: RCAAuditMetadata;
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
  // Authoritative human-response mode RA-005 decided (page | notify | log) —
  // carried on the live WS frame (to_record) and persisted on the row.
  // Optional for frames/rows written before it existed. Prefer this over a
  // severity-only guess: it also reflects business hours.
  response_mode?: string;
  title: string;
  body: string;
  incident_id: string | null;
  service: string | null;
  mentions: string[];
}

// Row shape of GET /api/notifications (the SQL-persisted RA-005 history,
// aiops/state/repository.py::_notification_row_to_dict). Used to backfill
// the Notifications page across server restarts — the WS feed's in-memory
// replay ring starts empty after every uvicorn restart.
export interface PersistedNotification {
  id: number;
  verdict_id: number | null;
  routed_at: string | null;
  channel: string;
  target: string;
  chat_severity: string;
  response_mode: string | null;
  title: string;
  body: string;
  service: string | null;
  actions: string[];
  reason: string;
  audit_trace: string[];
}

export interface NotificationsResponse {
  count: number;
  notifications: PersistedNotification[];
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

// ── HITL approvals (the human-in-the-loop) ──────────────────────────────────
export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired';

export interface ApprovalRecord {
  id: string;
  action: string;                       // capability, e.g. "rca.fix_step.execute"
  context: Record<string, unknown>;     // flag/variant/deployment/reason/...
  status: ApprovalStatus;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  approver: string | null;
  reason: string;
}

export interface ApprovalsResponse {
  count: number;
  approvals: ApprovalRecord[];
}

// ─── Knowledge Synthesizer (PRS-007) ───────────────────────────────────────
// Mirrors agents/knowledge_synthesizer/models.py and the
// demo/ui/knowledge_routes.py route shapes.

export type ReviewStatus = 'draft' | 'pending_review' | 'published' | 'rejected';

export interface TimelineEntry {
  ts: string | null;
  event: string;
  source_agent: string | null;
}

export interface Postmortem {
  affected_service: string;
  what_broke: string;
  root_cause: string;
  timeline: TimelineEntry[];
  fix: string;
  impact: string;
  confidence_score: number;
}

export interface RunbookSuggestion {
  mode: 'new' | 'update';
  target_id: string;
  title: string;
  body_markdown: string;
}

export interface KBArticleOut {
  incident_id: string | null;
  title: string;
  summary: string;
  body: string;
  service: string;
  tags: string[];
  quality_score: number;
  status: ReviewStatus;
  related_runbook_id: string | null;
}

export interface DedupDecision {
  action: 'create' | 'duplicate' | 'skip_idempotent';
  matched_article_id: number | null;
  similarity: number;
  method: 'embedding' | 'signature' | 'incident_id';
}

export interface SynthesisResult {
  incident_id: string | null;
  affected_service: string;
  status: ReviewStatus;
  root_cause: string;
  dedup_action: string;
  runbook_mode: string;
  related_runbook_id: string | null;
  kb_article_id: number | null;
  quality_score: number;
  redaction_summary: string;
  created_at: string;
  postmortem: Postmortem;
  kb_article: KBArticleOut;
  runbook_suggestion: RunbookSuggestion;
  dedup: DedupDecision;
}

// Row shape from GET /api/kb (the persisted KBArticleRow as a dict).
export interface KBArticleRow {
  id: number;
  incident_id: string | null;
  title: string;
  summary: string;
  body: string;
  service: string;
  tags: string[];
  status: ReviewStatus;
  quality_score: number;
  related_runbook_id: string | null;
  approval_id: string | null;
  approved_by: string | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
  audit_metadata: Record<string, unknown>;
}

export interface KbListResponse {
  count: number;
  articles: KBArticleRow[];
}

// ─── Runbook Executor (RA-004) ──────────────────────────────────────────────
// Mirrors agents/runbook_executor/models.py and the
// /api/demo/runbook-executor/* route shapes.

export type RunbookStepStatus =
  | 'executed'
  | 'denied'
  | 'failed'
  | 'rolled_back'
  | 'skipped';

// Final resolution of a run. 'pending' is the synthetic status the outcome
// poll returns while the agent thread is still blocked at the HITL gate.
export type RunbookResolutionStatus =
  | 'pending'
  | 'resolved'
  | 'rolled_back'
  | 'denied'
  | 'failed'
  | 'no_runbook';

export interface RunbookStepRecord {
  name: string;
  action: string;
  destructive: boolean;
  status: RunbookStepStatus;
  simulate?: Record<string, unknown> | null;
  executed?: Record<string, unknown> | null;
  rolled_back: boolean;
  rollback?: Record<string, unknown> | null;
  error?: string | null;
}

export interface RunbookIncident {
  incident_id: string;
  service: string;
  severity?: string | null;
  tags: string[];
}

export interface PlannedStep {
  name: string;
  action: string;
  destructive: boolean;
  // Read-only dry-run preview computed up-front (mock simulate provider).
  simulate?: { preview?: string; changes?: unknown[]; error?: string } & Record<string, unknown>;
}

// POST /api/demo/runbook-executor/run — returned immediately (runbook selected
// + dry-run previewed synchronously; the gated execution runs on a pool thread).
export interface RunbookRunResponse {
  approval_id: string;
  status: 'pending' | 'no_runbook';
  service: string;
  incident_id: string;
  selected_runbook: string | null;
  runbook_title: string | null;
  // Optional: older backend builds omit it — the UI falls back to `service`.
  matched_on?: { service: string; severity: string | null; tags: string[] };
  planned_steps: PlannedStep[];
  timeout_seconds: number;
}

// GET /api/verdicts — persisted triage verdicts (newest-first). Each injected
// failure, once triaged, lands here with its assigned severity.
export interface VerdictRecord {
  id: number;
  cluster_key: string | null;
  incident_id: string | null;
  affected_service: string;
  severity: Severity;
  confidence_score: number;
  alert_summary: string;
  assigned_team: string;
  assigned_engineer: string | null;
  recommended_runbook: string | null;
  duplicate_alert_count: number;
  status: Status;
  audit_metadata: AuditMetadata;
}

export interface VerdictsResponse {
  count: number;
  verdicts: VerdictRecord[];
}

// GET /api/demo/auto-heal/outcome/{id} — the parked RunbookExecution, or
// { status: 'pending' } until the agent thread finishes.
export interface RunbookOutcome {
  status: RunbookResolutionStatus;
  approval_id?: string;
  incident?: RunbookIncident;
  selected_runbook?: string | null;
  runbook_title?: string | null;
  steps?: RunbookStepRecord[];
  rollback_artifacts?: Record<string, unknown>[];
  reason?: string;
  steps_total?: number;
  steps_executed?: number;
  destructive_steps?: number;
}

// GET /api/approvals/{id} — ApprovalRequest.to_record().
export type ApprovalState = 'pending' | 'approved' | 'denied' | 'expired';

export interface ApprovalRecord {
  id: string;
  action: string;
  context: Record<string, unknown>;
  status: ApprovalState;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  approver: string | null;
  reason: string;
}

