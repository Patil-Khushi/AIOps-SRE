// Type contracts shared by the backend FastAPI service.
// Keep these in sync with `agents/alert_triage/models.py` and
// `demo/ui/server.py` route shapes.

import type { Investigation, RootCauseStatus } from './rca';

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
  // True when the affected service is on the customer-facing path. Derived by
  // RA-001 and surfaced so the UI can flag customer impact at a glance.
  customer_facing?: boolean;
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

// RA-002 Incident Classifier output — now produced by the Alert Triage agent
// as part of the same run (mirrors agents/alert_triage/classifier_models.py).
export type IncidentType =
  | 'infrastructure'
  | 'application'
  | 'network'
  | 'external_dependency'
  | 'change_related';

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
}

// Combined response from POST /api/triage and each entry in POST /api/triage/live.
// The Alert Triage agent triages AND classifies, so the bundle carries both the
// verdict and the classification.
export interface TriageResult {
  verdict: TriageVerdict;
  ticket: TicketRecord;
  classification?: Classification | null;
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
  // The RCA Agent now also drives remediation (the former standalone PRS-001
  // Remediation Recommender folded in): a ranked set of executable options the
  // operator picks + approves, each REQUIRED-HITL-gated. Present when the verdict
  // came from POST /api/rca; absent on the Incident Commander path, where the UI
  // falls back to rendering ranked_fix_steps.
  remediation_options?: RemediationOption[];
  recommended_option_id?: string | null;
  // Additive fields the backend has always sent (agents/rca_agent/models.py)
  // but this type previously omitted — silently dropping the entire
  // Investigation payload. root_cause_status is the platform-computed status
  // (never llm_stated_confidence as the headline number); investigation is
  // null on the offline/eval path or whenever the deterministic stages could
  // not run.
  root_cause_status: RootCauseStatus;
  investigation?: Investigation | null;
  llm_stated_confidence?: number | null;
}

// Log Correlation (RA-007) — mirrors agents/log_correlation/models.py.
// One observation on the shared timeline (a log line, trace-span summary, or
// metric reading reduced to an error signature + the raw sample).
export type SignalSource = 'logs' | 'traces' | 'metrics';

// Provenance: 'live' = pulled from Loki/Jaeger/Prometheus; 'synthetic' =
// deterministic fallback when those backends were unreachable; 'mixed' = both.
export type EvidenceProvenance = 'live' | 'synthetic' | 'mixed';

export interface CorrelatedSignal {
  source: SignalSource;
  signature: string;
  timestamp: string;
  severity: string;
  sample: string;
}

export interface CorrelationAuditMetadata {
  created_at: string;
  created_by: string;
  signal_source: EvidenceProvenance;
  decision_trace: string[];
}

// ── RA-007 evidence-pack sub-objects ────────────────────────────────────────
//
// These six were computed by the agent and returned by /api/correlate long
// before they were declared here, so the console silently dropped them: the
// dependency graph, the confidence rule trace, the retrieved history, the change
// context, the grouped timeline and the structured evidence. Declared now so the
// page can render what the agent already produces.

export interface DependencyEdgeMetadata {
  provider?: string | null;
  protocol?: string | null;
  call_rate?: number | null;
  error_rate?: number | null;
  latency_p95_ms?: number | null;
  observed_at?: string | null;
  confidence?: number | null;
}

export interface DependencyGraphNode {
  service: string;
  depth: number;
  relation: string;
  health?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface DependencyGraphEdge {
  source: string;
  target: string;
  metadata?: DependencyEdgeMetadata | null;
}

export interface DependencyGraph {
  root: string;
  nodes: DependencyGraphNode[];
  edges: DependencyGraphEdge[];
  downstream: string[];
  /** Empty means "not observable from this provider", NOT "nothing calls this" —
   *  see coverage_note. Rendering it as "none" would be a factual error. */
  upstream: string[];
  max_depth_reached: number;
  truncated: boolean;
  coverage_note?: string | null;
  provider?: string | null;
  built_at?: string | null;
  root_answered: boolean;
}

export interface ConfidenceContribution {
  rule_id: string;
  reason?: string | null;
  delta?: number | null;
}

export interface ConfidenceUnappliedRule {
  rule_id: string;
  reason: string;
  /** What the score would have gained had the rule applied — this is what makes
   *  a weak correlation diagnosable rather than just low. */
  potential_delta?: number | null;
}

export interface ConfidenceBreakdown {
  score: number;
  base: number;
  explanation: string;
  contributors: ConfidenceContribution[];
  deductions: ConfidenceContribution[];
  unapplied: ConfidenceUnappliedRule[];
  rule_trace: string[];
  capped: boolean;
}

export interface IncidentResolution {
  resolved: boolean;
  resolution_summary?: string | null;
  resolved_at?: string | null;
  time_to_resolve_minutes?: number | null;
  resolved_by?: string | null;
  /** Cause established for THAT past incident. Deliberately not called
   *  root_cause — it is history, not a verdict on the current incident. */
  recorded_cause?: string | null;
  ticket_ref?: string | null;
  runbook_ref?: string | null;
}

export interface IncidentMatch {
  incident_id: string;
  similarity_score: number;
  title?: string | null;
  occurred_at?: string | null;
  matching_signatures: string[];
  matching_services: string[];
  matching_topology: string[];
  resolution?: IncidentResolution | null;
  provider: string;
  match_explanation?: string | null;
}

export interface SimilarIncidents {
  matches: IncidentMatch[];
  /** Which backend answered. A semantic hit from a populated vector store and a
   *  keyword hit from a 15-row demo corpus are not equivalent evidence. */
  provider?: string | null;
  providers_attempted: string[];
  /** A similarity score is uninterpretable without the population searched. */
  corpus_size?: number | null;
  coverage_note?: string | null;
}

export interface ChangeRecord {
  change_id: string;
  change_type: string;
  source: string;
  timestamp?: string | null;
  service?: string | null;
  summary?: string | null;
  url?: string | null;
  rollback_status?: string | null;
  feature_flags?: Record<string, string>;
}

export interface ChangeContext {
  records: ChangeRecord[];
  sources_collected: string[];
  /** As material as the records: an empty record list means "nothing changed"
   *  only when every source actually answered. */
  sources_unavailable: string[];
  coverage_note?: string | null;
}

export interface IncidentTimelineEntry {
  timestamp: string;
  event: string;
  service?: string | null;
  severity: string;
  source: string;
  related_evidence_ids: string[];
  occurrences: number;
  group_id?: string | null;
}

export interface IncidentTimeline {
  correlation_id: string;
  service: string;
  entries: IncidentTimelineEntry[];
}

export interface EvidenceTelemetry {
  sample?: string | null;
  occurrences?: number | null;
  sources_agreeing: string[];
  first_seen?: string | null;
  last_seen?: string | null;
}

export interface EvidenceTopologyContext {
  relation?: string | null;
  implicated_service?: string | null;
  depth?: number | null;
  path: string[];
  upstream_complete?: boolean | null;
}

export interface CorrelationEvidence {
  evidence_id: string;
  correlation_id: string;
  timestamp: string;
  source: SignalSource;
  service: string;
  signal_type: string;
  normalized_signature: string;
  severity: string;
  confidence: number;
  supporting_telemetry?: EvidenceTelemetry | null;
  topology_context?: EvidenceTopologyContext | null;
}

// The correlated evidence pack RA-007 emits. suspected_dependencies is the
// catalog's "suspect components"; the whole object is the "evidence pack".
// Everything below audit_metadata is optional: each is opt-in behind its own
// env flag, and `null` means "not attempted" — distinct from an empty list
// meaning "looked and found nothing".
export interface CorrelationResult {
  service: string;
  summary: string;
  timeline: CorrelatedSignal[];
  top_signatures: string[];
  suspected_dependencies: string[];
  confidence: number;
  audit_metadata: CorrelationAuditMetadata;
  evidence?: CorrelationEvidence[] | null;
  incident_timeline?: IncidentTimeline | null;
  confidence_breakdown?: ConfidenceBreakdown | null;
  similar_incidents?: SimilarIncidents | null;
  dependency_graph?: DependencyGraph | null;
  deployment_context?: ChangeContext | null;
}

// RA-008 Incident Commander — mirrors agents/incident_commander/models.py.
// Chains the reactive flow + RCA into one coordinated Sev-1/Sev-2 response.
export interface IcTimelineEntry {
  ts: string;
  stage: string;   // detected | triage | classify | correlate | ticket | notify | rca | comms | handoff
  detail: string;
}

// Derived MTTA/MTTR-style durations, all measured from detection (T0). A field
// is null when that stage did not run (e.g. handoff on a non-engaged incident).
export interface IncidentMetrics {
  detected_at: string;
  time_to_triage_seconds?: number | null;
  time_to_notify_seconds?: number | null;  // detect → on-call paged (MTTA)
  time_to_handoff_seconds?: number | null;
  total_coordination_seconds?: number | null;
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
  metrics?: IncidentMetrics | null;
}

export interface IncidentCommandResult {
  engaged: boolean;                       // true only for Sev-1/Sev-2
  severity: Severity;
  affected_service: string;
  reactive: TriageResult;                 // the /api/triage bundle (verdict + ticket + …)
  rca: RCAVerdict | null;                 // null below Sev-2
  timeline: IcTimelineEntry[];
  metrics: IncidentMetrics | null;
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

// ─── RA-004 audit event log + simulation detail/comparison (#213 / #217) ─────

export type AuditEventType =
  | 'STEP_STARTED'
  | 'STEP_SIMULATED'
  | 'GATE_CHECKED'
  | 'HITL_REQUESTED'
  | 'HITL_APPROVED'
  | 'STEP_EXECUTED'
  | 'STEP_FAILED'
  | 'STEP_BLOCKED'
  | 'STEP_ROLLED_BACK';

export interface AuditEventMetadata {
  reason?: string;
  gate_type?: string; // '' | 'none' | 'required'
  approval_id?: string;
  // Backend uses extra="allow", so unknown keys may appear.
  [key: string]: unknown;
}

export interface AuditEvent {
  seq: number;
  incident_id: string;
  runbook_id: string;
  step_id: string;
  timestamp: string;
  status: AuditEventType;
  metadata: AuditEventMetadata;
}

// The full dry-run prediction captured per step.
export interface SimulationDetail {
  predicted_actions: string[];
  warnings: string[];
  estimated_duration_ms: number | null;
  predicted_side_effects: string[];
  summary: string;
}

// Structured diff of prediction vs. actual execution (side-effects + duration).
export interface SimulationComparison {
  matched: boolean;
  divergences: string[];
  predicted_side_effects: string[];
  actual_side_effects: string[];
  unexpected_side_effects: string[];
  missing_side_effects: string[];
  estimated_duration_ms: number | null;
  actual_duration_ms: number | null;
  duration_delta_ms: number | null;
}

export interface RunbookStepRecord {
  name: string;
  action: string;
  destructive: boolean;
  status: RunbookStepStatus;
  simulate?: Record<string, unknown> | null;
  simulation?: SimulationDetail | null;
  comparison?: SimulationComparison | null;
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
  // Read-only dry-run preview computed up-front (mock simulate provider). Now
  // carries the full prediction (#213) alongside the legacy preview/changes.
  simulate?: {
    preview?: string;
    changes?: unknown[];
    error?: string;
    predicted_actions?: string[];
    warnings?: string[];
    estimated_duration_ms?: number | null;
    predicted_side_effects?: string[];
    summary?: string;
  } & Record<string, unknown>;
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
  // True when the operator picked this runbook instead of the auto-selected match.
  overridden?: boolean;
  planned_steps: PlannedStep[];
  timeout_seconds: number;
}

// One library runbook for the picker (GET /api/runbook-executor/runbooks).
export interface RunbookLibraryStep {
  name: string;
  action: string;
  destructive: boolean;
}
export interface RunbookLibraryItem {
  id: string;
  title: string;
  service: string;
  severity: string | null;
  tags: string[];
  matches_service: boolean;
  recommended: boolean;
  steps: RunbookLibraryStep[];
}
export interface RunbookLibraryResponse {
  count: number;
  recommended: string | null;
  runbooks: RunbookLibraryItem[];
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
  // Append-only, ordered audit trail of the run (#213). Ordered by `seq`.
  audit_events?: AuditEvent[];
  // Post-run verification: re-reads the flags the runbook reset and confirms
  // the injected scenario actually cleared (⑤ Verify stage).
  verification?: {
    status: 'verified' | 'unverified' | 'skipped';
    reason?: string;
    checks: { name: string; flag: string; variant: string | null; ok: boolean; available: boolean }[];
  };
}

// ─── Remediation Recommender (PRS-001) ──────────────────────────────────────
// Mirrors agents/remediation_recommender/models.py and POST /api/remediation.

export type RemediationActionType =
  | 'set_flag'
  | 'rollback_deploy'
  | 'scale'
  | 'restart'
  | 'circuit_breaker'
  | 'manual';

export type OptionSource = 'rca_fix_step' | 'playbook_pattern' | 'operator_seeded';

export interface RemediationOption {
  option_id: string;
  title: string;
  description: string;
  action_type: RemediationActionType;
  blast_radius: BlastRadius;
  blast_radius_score: number; // 1..5, lower = safer
  rollback: string;
  rollback_tested: boolean;
  confidence: number;
  estimated_mttr_minutes: number;
  requires_hitl: true;
  rationale: string;
  // Tool seam handoff: how Auto-Healer would execute this option.
  tool_capability: string | null;
  tool_args: Record<string, unknown>;
  source: OptionSource;
}

export interface RemediationVerdict {
  affected_service: string;
  incident_summary: string;
  options: RemediationOption[];          // sorted; index 0 is the top pick
  recommended_option_id: string;
  auto_pick_eligible: false;
  confidence_score: number;
  requires_hitl: true;
  rationale: string;
  audit_metadata: RCAAuditMetadata;
}

// ─── Auto-Healer Lite (PRS-002) ──────────────────────────────────────────────
// Mirrors agents/auto_healer_lite/models.py and the
// /api/demo/auto-heal/execute route shape.

// Terminal status of one execution attempt. The outcome poll returns the
// synthetic 'pending' while the agent thread is still blocked at the gate.
export type ExecutionStatus =
  | 'pending'
  | 'refused'
  | 'pending_approval'
  | 'blocked'
  | 'approved'
  | 'dry_run_ok'
  | 'executed'
  | 'execution_failed';

export interface GateDecisionSummary {
  allowed: boolean;
  level: string;                 // AutonomyLevel.value, e.g. "required"
  reason: string;
  approver: string | null;
  approval_id: string | null;
  approval_status: string | null;
}

export interface ExecutionVerdict {
  status: ExecutionStatus;
  request_id?: string;
  option_id?: string;
  affected_service?: string;
  dry_run?: boolean;
  requires_hitl?: true;
  decision?: GateDecisionSummary;
  tool_capability?: string | null;
  tool_args?: Record<string, unknown>;
  tool_result?: Record<string, unknown> | null;
  would_execute?: boolean;
  error?: string | null;
  rationale?: string;
  audit_metadata?: RCAAuditMetadata;
}

// POST /api/demo/auto-heal/execute — returned immediately (the gated execution
// runs on a pool thread; poll /api/demo/auto-heal/outcome/{id} for the verdict).
export interface ExecuteRunResponse {
  approval_id: string;
  status: 'pending';
  option_id: string | null;
  affected_service: string;
  dry_run: boolean;
  timeout_seconds: number;
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

