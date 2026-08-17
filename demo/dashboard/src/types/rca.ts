// The Investigation tree — mirrors agents/rca_agent/investigation/models.py
// field-for-field. Enums are string unions matching the Python StrEnum values
// exactly. Python `tuple[X, ...]` -> TS `X[]`; `X | None` -> `X | null`.
//
// Several Python `@property` accessors (BlastRadiusReport.directly_affected,
// RiskAssessment.unassessed, RootCauseStatus.is_actionable, etc.) do NOT
// survive `model_dump()` — they must be re-derived client-side. See
// src/lib/rcaDerive.ts, which documents the Python source line for each.
//
// CausalChain exists in the Python models but is not a field on Investigation
// and is referenced nowhere in agents/rca_agent/ — deliberately not modeled
// here.

export type EvidenceStance =
  | 'supports'
  | 'contradicts'
  | 'checked_absent'
  | 'unavailable'
  | 'not_requested'
  | 'failed';

export type RootCauseStatus = 'confirmed' | 'probable' | 'uncertain' | 'insufficient_evidence';

export type MemoryStatus = 'new' | 'unverified' | 'verified' | 'trusted' | 'superseded' | 'invalidated';

export type BaselineStatus = 'available' | 'partial' | 'unavailable' | 'stale';

export type ImpactState =
  | 'directly_affected'
  | 'indirectly_affected'
  | 'observed_healthy'
  | 'not_observed'
  | 'unknown';

export type TemporalRelation = 'precedes_onset' | 'at_onset' | 'follows_onset' | 'unknown';

export type TimelineSource =
  | 'alert'
  | 'metrics'
  | 'logs'
  | 'traces'
  | 'k8s_events'
  | 'deployment'
  | 'configuration'
  | 'dependency'
  | 'remediation'
  | 'verification';

export interface IncidentScope {
  incident_id: string;
  affected_service: string;
  severity: string;
  user_visible_symptom: string;
  alert_name?: string | null;
  alert_summary?: string | null;
  affected_endpoint?: string | null;
  affected_workload?: string | null;
  onset_at?: string | null;
  observed_at?: string | null;
  current_state: string;
  initial_blast_radius: string[];
  correlation_id?: string | null;
}

export interface RcaTimelineEvent {
  timestamp: string;
  source: TimelineSource;
  service: string;
  event: string;
  severity: string;
  temporal_relation: TemporalRelation;
  is_change: boolean;
  occurrences: number;
  evidence_ids: string[];
}

export interface IncidentTimelineView {
  events: RcaTimelineEvent[];
  onset_at?: string | null;
  sources_present: string[];
  sources_unavailable: string[];
  truncated: boolean;
  coverage_note?: string | null;
}

export interface BaselineComparison {
  metric: string;
  status: BaselineStatus;
  current_value?: number | null;
  baseline_value?: number | null;
  deviation_ratio?: number | null;
  window_note?: string | null;
  is_abnormal?: boolean | null;
}

export interface InvestigationCompleteness {
  per_source: Record<string, string>;
  overall: number;
  critical_gaps: string[];
  note?: string | null;
}

export interface MemoryProvenance {
  source_incident_ids: string[];
  evidence_ids: string[];
  recorded_at?: string | null;
  verification_result?: string | null;
  human_confirmed: boolean;
  human_corrected: boolean;
  service_version?: string | null;
  topology_version?: string | null;
  action_ref?: string | null;
  recovery_result?: string | null;
}

export interface MemoryReliability {
  occurrences: number;
  verified_correct: number;
  rejected: number;
  freshness_days?: number | null;
  superseded_by: string[];
}

export interface HistoricalPrior {
  memory_id: string;
  status: MemoryStatus;
  similarity: number;
  recorded_cause?: string | null;
  matched_on: string[];
  reliability: MemoryReliability;
  provenance: MemoryProvenance;
}

export interface HistoricalInfluence {
  level: 'none' | 'weak' | 'moderate' | 'strong';
  priors_considered: number;
  priors_eligible: number;
  priors_applied: string[];
  overridden_by_current_evidence: string[];
  changed_ranking: boolean;
  note?: string | null;
}

export interface EvidenceItem {
  evidence_id: string;
  stance: EvidenceStance;
  statement: string;
  source: string;
  signature?: string | null;
  observation_id?: string | null;
  section_status?: string | null;
  topology_relation?: string | null;
  sources_agreeing: string[];
  occurrences: number;
  temporal_relation: TemporalRelation;
  observed_at?: string | null;
}

export interface ScoreFactor {
  rule_id: string;
  description: string;
  delta: number;
  triggered_by: string[];
}

export interface UnappliedRule {
  rule_id: string;
  reason: string;
  potential_delta: number;
}

export interface HypothesisScore {
  score: number;
  factors: ScoreFactor[];
  unapplied: UnappliedRule[];
  rule_trace: string[];
  capped: boolean;
  explanation: string;
}

export interface Hypothesis {
  hypothesis_id: string;
  label: string;
  mechanism: string;
  candidate_component?: string | null;
  category: string;
  origin: 'catalog' | 'llm' | 'historical' | 'operator';
  action_hint?: string | null;
}

export interface EvidenceMatrix {
  hypothesis: Hypothesis;
  supporting: EvidenceItem[];
  contradicting: EvidenceItem[];
  checked_absent: EvidenceItem[];
  gaps: EvidenceItem[];
  baseline: BaselineComparison[];
  priors: HistoricalPrior[];
  score: HypothesisScore | null;
  contradiction_search_performed: boolean;
}

export interface ServiceImpact {
  service: string;
  state: ImpactState;
  relation?: string | null;
  hops?: number | null;
  evidence_ids: string[];
  rationale: string;
}

export interface BlastRadiusReport {
  impacts: ServiceImpact[];
  affected_endpoints: string[];
  topology_available: boolean;
  note?: string | null;
}

export interface RiskAssessment {
  level: 'low' | 'medium' | 'high' | 'unknown';
  causes_downtime: boolean | null;
  interrupts_active_requests: boolean | null;
  risks_data_loss: boolean | null;
  risks_duplicate_transactions: boolean | null;
  affects_downstream: boolean | null;
  affects_upstream: boolean | null;
  destroys_evidence: boolean | null;
  reversible: boolean;
  rollback_available: boolean;
  safer_alternative?: string | null;
  concerns: string[];
  rationale: string;
}

export interface RecoveryOption {
  option_id: string;
  description: string;
  addresses_hypothesis_id?: string | null;
  why_it_addresses_the_cause: string;
  expected_effect: string;
  expected_recovery_seconds?: number | null;
  changes: string;
  dependencies: string[];
  rollback: string;
  blast_radius: 'low' | 'medium' | 'high';
  risk: RiskAssessment;
  confidence: number;
  action_key?: string | null;
  grounded: boolean;
  executable: boolean;
  requires_hitl: true;
}

export interface VerificationPlan {
  checks: string[];
  success_criteria: string[];
  window_seconds: number[];
  if_not_resolved: string;
}

export interface Investigation {
  scope: IncidentScope;
  timeline: IncidentTimelineView;
  completeness: InvestigationCompleteness;
  baselines: BaselineComparison[];
  matrices: EvidenceMatrix[];
  status: RootCauseStatus;
  confidence: number;
  selected_hypothesis_id?: string | null;
  discriminated: boolean;
  historical_influence: HistoricalInfluence;
  blast_radius: BlastRadiusReport | null;
  recovery_options: RecoveryOption[];
  verification: VerificationPlan | null;
  budget?: string | null;
  notes: string[];
}

// ─── RCA chat (agents/rca_agent/chat.py) ───────────────────────────────────

export interface SuggestedAction {
  kind: 'reanalyze' | 'open_tab' | 'review_option';
  reason: string;
  tab?: string | null;
  recovery_option_id?: string | null;
}

export interface HistoricalIncidentRef {
  incident_id: string;
  similarity: number;
  recorded_fix?: string | null;
}

export interface ChatAnswer {
  answer: string;
  answerable: boolean;
  citations: string[];
  missing: string[];
  caveats: string[];
  referenced_hypotheses: string[];
  suggested_actions: SuggestedAction[];
  source: 'model' | 'deterministic';
  fabricated_citations: number;
  warnings: string[];
  history_truncated: boolean;
  historical_incidents: HistoricalIncidentRef[];
}

export interface RcaChatMessageOut {
  role: 'agent';
  text: string;
  answer: ChatAnswer;
}

export interface RcaVerdictSnapshot {
  affected_service: string;
  root_cause: string;
  root_cause_status: RootCauseStatus;
  confidence_score: number;
  selected_hypothesis_id: string | null;
}

export interface RcaChatResponse {
  run_id: string;
  message: RcaChatMessageOut;
  verdict_snapshot: RcaVerdictSnapshot;
}

export interface RcaChatHistoryResponse {
  run_id: string;
  messages: Array<{ role: 'user' | 'assistant'; text: string }>;
  verdict_snapshot: RcaVerdictSnapshot;
}

export interface RcaChatByIncidentResponse {
  run_id: string | null;
  has_session: boolean;
  verdict?: Record<string, unknown> | null;
}

// ─── RCA progress (agents/rca_agent/progress.py) ───────────────────────────

export type RcaStage =
  | 'received'
  | 'change_correlation'
  | 'evidence'
  | 'context_pack'
  | 'memory_recall'
  | 'action_vocabulary'
  | 'hypotheses'
  | 'explaining'
  | 'complete'
  | 'failed'
  | 'chat_turn_started'
  | 'chat_turn_answered';

export type StageOutcome = 'started' | 'ok' | 'degraded' | 'failed';

export interface RcaProgressEvent {
  run_id: string;
  seq: number;
  stage: RcaStage;
  outcome: StageOutcome;
  label: string;
  detail: string;
  elapsed_ms: number;
  data: Record<string, unknown>;
  at: string;
}
