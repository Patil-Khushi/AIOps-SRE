import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  PlayCircle,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import {
  AgentOutcome,
  ApprovalRecord,
  approve,
  deny,
  getAgentOutcome,
  listApprovals,
  triggerDemoRestart,
} from './api';

const POLL_INTERVAL_MS = 1500;
const DEFAULT_APPROVER = 'demo-sre';

type Toast = { id: number; text: string; tone: 'good' | 'bad' };

export default function App() {
  const [pending, setPending] = useState<ApprovalRecord[]>([]);
  const [recent, setRecent] = useState<ApprovalRecord[]>([]);
  const [approver, setApprover] = useState(DEFAULT_APPROVER);
  const [outcomes, setOutcomes] = useState<Record<string, AgentOutcome>>({});
  const [triggering, setTriggering] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const pushToast = (text: string, tone: 'good' | 'bad') => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500);
  };

  // Poll the registry for the live state.  Two queries: pending (live), and
  // include_resolved=true to keep the recent-decisions table populated even
  // after a pending entry leaves the pending list.
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
          .slice(0, 20);
        setRecent(resolved);
      } catch {
        // Server down / network blip — silent so the polling loop keeps trying.
      }
    };
    tick();
    const t = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // For every approval id we've launched via the demo button, poll the
  // outcome endpoint until it flips from pending → executed/denied/expired.
  // Stop polling once a terminal status is seen.
  useEffect(() => {
    const interval = setInterval(async () => {
      const ids = Object.keys(outcomes).filter((id) => outcomes[id]?.status === 'pending');
      if (ids.length === 0) return;
      const results = await Promise.all(
        ids.map(async (id) => {
          try {
            const o = await getAgentOutcome(id);
            return [id, o] as const;
          } catch {
            return [id, null] as const;
          }
        }),
      );
      setOutcomes((prev) => {
        const next = { ...prev };
        for (const [id, o] of results) if (o) next[id] = o;
        return next;
      });
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [outcomes]);

  const trigger = async () => {
    setTriggering(true);
    try {
      const res = await triggerDemoRestart({ deployment: 'product-catalog' });
      setOutcomes((p) => ({ ...p, [res.approval_id]: { status: 'pending', approval_id: res.approval_id } }));
      pushToast(`Approval ${res.approval_id.slice(0, 8)} requested`, 'good');
    } catch (e: any) {
      pushToast(`Trigger failed: ${e?.message ?? e}`, 'bad');
    } finally {
      setTriggering(false);
    }
  };

  const onApprove = async (req: ApprovalRecord, reasonText: string) => {
    if (!approver.trim()) {
      pushToast('Enter your approver id first.', 'bad');
      return;
    }
    setBusyId(req.id);
    try {
      await approve(req.id, approver.trim(), reasonText.trim() || 'Approved via console');
      pushToast(`Approved ${req.action}`, 'good');
    } catch (e: any) {
      pushToast(`Approve failed: ${e?.response?.data?.detail ?? e?.message}`, 'bad');
    } finally {
      setBusyId(null);
    }
  };

  const onDeny = async (req: ApprovalRecord, reasonText: string) => {
    if (!approver.trim()) {
      pushToast('Enter your approver id first.', 'bad');
      return;
    }
    setBusyId(req.id);
    try {
      await deny(req.id, approver.trim(), reasonText.trim() || 'Denied via console');
      pushToast(`Denied ${req.action}`, 'good');
    } catch (e: any) {
      pushToast(`Deny failed: ${e?.response?.data?.detail ?? e?.message}`, 'bad');
    } finally {
      setBusyId(null);
    }
  };

  const triggeredOutcomes = useMemo(
    () => Object.values(outcomes).sort((a, b) => (a.approval_id || '').localeCompare(b.approval_id || '')),
    [outcomes],
  );

  return (
    <div className="min-h-screen px-6 py-8 max-w-6xl mx-auto">
      <Header />
      <PrincipleBanner />
      <TriggerPanel onClick={trigger} loading={triggering} />
      <ApproverIdRow approver={approver} setApprover={setApprover} />
      <PendingList
        pending={pending}
        busyId={busyId}
        onApprove={(r, reason) => onApprove(r, reason)}
        onDeny={(r, reason) => onDeny(r, reason)}
      />
      <AgentOutcomes outcomes={triggeredOutcomes} />
      <RecentDecisions recent={recent} />
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
          Required-HITL gate · platform-enforced (CLAUDE.md non-negotiable #3)
        </p>
      </div>
    </div>
  );
}

function PrincipleBanner() {
  return (
    <div className="rounded-xl border border-pink-500/20 bg-pink-500/5 px-4 py-3 mb-6 text-sm text-pink-100">
      <span className="font-medium">How it works:</span> the agent calls a{' '}
      <code className="text-pink-300">Required</code>-HITL capability; the platform pauses the
      action and posts an approval prompt. You decide here or in Slack. The agent only proceeds
      after a human says yes.
    </div>
  );
}

function TriggerPanel({ onClick, loading }: { onClick: () => void; loading: boolean }) {
  return (
    <section className="rounded-xl bg-slate-900/60 border border-slate-800 px-5 py-4 mb-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold">Trigger demo agent</h2>
          <p className="text-slate-400 text-sm">
            Runs <code className="text-cyan-300">auto_healer_lite</code> against{' '}
            <code className="text-cyan-300">deployment/product-catalog</code>. The gate will block
            it until you approve below.
          </p>
        </div>
        <button
          onClick={onClick}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-lg bg-pink-500 hover:bg-pink-400 active:bg-pink-600 disabled:opacity-50 disabled:hover:bg-pink-500 transition px-4 py-2 text-sm font-medium text-slate-900"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <PlayCircle className="w-4 h-4" />}
          {loading ? 'Requesting…' : 'Restart product-catalog'}
        </button>
      </div>
    </section>
  );
}

function ApproverIdRow({
  approver,
  setApprover,
}: {
  approver: string;
  setApprover: (s: string) => void;
}) {
  return (
    <div className="flex items-center gap-3 mb-4 text-sm">
      <label className="text-slate-400">Your approver id:</label>
      <input
        value={approver}
        onChange={(e) => setApprover(e.target.value)}
        placeholder="alice@example.com"
        className="rounded-md bg-slate-900 border border-slate-700 focus:border-pink-400 focus:outline-none px-3 py-1.5 text-slate-100 w-64"
      />
      <span className="text-slate-500 text-xs">recorded against every decision (audit trail)</span>
    </div>
  );
}

function PendingList({
  pending,
  busyId,
  onApprove,
  onDeny,
}: {
  pending: ApprovalRecord[];
  busyId: string | null;
  onApprove: (r: ApprovalRecord, reason: string) => void;
  onDeny: (r: ApprovalRecord, reason: string) => void;
}) {
  return (
    <section className="mb-8">
      <h2 className="text-base font-semibold mb-3 flex items-center gap-2">
        <AlertTriangle className="w-5 h-5 text-amber-300" />
        Pending approvals
        <span className="text-xs text-slate-500">({pending.length})</span>
      </h2>
      {pending.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 text-slate-500 text-sm px-5 py-6 text-center">
          No approvals waiting. Click "Restart product-catalog" above to demo the flow.
        </div>
      ) : (
        <div className="space-y-3">
          {pending.map((r) => (
            <PendingCard
              key={r.id}
              r={r}
              busy={busyId === r.id}
              onApprove={(reason) => onApprove(r, reason)}
              onDeny={(reason) => onDeny(r, reason)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function PendingCard({
  r,
  busy,
  onApprove,
  onDeny,
}: {
  r: ApprovalRecord;
  busy: boolean;
  onApprove: (reason: string) => void;
  onDeny: (reason: string) => void;
}) {
  const expiresIn = useCountdown(r.expires_at);
  const [reason, setReason] = useState('');

  return (
    <div className="rounded-xl border border-amber-400/30 bg-amber-400/[0.04] px-5 py-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm">
            <code className="text-amber-200 font-medium">{r.action}</code>
            <span className="text-slate-500">·</span>
            <span className="text-slate-400">id {r.id.slice(0, 8)}…</span>
          </div>
          {Object.keys(r.context).length > 0 && (
            <pre className="mt-2 text-xs text-slate-300 bg-slate-950/60 rounded-md px-3 py-2 max-w-full overflow-x-auto">
              {JSON.stringify(r.context, null, 2)}
            </pre>
          )}
          <div className="mt-2 text-xs text-slate-500 flex items-center gap-1">
            <Clock className="w-3 h-3" /> expires in {expiresIn}
          </div>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Justification (recorded in the audit trail)…"
            className="mt-3 w-full rounded-md bg-slate-900 border border-slate-700 focus:border-amber-400 focus:outline-none px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !busy) onApprove(reason);
            }}
          />
        </div>
        <div className="flex flex-col gap-2 shrink-0 self-stretch justify-end">
          <button
            onClick={() => onApprove(reason)}
            disabled={busy}
            className="rounded-md bg-emerald-500 hover:bg-emerald-400 disabled:opacity-50 px-3 py-1.5 text-sm font-medium text-slate-900"
          >
            {busy ? '…' : 'Approve'}
          </button>
          <button
            onClick={() => onDeny(reason)}
            disabled={busy}
            className="rounded-md bg-rose-500 hover:bg-rose-400 disabled:opacity-50 px-3 py-1.5 text-sm font-medium text-slate-900"
          >
            {busy ? '…' : 'Deny'}
          </button>
        </div>
      </div>
    </div>
  );
}

function AgentOutcomes({ outcomes }: { outcomes: AgentOutcome[] }) {
  if (outcomes.length === 0) return null;
  return (
    <section className="mb-8">
      <h2 className="text-base font-semibold mb-3">Agent outcomes (this session)</h2>
      <div className="space-y-2">
        {outcomes.map((o) => (
          <div
            key={o.approval_id || Math.random()}
            className="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-sm"
          >
            <div className="flex items-center gap-2 mb-1">
              <OutcomeBadge status={o.status} />
              <span className="text-slate-500">id {String(o.approval_id || '').slice(0, 8)}…</span>
              {o.approver && (
                <span className="text-slate-400">
                  by <code className="text-pink-300">{o.approver}</code>
                </span>
              )}
            </div>
            {o.result?.stdout && (
              <pre className="text-xs text-cyan-300 bg-slate-950/60 rounded-md px-3 py-2 overflow-x-auto">
                {o.result.stdout}
              </pre>
            )}
            {o.error && <div className="text-xs text-rose-300">{o.error}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}

function OutcomeBadge({ status }: { status: AgentOutcome['status'] }) {
  const map: Record<AgentOutcome['status'], { color: string; icon: JSX.Element; label: string }> = {
    pending: { color: 'bg-slate-700 text-slate-200', icon: <Loader2 className="w-3 h-3 animate-spin" />, label: 'waiting for human' },
    executed: { color: 'bg-emerald-500/20 text-emerald-200', icon: <CheckCircle2 className="w-3 h-3" />, label: 'executed' },
    blocked: { color: 'bg-slate-600/40 text-slate-200', icon: <XCircle className="w-3 h-3" />, label: 'blocked' },
    denied: { color: 'bg-rose-500/20 text-rose-200', icon: <XCircle className="w-3 h-3" />, label: 'denied' },
    expired: { color: 'bg-amber-500/20 text-amber-200', icon: <Clock className="w-3 h-3" />, label: 'expired (no answer)' },
    error: { color: 'bg-rose-500/20 text-rose-200', icon: <AlertTriangle className="w-3 h-3" />, label: 'error' },
  };
  const m = map[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${m.color}`}>
      {m.icon}
      {m.label}
    </span>
  );
}

function RecentDecisions({ recent }: { recent: ApprovalRecord[] }) {
  if (recent.length === 0) return null;
  return (
    <section>
      <h2 className="text-base font-semibold mb-3">Recent decisions</h2>
      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/60 text-slate-400 text-xs uppercase tracking-wide">
            <tr>
              <th className="text-left px-4 py-2">When</th>
              <th className="text-left px-4 py-2">Action</th>
              <th className="text-left px-4 py-2">Status</th>
              <th className="text-left px-4 py-2">Approver</th>
              <th className="text-left px-4 py-2">Reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {recent.map((r) => (
              <tr key={r.id} className="hover:bg-slate-900/40">
                <td className="px-4 py-2 text-slate-300">{relativeTime(r.decided_at)}</td>
                <td className="px-4 py-2"><code className="text-cyan-300">{r.action}</code></td>
                <td className="px-4 py-2"><StatusPill status={r.status} /></td>
                <td className="px-4 py-2 text-slate-300">{r.approver || '—'}</td>
                <td className="px-4 py-2 text-slate-400 max-w-xs truncate" title={r.reason}>{r.reason || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function StatusPill({ status }: { status: ApprovalRecord['status'] }) {
  const m: Record<ApprovalRecord['status'], string> = {
    pending: 'bg-slate-700 text-slate-200',
    approved: 'bg-emerald-500/20 text-emerald-200',
    denied: 'bg-rose-500/20 text-rose-200',
    expired: 'bg-amber-500/20 text-amber-200',
  };
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${m[status]}`}>{status}</span>;
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
