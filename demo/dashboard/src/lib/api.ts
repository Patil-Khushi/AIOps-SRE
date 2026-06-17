import axios, { AxiosError } from 'axios';
import type {
  ApprovalRecord,
  ApprovalsResponse,
  HealthResponse,
  IncidentCommandResult,
  KbListResponse,
  KBArticleRow,
  LiveAlertsResponse,
  NotificationsResponse,
  PrometheusAlert,
  RCAVerdict,
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
// Default timeout is generous (90 s) because /api/triage/live runs the agent's
// 8-stage pipeline (including LLM calls) for every firing alert. Even with the
// backend parallelizing per-alert triage via asyncio.gather, a worst-case
// Azure OpenAI cold-start can still take 10–20 s.
export const http = axios.create({
  baseURL: '',
  timeout: 90_000,
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

// ── RA-006 War-Room Assembler shapes (agents/war_room_assembler models) ──
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
    timeout_seconds?: number;
  }) =>
    unwrap<RunbookRunResponse>(http.post('/api/demo/runbook-executor/run', req ?? {})),
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
  synthesize: (bundle: Record<string, unknown>) =>
    unwrap<SynthesisResult>(http.post('/api/synthesize', bundle)),
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

  // ── RA-006 War-Room Assembler ───────────────────────────────────────────
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
