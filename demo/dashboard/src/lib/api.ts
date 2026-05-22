import axios, { AxiosError } from 'axios';
import type {
  HealthResponse,
  LiveAlertsResponse,
  PrometheusAlert,
  RCAVerdict,
  ScenariosResponse,
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
};
