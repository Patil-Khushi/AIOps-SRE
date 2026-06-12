// Thin axios wrapper for the HITL endpoints exposed by demo/ui/server.py.
// All paths are absolute — the page is served from /hitl/ on the same host
// the API lives on, so a relative `/api/...` works for both built bundle
// and dev-server (vite proxies /api → :8765).

import axios from 'axios';

export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired';

export interface ApprovalRecord {
  id: string;
  action: string;
  context: Record<string, unknown>;
  status: ApprovalStatus;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  approver: string | null;
  reason: string;
}

export interface ApprovalListResponse {
  count: number;
  approvals: ApprovalRecord[];
}

export interface TriggerResponse {
  approval_id: string;
  deployment: string;
  namespace: string;
  status: 'pending';
  timeout_seconds: number;
}

export interface AgentOutcome {
  status: 'pending' | 'executed' | 'blocked' | 'denied' | 'expired' | 'error';
  approval_id?: string | null;
  approver?: string | null;
  recommendation?: {
    deployment: string;
    namespace: string;
    reason: string;
    runbook: string;
    dry_run: boolean;
  };
  result?: {
    runbook: string;
    target: string;
    namespace: string;
    dry_run: boolean;
    exit_code: number;
    stdout: string;
  };
  error?: string | null;
}

export async function listApprovals(includeResolved = false): Promise<ApprovalListResponse> {
  const { data } = await axios.get<ApprovalListResponse>('/api/approvals', {
    params: { include_resolved: includeResolved },
  });
  return data;
}

// HITL-2 (#102): the server reads AIOPS_HITL_APPROVAL_TOKEN and, when set,
// authorizes this approver console automatically because it is served
// same-origin by the demo server — the secret stays in the backend env and is
// never typed into the UI. So approve/deny send no Authorization header.
export async function approve(
  id: string,
  approver: string,
  reason: string,
): Promise<ApprovalRecord> {
  const { data } = await axios.post<ApprovalRecord>(
    `/api/approvals/${id}/approve`,
    { approver, reason },
  );
  return data;
}

export async function deny(
  id: string,
  approver: string,
  reason: string,
): Promise<ApprovalRecord> {
  const { data } = await axios.post<ApprovalRecord>(
    `/api/approvals/${id}/deny`,
    { approver, reason },
  );
  return data;
}

export async function triggerDemoRestart(opts: {
  deployment?: string;
  namespace?: string;
  reason?: string;
  timeout_seconds?: number;
}): Promise<TriggerResponse> {
  const { data } = await axios.post<TriggerResponse>('/api/demo/auto-heal/restart', opts);
  return data;
}

export async function getAgentOutcome(approvalId: string): Promise<AgentOutcome> {
  const { data } = await axios.get<AgentOutcome>(`/api/demo/auto-heal/outcome/${approvalId}`);
  return data;
}
