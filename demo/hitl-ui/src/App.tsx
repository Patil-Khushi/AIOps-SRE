import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Clock,
  Loader2,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import { ApprovalRecord, ApprovalStatus, approve, deny, listApprovals } from './api';

const POLL_INTERVAL_MS = 1500;
const DEFAULT_APPROVER = 'demo-sre';

// HITL-2 (#102): the approval token lives in sessionStorage so it disappears
// when the operator closes the tab — never written to localStorage / disk.
const TOKEN_STORAGE_KEY = 'aiops.hitl.approval_token';
const readStoredToken = () => {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
};
const writeStoredToken = (t: string) => {
  try {
    if (t) sessionStorage.setItem(TOKEN_STORAGE_KEY, t);
    else sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // sessionStorage disabled (privacy mode, etc.) — fall back to memory only.
  }
};

// Friendly labels for the gated capabilities.
const ACTION_LABEL: Record<string, string> = {
  'rca.fix_step.execute': 'RCA fix step',
  'automation.runbook.execute': 'Runbook execution',
  'auto_heal.execute': 'Auto-heal',
  'remediation.recommend': 'Remediation',
  'capacity.recommend': 'Capacity change',
  'policy.optimize': 'Policy change',
  'chaos.experiment.run': 'Chaos experiment',
};

// Context keys worth surfacing as detail chips (everything else is plumbing).
const NOTABLE_CTX = ['service', 'deployment', 'namespace', 'flag', 'variant', 'runbook', 'action_type'];

type Toast = { id: number; text: string; tone: 'good' | 'bad' };

export default function App() {
  const [pending, setPending] = useState<ApprovalRecord[]>([]);
  const [recent, setRecent] = useState<ApprovalRecord[]>([]);
  const [approver, setApprover] = useState(DEFAULT_APPROVER);
  const [approvalToken, setApprovalToken] = useState<string>(readStoredToken);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Optimistic status overrides so a row flips the instant a decision lands,
  // before the next poll catches up.
  const [optimistic, setOptimistic] = useState<Record<string, ApprovalStatus>>({});
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    writeStoredToken(approvalToken);
  }, [approvalToken]);

  const pushToast = (text: string, tone: 'good' | 'bad') => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500);
  };

  // Poll the registry: pending (live) + resolved history.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const [p, all] = await Promise.all([listApprovals(false), listApprovals(true)]);
        if (cancelled) return;
        setPending(p.approvals);
        const resolved = all.approvals
          .filter((r) => r.status !== 'pending')
          .sort((a, b) => (b.decided_at || '').localeCompare(a.decided_at || ''))
          .slice(0, 25);
        setRecent(resolved);
        // Drop optimistic overrides the server has now confirmed.
        setOptimistic((o) => {
          const next = { ...o };
          for (const r of all.approvals) if (r.status !== 'pending') delete next[r.id];
          return next;
        });
      } catch {
        // Server down / network blip — silent so the loop keeps retrying.
      }
    };
    tick();
    const t = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const decide = async (req: ApprovalRecord, kind: 'approve' | 'deny') => {
    if (!approver.trim()) {
      pushToast('Enter your approver id first.', 'bad');
      return;
    }
    setBusyId(req.id);
    try {
      if (kind === 'approve') {
        await approve(req.id, approver.trim(), 'Approved via console', approvalToken);
        setOptimistic((o) => ({ ...o, [req.id]: 'approved' }));
        pushToast(`Approved ${req.action}`, 'good');
      } else {
        await deny(req.id, approver.trim(), 'Denied via console', approvalToken);
        setOptimistic((o) => ({ ...o, [req.id]: 'denied' }));
        pushToast(`Denied ${req.action}`, 'good');
      }
    } catch (e: any) {
      pushToast(`${kind === 'approve' ? 'Approve' : 'Deny'} failed: ${e?.response?.data?.detail ?? e?.message}`, 'bad');
    } finally {
      setBusyId(null);
    }
  };

  // One merged list — pending first, then by most recent.
  const rows = useMemo(() => {
    const byId = new Map<string, ApprovalRecord>();
    for (const r of recent) byId.set(r.id, r);
    for (const r of pending) byId.set(r.id, r);
    const effStatus = (r: ApprovalRecord) => optimistic[r.id] ?? r.status;
    return [...byId.values()].sort((a, b) => {
      const ap = effStatus(a) === 'pending' ? 0 : 1;
      const bp = effStatus(b) === 'pending' ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return (b.requested_at || '').localeCompare(a.requested_at || '');
    });
  }, [pending, recent, optimistic]);

  const pendingCount = rows.filter((r) => (optimistic[r.id] ?? r.status) === 'pending').length;

  return (
    <div className="min-h-screen px-6 py-8 max-w-6xl mx-auto">
      <Header />

      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 mb-6">
        <ApproverIdRow approver={approver} setApprover={setApprover} />
        <ApprovalTokenRow token={approvalToken} setToken={setApprovalToken} />
      </div>

      <RequestsTable
        rows={rows}
        pendingCount={pendingCount}
        busyId={busyId}
        statusOf={(r) => optimistic[r.id] ?? r.status}
        onApprove={(r) => decide(r, 'approve')}
        onDeny={(r) => decide(r, 'deny')}
      />

      <ToastStack toasts={toasts} />
    </div>
  );
}

function Header() {
  return (
    <div className="flex items-center gap-3 mb-6">
      <ShieldCheck className="w-8 h-8 text-pink-400" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">HITL Approver Console</h1>
        <p className="text-slate-400 text-sm">
          Required-HITL gate · decide here or in Slack — the agent only proceeds after a human says yes.
        </p>
      </div>
    </div>
  );
}

function ApproverIdRow({ approver, setApprover }: { approver: string; setApprover: (s: string) => void }) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <label className="text-slate-400">Approver id:</label>
      <input
        value={approver}
        onChange={(e) => setApprover(e.target.value)}
        placeholder="alice@example.com"
        className="rounded-md bg-slate-900 border border-slate-700 focus:border-pink-400 focus:outline-none px-3 py-1.5 text-slate-100 w-56"
      />
    </div>
  );
}

function ApprovalTokenRow({ token, setToken }: { token: string; setToken: (s: string) => void }) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <label className="text-slate-400">Token:</label>
      <input
        type="password"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder="AIOPS_HITL_APPROVAL_TOKEN (blank in demo mode)"
        className="rounded-md bg-slate-900 border border-slate-700 focus:border-pink-400 focus:outline-none px-3 py-1.5 text-slate-100 w-80 font-mono"
        autoComplete="off"
        spellCheck={false}
      />
    </div>
  );
}

// ─── the requests table ─────────────────────────────────────────────────────

interface TableProps {
  rows: ApprovalRecord[];
  pendingCount: number;
  busyId: string | null;
  statusOf: (r: ApprovalRecord) => ApprovalStatus;
  onApprove: (r: ApprovalRecord) => void;
  onDeny: (r: ApprovalRecord) => void;
}

function RequestsTable({ rows, pendingCount, busyId, statusOf, onApprove, onDeny }: TableProps) {
  return (
    <section className="mb-8">
      <h2 className="text-base font-semibold mb-3 flex items-center gap-2">
        <AlertTriangle className="w-5 h-5 text-amber-300" />
        Approval requests
        <span className="text-xs text-slate-500">({pendingCount} pending)</span>
      </h2>

      {rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 text-slate-500 text-sm px-5 py-6 text-center">
          No requests yet. Trigger a Required-HITL action (e.g. an RCA fix step or auto-heal restart)
          and it appears here for you to approve or deny.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-slate-400 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-4 py-2.5">Requested</th>
                <th className="text-left px-4 py-2.5">Action</th>
                <th className="text-left px-4 py-2.5">Details</th>
                <th className="text-left px-4 py-2.5">Window</th>
                <th className="text-left px-4 py-2.5">Status</th>
                <th className="text-right px-4 py-2.5">Decision</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {rows.map((r) => (
                <RequestRow
                  key={r.id}
                  r={r}
                  status={statusOf(r)}
                  busy={busyId === r.id}
                  onApprove={() => onApprove(r)}
                  onDeny={() => onDeny(r)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function RequestRow({
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
  const expiresIn = useCountdown(r.expires_at);
  const label = ACTION_LABEL[r.action] ?? r.action;
  const chips = NOTABLE_CTX.filter((k) => r.context[k] != null).map((k) => [k, String(r.context[k])] as const);

  return (
    <tr className="hover:bg-slate-900/40 align-top">
      <td className="px-4 py-3 text-slate-300 whitespace-nowrap">{relativeTime(r.requested_at)}</td>
      <td className="px-4 py-3">
        <div className="font-medium text-slate-100">{label}</div>
        <code className="text-[11px] text-slate-500">{r.action}</code>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {chips.length > 0 ? (
            chips.map(([k, v]) => (
              <span key={k} className="rounded-md bg-slate-800 px-2 py-0.5 text-[11px] font-mono text-slate-300">
                {k}: {v}
              </span>
            ))
          ) : (
            <span className="text-slate-500 text-xs">—</span>
          )}
        </div>
        {typeof r.context.reason === 'string' && r.context.reason && (
          <p className="mt-1 text-[11px] text-slate-500 max-w-md">{r.context.reason}</p>
        )}
      </td>
      <td className="px-4 py-3 whitespace-nowrap">
        {pending ? (
          <span className="inline-flex items-center gap-1 text-xs text-amber-300">
            <Clock className="w-3 h-3" /> {expiresIn}
          </span>
        ) : (
          <span className="text-xs text-slate-400">{r.approver ? `by ${r.approver}` : '—'}</span>
        )}
      </td>
      <td className="px-4 py-3"><StatusPill status={status} /></td>
      <td className="px-4 py-3 text-right">
        {pending ? (
          <ActionDropdown busy={busy} onApprove={onApprove} onDeny={onDeny} />
        ) : (
          <span className="text-slate-600 text-xs">done</span>
        )}
      </td>
    </tr>
  );
}

function ActionDropdown({ busy, onApprove, onDeny }: { busy: boolean; onApprove: () => void; onDeny: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative inline-block text-left">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={busy}
        className="inline-flex items-center gap-1.5 rounded-md border border-slate-700 bg-slate-900 hover:bg-slate-800 disabled:opacity-50 px-3 py-1.5 text-xs font-medium text-slate-100"
      >
        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
        {busy ? 'Working…' : 'Action'}
        {!busy && <ChevronDown className="w-3.5 h-3.5" />}
      </button>
      {open && !busy && (
        <>
          {/* click-away catcher */}
          <button type="button" aria-hidden className="fixed inset-0 z-10 cursor-default" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-20 mt-1 w-32 overflow-hidden rounded-md border border-slate-700 bg-slate-900 shadow-xl">
            <button
              type="button"
              onClick={() => { setOpen(false); onApprove(); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-emerald-300 hover:bg-emerald-500/10"
            >
              <CheckCircle2 className="w-3.5 h-3.5" /> Approve
            </button>
            <button
              type="button"
              onClick={() => { setOpen(false); onDeny(); }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-rose-300 hover:bg-rose-500/10"
            >
              <XCircle className="w-3.5 h-3.5" /> Deny
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: ApprovalStatus }) {
  const m: Record<ApprovalStatus, string> = {
    pending: 'bg-amber-500/20 text-amber-200',
    approved: 'bg-emerald-500/20 text-emerald-200',
    denied: 'bg-rose-500/20 text-rose-200',
    expired: 'bg-slate-600/40 text-slate-300',
  };
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium uppercase ${m[status]}`}>{status}</span>;
}

function ToastStack({ toasts }: { toasts: Toast[] }) {
  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-2 z-50">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`rounded-lg px-3 py-2 text-sm shadow-lg ${
            t.tone === 'good' ? 'bg-emerald-500 text-slate-900' : 'bg-rose-500 text-slate-900'
          }`}
        >
          {t.text}
        </div>
      ))}
    </div>
  );
}

// ─── tiny utilities ─────────────────────────────────────────────────────────

function useCountdown(isoTarget: string) {
  const target = new Date(isoTarget).getTime();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const remain = Math.max(0, Math.round((target - now) / 1000));
  if (remain <= 0) return 'expired';
  const m = Math.floor(remain / 60);
  const s = remain % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function relativeTime(iso: string | null) {
  if (!iso) return '—';
  const d = new Date(iso).getTime();
  const diff = Math.max(0, Date.now() - d);
  if (diff < 60_000) return `${Math.round(diff / 1000)}s ago`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  return new Date(iso).toLocaleTimeString();
}
