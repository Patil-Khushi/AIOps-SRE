import axios, { AxiosError } from 'axios';
import type {
  HealthResponse,
  LiveAlertsResponse,
  PrometheusAlert,
  ScenariosResponse,
  SystemPodsResponse,
  TopologyResponse,
  TriageVerdict,
} from '@/types/api';

// Axios instance — uses Vite proxy in dev, same-origin in prod (served from /dashboard).
export const http = axios.create({
  baseURL: '',
  timeout: 30_000,
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
    unwrap<TriageVerdict>(http.post('/api/triage', { alert })),
  triageLive: () =>
    unwrap<{ count: number; verdicts: TriageVerdict[] }>(http.post('/api/triage/live')),
  topology:    () => unwrap<TopologyResponse>(http.get('/api/topology')),
  pods:        () => unwrap<SystemPodsResponse>(http.get('/api/system/pods')),
};
