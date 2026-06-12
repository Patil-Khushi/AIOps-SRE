import { useMemo, useState } from 'react';
import { Check, ChevronDown, Clock, Gavel, ShieldCheck, X } from 'lucide-react';
import { api, ApiError } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, ErrorState, LoadingState } from '@/components/states';
import { clsx, timeAgo } from '@/lib/format';
import type { ApprovalRecord, ApprovalStatus } from '@/types/api';

const ACTION_LABEL: Record<string, string> = {
  'rca.fix_step.execute': 'RCA fix step',
  'automation.runbook.execute': 'Runbook execution',
  'auto_heal.execute': 'Auto-heal',
  'remediation.recommend': 'Remediation',
  'capacity.recommend': 'Capacity change',
  'policy.optimize': 'Policy change',
  'chaos.experiment.run': 'Chaos experiment',
};

// Context keys shown as detail chips; title/reason are shown elsewhere.
const NOTABLE_CTX = ['service', 'alert', 'deployment', 'namespace', 'flag', 'variant', 'runbook', 'action_type'];

function statusClasses(s: ApprovalStatus): string {
  return {
    pending: 'bg-warn/15 text-warn',
    approved: 'bg-ok/15 text-ok',
    denied: 'bg-bad/15 text-bad',
    expired: 'bg-ink-200 text-ink-500 dark:bg-ink-800 dark:text-ink-400',
  }[s];
}

function expiresLabel(iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.round(s / 60)}m ${s % 60}s`;
}

export default function Approvals() {
  const [approver, setApprover] = useState('console-operator');
  const [showResolved, setShowResolved] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<Record<string, ApprovalStatus>>({});
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, loading, error, refetch } = useFetch(() => api.approvals(showResolved), { intervalMs: 3000 });

  const rows = useMemo(() => {
    const list = data?.approvals ?? [];
    const eff = (r: ApprovalRecord) => optimistic[r.id] ?? r.status;
    return [...list].sort((a, b) => {
      const ap = eff(a) === 'pending' ? 0 : 1;
      const bp = eff(b) === 'pending' ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (b.requested_at || '').localeCompare(a.requested_at || '');
    });
  }, [data, optimistic]);

  const pendingCount = rows.filter((r) => (optimistic[r.id] ?? r.status) === 'pending').length;

  const decide = async (req: ApprovalRecord, kind: 'approve' | 'deny') => {
    if (!approver.trim()) {
      setActionError('Enter an approver name first.');
      return;
    }
    setBusyId(req.id);
    setActionError(null);
    try {
      // No token is passed from the UI — the backend authorizes the
      // same-origin console against AIOPS_HITL_APPROVAL_TOKEN itself.
      if (kind === 'approve') await api.approve(req.id, approver.trim(), 'Approved from console');
      else await api.deny(req.id, approver.trim(), 'Denied from console');
      setOptimistic((o) => ({ ...o, [req.id]: kind === 'approve' ? 'approved' : 'denied' }));
      await refetch();
    } catch (e) {
      setActionError(e instanceof ApiError ? `${e.status} · ${e.message}` : String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <Gavel className="h-6 w-6 text-accent" /> Approvals
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            Human-in-the-loop gate · {pendingCount} pending · approve or deny the AI's risky fixes
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-ink-500 dark:text-ink-400">
            Approver
            <input value={approver} onChange={(e) => setApprover(e.target.value)} className="input !w-40 !py-1.5" placeholder="your name" />
          </label>
          <button type="button" onClick={() => setShowResolved((v) => !v)} className={clsx('btn', showResolved ? 'btn-primary' : 'btn-ghost')}>
            {showResolved ? 'Showing all' : 'Pending only'}
          </button>
        </div>
      </div>

      {actionError && (
        <div className="card border-bad/40">
          <div className="card-body !py-3 text-sm text-bad">Action failed: {actionError}</div>
        </div>
      )}

      {loading && !data ? (
        <div className="card"><LoadingState label="Loading approvals…" /></div>
      ) : error ? (
        <div className="card"><ErrorState error={error} /></div>
      ) : rows.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No approvals waiting"
            hint="Inject a failure (e.g. payment) — the agent's proposed fix lands here for you to approve or deny."
            icon={<ShieldCheck className="h-7 w-7" />}
          />
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-ink-50 text-left text-[11px] uppercase tracking-wider text-ink-500 dark:bg-ink-950/60 dark:text-ink-400">
                <tr>
                  <th className="px-4 py-2.5 font-semibold">Requested</th>
                  <th className="px-4 py-2.5 font-semibold">Issue / Action</th>
                  <th className="px-4 py-2.5 font-semibold">Details</th>
                  <th className="px-4 py-2.5 font-semibold">Window</th>
                  <th className="px-4 py-2.5 font-semibold">Status</th>
                  <th className="px-4 py-2.5 text-right font-semibold">Decision</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-200 dark:divide-ink-800">
                {rows.map((r) => (
                  <Row
                    key={r.id}
                    r={r}
                    status={optimistic[r.id] ?? r.status}
                    busy={busyId === r.id}
                    onApprove={() => decide(r, 'approve')}
                    onDeny={() => decide(r, 'deny')}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Row({
  r,
  status,
  busy,
  onApprove,
  onDeny,
}: {
  r: ApprovalRecord;
  status: ApprovalStatus;
  busy: boolean;
  onApprove: () => void;
  onDeny: () => void;
}) {
  const pending = status === 'pending';
  const label = ACTION_LABEL[r.action] ?? r.action;
  const title = typeof r.context.title === 'string' && r.context.title ? r.context.title : label;
  const chips = NOTABLE_CTX.filter((k) => r.context[k] != null && r.context[k] !== '').map(
    (k) => [k, String(r.context[k])] as const,
  );

  return (
    <tr className="align-top hover:bg-ink-50/60 dark:hover:bg-ink-800/30">
      <td className="whitespace-nowrap px-4 py-3 font-mono text-[11px] text-ink-500 dark:text-ink-400">
        {timeAgo(r.requested_at)}
      </td>
      <td className="px-4 py-3">
        <div className="font-semibold text-ink-900 dark:text-ink-50">{title}</div>
        <div className="text-[11px] text-ink-500 dark:text-ink-400">
          {label} · <span className="font-mono">{r.action}</span>
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {chips.length > 0 ? (
            chips.map(([k, v]) => (
              <span key={k} className="chip font-mono">{k}: {v}</span>
            ))
          ) : (
            <span className="text-xs text-ink-400">—</span>
          )}
        </div>
      </td>
      <td className="whitespace-nowrap px-4 py-3">
        {pending ? (
          <span className="inline-flex items-center gap-1 text-xs text-warn">
            <Clock className="h-3 w-3" /> {expiresLabel(r.expires_at)}
          </span>
        ) : (
          <span className="text-xs text-ink-500 dark:text-ink-400">{r.approver ? `by ${r.approver}` : '—'}</span>
        )}
      </td>
      <td className="px-4 py-3">
        <span className={clsx('rounded-md px-2 py-0.5 text-[11px] font-semibold uppercase', statusClasses(status))}>
          {status}
        </span>
      </td>
      <td className="px-4 py-3 text-right">
        {pending ? <ActionDropdown busy={busy} onApprove={onApprove} onDeny={onDeny} /> : <span className="text-xs text-ink-400">done</span>}
      </td>
    </tr>
  );
}

function ActionDropdown({ busy, onApprove, onDeny }: { busy: boolean; onApprove: () => void; onDeny: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative inline-block text-left">
      <button type="button" onClick={() => setOpen((o) => !o)} disabled={busy} className="btn btn-ghost !py-1.5 !text-xs">
        {busy ? 'Working…' : 'Action'} {!busy && <ChevronDown className="h-3.5 w-3.5" />}
      </button>
      {open && !busy && (
        <>
          <button type="button" aria-hidden className="fixed inset-0 z-10 cursor-default" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-20 mt-1 w-32 overflow-hidden rounded-lg border border-ink-200 bg-white shadow-lg dark:border-ink-700 dark:bg-ink-900">
            <button
              type="button"
              onClick={() => { setOpen(false); onApprove(); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-ok hover:bg-ok/10"
            >
              <Check className="h-3.5 w-3.5" /> Approve
            </button>
            <button
              type="button"
              onClick={() => { setOpen(false); onDeny(); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-bad hover:bg-bad/10"
            >
              <X className="h-3.5 w-3.5" /> Deny
            </button>
          </div>
        </>
      )}
    </div>
  );
}
