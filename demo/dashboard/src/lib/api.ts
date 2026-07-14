import axios, { AxiosError } from 'axios';
import type {
  ApprovalRecord,
  ApprovalsResponse,
  CorrelationResult,
  ExecuteRunResponse,
  ExecutionVerdict,
  HealthResponse,
  IncidentCommandResult,
  KbListResponse,
  KBArticleRow,
  LiveAlertsResponse,
  NotificationsResponse,
  PrometheusAlert,
  RCAVerdict,
  RemediationOption,
  RemediationVerdict,
  RunbookLibraryResponse,
  RunbookOutcome,
  RunbookRunResponse,
  ScenariosResponse,
  VerdictsResponse,
  SynthesisResult,
  SystemPodsResponse,
  TopologyResponse,
  TriageResult,
  TriageVerdict,
} from '@/types/api';

// Axios instance — uses Vite proxy in dev, same-origin in prod (served from /dashboard).
// Default timeout is generous (600 s / 10 min) because /api/triage/live runs the agent's
// 8-stage pipeline (including LLM calls + embedding inference) for every firing alert.
// First run: embedding model load ~120s, subsequent runs ~10–30s. Even with backend
// parallelizing per-alert triage via asyncio.gather, first-run can exceed 90s.
export const http = axios.create({
  baseURL: '',
  timeout: 600_000,  // 10 minutes — first run embedding load + LLM cold-start
  headers: { 'Content-Type': 'application/json' },
});

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
  }
}

function unwrap<T>(p: Promise<{ data: T }>): Promise<T> {
  return p.then((r) => r.data).catch((err: AxiosError<{ detail?: string }>) => {
    const status = err.response?.status ?? 0;
    const detail = err.response?.data?.detail ?? err.message;
    throw new ApiError(status, String(detail), err.response?.data);
  });
}

// ── War-room shapes (agents/notification_assembler models — RA-005+006) ──
export interface InvitedSME {
  handle: string;
  name?: string | null;
  team?: string | null;
  reason: string;
  source: string;
  slack_user_id?: string | null;
  invite_status?: string | null;
  attendance?: string | null; // invited | joined | declined
}
export interface ContextPackItem {
  label: string;
  value: string;
  source?: string | null;
}
export interface TimelineEvent {
  at: string;
  event: string;
}
export interface WarRoomAssembly {
  assembled: boolean;
  channel: string;
  title: string;
  chat_severity: string;
  invited: InvitedSME[];
  context_pack: ContextPackItem[];
  timeline: TimelineEvent[];
  reason: string;
  audit_trace: string[];
  assembled_at: string;
  bridge_status: string;
  bridge_provider?: string | null;
  bridge_channel_id?: string | null;
  bridge_url?: string | null;
  meeting_url?: string | null;
}
export interface WarRoomTryRequest {
  affected_service: string;
  severity: string;
  assigned_team: string;
  assigned_engineer?: string | null;
  alert_summary?: string | null;
  recommended_runbook?: string | null;
  status: string;
  incident_id?: string | null;
  create_bridge?: boolean;
}
// The single notification this incident produced (RA-005+006 RoutingDecision).
// Embedded on the incident feed row now that notification + war room are one
// agent, so the combined page renders the message and the room from one row.
export interface IncidentNotification {
  chat_severity: string;
  channel: string;
  title: string;
  body: string;
  mentions: string[];
  actions: string[];
  reason: string;
  response_mode?: string;
  category_display?: string | null;
  assignee?: string | null;
  assignee_name?: string | null;
  assignee_email?: string | null;
}
export interface WarRoomFeedRow {
  id: string;
  status: string; // open | in_call | resolved | no_room
  assembled: boolean;
  channel: string;
  severity: string;
  chat_severity: string;
  service: string;
  team: string;
  sme_count: number;
  reason: string;
  bridge_url?: string | null;
  bridge_status?: string | null;
  assembled_at: string;
  assembly: WarRoomAssembly;
  notification?: IncidentNotification | null;
}
export interface WarRoomRecentResponse {
  count: number;
  war_rooms: WarRoomFeedRow[];
}
export interface WarRoomMetrics {
  total_seen: number;
  assembled: number;
  suppressed_or_minor: number;
  open: number;
  resolved: number;
  avg_smes: number | null;
  checked_at: string;
}

export const api = {
  health:      () => unwrap<HealthResponse>(http.get('/api/health')),
  liveAlerts:  () => unwrap<LiveAlertsResponse>(http.get('/api/live-alerts')),
  scenarios:   () => unwrap<ScenariosResponse>(http.get('/api/scenarios')),
  injectScenario: (id: string) =>
    unwrap<unknown>(http.post(`/api/scenarios/${id}/inject`)),
  resetScenario: (id: string) =>
    unwrap<unknown>(http.post(`/api/scenarios/${id}/reset`)),
  resetAllScenarios: () =>
    unwrap<{ reset_count: number; touched: { flag: string; from: string; to: string }[] }>(
      http.post('/api/scenarios/reset-all'),
    ),
  triage: (alert: PrometheusAlert) =>
    unwrap<TriageResult>(http.post('/api/triage', { alert })),
  triageLive: () =>
    unwrap<{ count: number; results: TriageResult[] }>(http.post('/api/triage/live')),
  rca: (triageVerdict: TriageVerdict, scenarioId?: string) =>
    unwrap<RCAVerdict>(
      http.post('/api/rca', { triage_verdict: triageVerdict, scenario_id: scenarioId ?? null }),
    ),
  // RA-007 Log Correlation — pulls logs (Loki) + traces (Jaeger) + metrics
  // (Prometheus) for a service/window and returns a correlated evidence pack.
  // signal_source on the result is 'live' when the backends were reachable,
  // 'synthetic' when the deterministic fallback ran.
  correlate: (service: string, windowMinutes = 15) =>
    unwrap<CorrelationResult>(
      http.post('/api/correlate', { service, window_minutes: windowMinutes }),
    ),
  // RA-008 Incident Commander — chains the reactive flow + RCA into one
  // coordinated Sev-1/Sev-2 response. Takes the firing alert (it re-runs the
  // flow) and returns the timeline, RCA, postmortem seed, and handoff status.
  incidentCommander: (alert: PrometheusAlert, scenarioId?: string) =>
    unwrap<IncidentCommandResult>(
      http.post('/api/incident-commander', { alert, scenario_id: scenarioId ?? null }),
    ),
  topology:    () => unwrap<TopologyResponse>(http.get('/api/topology')),
  pods:        () => unwrap<SystemPodsResponse>(http.get('/api/system/pods')),
  // Persisted RA-005 notification history (SQL) — backfills the
  // Notifications page so it survives server restarts; live updates still
  // arrive over /ws/chatops.
  notifications: (limit = 200) =>
    unwrap<NotificationsResponse>(http.get('/api/notifications', { params: { limit } })),

  // RCA → approve → apply. Fires the REQUIRED-HITL-gated flag flip; returns an
  // approval id immediately while the platform blocks on human approval.
  applyRcaFix: (
    flag: string,
    variant = 'off',
    actionType = 'set_flag',
    reason?: string,
    // Optional resolution-verifier context: incident_id (ServiceNow number),
    // service, alert/metric/threshold, and rca_verdict. When incident_id is
    // present the backend persists the RCA verdict and fires the verifier.
    context?: Record<string, unknown>,
  ) =>
    unwrap<{
      approval_id: string;
      action_type: string;
      flag: string;
      variant: string;
      status: string;
      timeout_seconds: number;
    }>(
      http.post('/api/demo/rca/apply-fix', {
        flag,
        variant,
        action_type: actionType,
        reason,
        ...(context ?? {}),
      }),
    ),
  // Poll the shared HITL outcome store for an approval id (returns
  // {status:'pending'} until the executor thread finishes).
  hitlOutcome: (approvalId: string) =>
    unwrap<{
      status: string;
      approval_id?: string;
      approver?: string | null;
      flag?: string;
      variant?: string;
      error?: string | null;
    }>(http.get(`/api/demo/auto-heal/outcome/${approvalId}`)),

  // ─── Remediation Recommender (PRS-001) ────────────────────────────────────
  // Rank remediation options for a diagnosed incident. Pure data, no tool
  // dispatch — the operator picks one and hands it to Auto-Healer to execute.
  remediation: (
    rcaVerdict: RCAVerdict,
    triageVerdict?: TriageVerdict,
    environment: 'production' | 'staging' | 'dev' = 'production',
  ) =>
    unwrap<RemediationVerdict>(
      http.post('/api/remediation', {
        rca_verdict: rcaVerdict,
        triage_verdict: triageVerdict ?? null,
        environment,
        operator_preferences: {},
      }),
    ),

  // ─── Auto-Healer Lite (PRS-002) ───────────────────────────────────────────
  // Execute a chosen RemediationOption through the REQUIRED-HITL gate. Async:
  // returns an approval id immediately; the agent blocks at the gate on a pool
  // thread until a human resolves it. Poll autoHealOutcome for the verdict.
  executeOption: (
    option: RemediationOption,
    affectedService: string,
    opts?: { incidentId?: string | null; operator?: string; dryRun?: boolean },
  ) =>
    unwrap<ExecuteRunResponse>(
      http.post('/api/demo/auto-heal/execute', {
        option,
        affected_service: affectedService,
        incident_id: opts?.incidentId ?? null,
        operator: opts?.operator ?? null,
        dry_run: opts?.dryRun ?? true,
      }),
    ),
  // Poll the shared HITL outcome store for an Auto-Healer execution. Returns
  // { status: 'pending' } until the agent thread finishes, then the verdict.
  autoHealOutcome: (approvalId: string) =>
    unwrap<ExecutionVerdict>(http.get(`/api/demo/auto-heal/outcome/${approvalId}`)),
  // Legacy HITL-1 narrow path: recommend + execute a deployment restart. Same
  // async approval-id + poll shape (poll autoHealOutcome).
  autoHealRestart: (req?: {
    deployment?: string;
    namespace?: string;
    reason?: string;
    timeout_seconds?: number;
  }) =>
    unwrap<{
      approval_id: string;
      deployment: string;
      namespace: string;
      status: string;
      timeout_seconds: number;
    }>(http.post('/api/demo/auto-heal/restart', req ?? {})),

  // ── HITL approval loop ──────────────────────────────────────────────────
  approvals: (includeResolved = false) =>
    unwrap<ApprovalsResponse>(
      http.get('/api/approvals', { params: { include_resolved: includeResolved } }),
    ),
  // No bearer token is sent from the browser: the dashboard is served
  // same-origin by the demo server, which authorizes its own console against
  // AIOPS_HITL_APPROVAL_TOKEN internally. The secret stays in the backend env.
  approve: (id: string, approver: string, reason = '') =>
    unwrap<ApprovalRecord>(http.post(`/api/approvals/${id}/approve`, { approver, reason })),
  deny: (id: string, approver: string, reason = '') =>
    unwrap<ApprovalRecord>(http.post(`/api/approvals/${id}/deny`, { approver, reason })),
  // ─── Runbook Executor (RA-004) ────────────────────────────────────────────
  // Kick off the real agent: selects a runbook, simulates, then runs it with
  // the destructive step gated through the platform HITL gate. Returns an
  // approval id + the planned steps immediately; the gated execution runs on a
  // server pool thread.
  runbookExecutorRun: (req?: {
    service?: string;
    severity?: string | null;
    tags?: string[];
    incident_id?: string;
    summary?: string;
    runbook_id?: string;
    timeout_seconds?: number;
  }) =>
    unwrap<RunbookRunResponse>(http.post('/api/demo/runbook-executor/run', req ?? {})),
  // Available runbooks for the picker — each with its steps so the operator can
  // review them and choose a different runbook than the auto-selected match.
  runbookExecutorRunbooks: (params?: { service?: string; severity?: string | null; summary?: string }) =>
    unwrap<RunbookLibraryResponse>(http.get('/api/runbook-executor/runbooks', { params })),
  // Newest-first triaged incidents (each injected failure lands here once
  // triage assigns it a severity). Drives the agent page's incident list.
  verdicts: (params?: { limit?: number; service?: string; severity?: string }) =>
    unwrap<VerdictsResponse>(http.get('/api/verdicts', { params })),
  // Poll the shared HITL outcome store — returns { status: 'pending' } until
  // the agent thread finishes, then the full RunbookExecution.
  runbookOutcome: (approvalId: string) =>
    unwrap<RunbookOutcome>(http.get(`/api/demo/auto-heal/outcome/${approvalId}`)),
  // Read one approval (PENDING → APPROVED/DENIED), to drive the HITL stage.
  getApproval: (approvalId: string) =>
    unwrap<ApprovalRecord>(http.get(`/api/approvals/${approvalId}`)),

  // ─── Knowledge Synthesizer (PRS-007) ──────────────────────────────────────
  // Synthesize a resolved-incident bundle → postmortem + runbook + KB draft.
  // Gated by default: the backend refuses to draft unless the incident's
  // ServiceNow ticket is Resolved/Closed (the 2nd HITL close approval must have
  // run). Pass bypassTicketCheck for an offline demo with no live ServiceNow.
  synthesize: (bundle: Record<string, unknown>, bypassTicketCheck = false) =>
    unwrap<SynthesisResult>(
      http.post('/api/synthesize', bundle, {
        params: { bypass_ticket_check: bypassTicketCheck },
      }),
    ),
  listKb: (params?: { status?: string; service?: string; limit?: number }) =>
    unwrap<KbListResponse>(http.get('/api/kb', { params })),
  getKb: (id: number) => unwrap<KBArticleRow>(http.get(`/api/kb/${id}`)),
  // Demo-only: clear all KB articles so a presentation starts from an empty table.
  resetKb: () => unwrap<{ deleted: number }>(http.post('/api/kb/reset', {})),
  // Request HITL-gated publication; returns an approval id to poll.
  publishKb: (id: number) =>
    unwrap<{ approval_id: string; article_id: number; status: string; timeout_seconds: number }>(
      http.post(`/api/kb/${id}/publish`, {}),
    ),
  kbPublishOutcome: (approvalId: string) =>
    unwrap<{ status: string; approval_id?: string; approver?: string | null; error?: string | null }>(
      http.get(`/api/kb/publish/outcome/${approvalId}`),
    ),

  // ── War-room endpoints (RA-005+006 Notification Assembler) ──────────────
  warRoomAssemble: (req: WarRoomTryRequest) =>
    unwrap<WarRoomAssembly>(http.post('/api/war-room/assemble', req)),
  warRoomRecent: (limit = 50) =>
    unwrap<WarRoomRecentResponse>(http.get('/api/war-room/recent', { params: { limit } })),
  warRoomMetrics: () => unwrap<WarRoomMetrics>(http.get('/api/war-room/metrics')),
  warRoomSetStatus: (id: string, status: string) =>
    unwrap<{ id: string; status: string }>(http.post(`/api/war-room/${id}/status`, { status })),
  warRoomSetAttendee: (id: string, handle: string, attendance: string) =>
    unwrap<{ id: string; handle: string; attendance: string }>(
      http.post(`/api/war-room/${id}/attendee`, { handle, attendance }),
    ),
};
