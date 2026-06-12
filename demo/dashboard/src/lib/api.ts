import axios, { AxiosError } from 'axios';
import type {
  ApprovalRecord,
  ApprovalsResponse,
  HealthResponse,
  KbListResponse,
  KBArticleRow,
  LiveAlertsResponse,
  PrometheusAlert,
  RCAVerdict,
  ScenariosResponse,
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
  topology:    () => unwrap<TopologyResponse>(http.get('/api/topology')),
  pods:        () => unwrap<SystemPodsResponse>(http.get('/api/system/pods')),

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
};
