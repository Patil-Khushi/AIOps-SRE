import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  HeartPulse, ShieldCheck, ShieldAlert, PlayCircle, RotateCcw, Loader2, CheckCircle2,
  XCircle, Gavel, AlertTriangle, Clock, Wrench, Undo2, FlaskConical, ListChecks,
  ArrowRight, Server,
} from 'lucide-react';
import StatCard from '@/components/StatCard';
import { ErrorState } from '@/components/states';
import { api } from '@/lib/api';
import { getAgentById } from '@/data/agentCatalog';
import { setConsoleAgent } from '@/lib/consoleScope';
import { makeCache } from '@/lib/persistentCache';
import { clsx } from '@/lib/format';
import type {
  ApprovalRecord, ExecutionVerdict, RemediationOption,
} from '@/types/api';

// ─── Auto-Healer Lite console (PRS-002) ──────────────────────────────────────
//
// Auto-Healer's OWN surface. It takes a single RemediationOption (chosen in the
// RCA console, or handed from the Approvals page after a human grants the fix)
// and executes it — but only through the platform's REQUIRED HITL gate, so
// nothing fires until a human approves it. Day-1 is a safe dry-run. A standalone
// "restart a deployment" demo exercises the same gate when no option is handed.

type HandoffState = {
  option?: RemediationOption;
  affectedService?: string;
  incidentId?: string | null;
  rootCause?: string | null;
} | null;

type Phase = 'idle' | 'running' | 'done';

// Single-slot persisted execution session. When the operator leaves to grant
// the HITL approval on the Approvals console and comes back, we resume the SAME
// run (its option + approval id) instead of showing an empty "nothing chosen"
// page.
type HealSession = {
  option: RemediationOption;
  affectedService: string;
  incidentId: string | null;
  rootCause: string | null;
  approvalId: string | null;
};
const healSession = makeCache<HealSession>('autoheal-session');

export default function AutoHealer() {
  const catalog = getAgentById('auto-healer');
  const location = useLocation();
  const stateHandoff = (location.state as HandoffState) ?? null;
  // Fresh router state wins; otherwise resume a persisted session so a return
  // trip from the Approvals console lands back on the same execution.
  const persisted = stateHandoff ? null : healSession.get('current');
  const option = stateHandoff?.option ?? persisted?.option ?? null;
  const affectedService =
    stateHandoff?.affectedService ?? persisted?.affectedService ?? (option?.tool_args?.service as string) ?? 'unknown';
  const incidentId = stateHandoff?.incidentId ?? persisted?.incidentId ?? null;
  const rootCause = stateHandoff?.rootCause ?? persisted?.rootCause ?? null;
  const resumeApprovalId = persisted?.approvalId ?? null;

  // Scope the console to this agent so the sidebar shows its focused surfaces
  // (and its name) — including after navigating to a shared /console link.
  useEffect(() => { setConsoleAgent('auto-healer'); }, []);

  return (
    <div className="space-y-6">
      <PageHeader />
      {option ? (
        <ExecuteOption
          option={option}
          affectedService={affectedService}
          incidentId={incidentId}
          rootCause={rootCause}
          resumeApprovalId={resumeApprovalId}
        />
      ) : (
        <>
          <NoOptionCard />
          <RestartDemo />
          {catalog && <AboutCard howItWorks={catalog.howItWorks ?? []} />}
        </>
      )}
    </div>
  );
}

function PageHeader() {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <HeartPulse className="h-6 w-6 text-accent" /> Auto-Healer
          </h1>
          <span className="chip font-mono">PRS-002</span>
          <span className="chip !border-warn/40 !text-warn"><ShieldCheck className="h-3 w-3" /> HITL Required</span>
          <span className="chip"><FlaskConical className="mr-1 inline h-3 w-3" /> dry-run</span>
        </div>
        <p className="mt-1 max-w-2xl text-sm text-ink-500 dark:text-ink-400">
          Apply a chosen remediation through the platform approval gate. Day-1 runs a safe dry-run —
          it validates the option and reports what it would execute, without firing a real tool.
        </p>
      </div>
    </div>
  );
}

// ─── primary surface: execute a handed-off option ────────────────────────────
function ExecuteOption({
  option, affectedService, incidentId, rootCause, resumeApprovalId,
}: {
  option: RemediationOption;
  affectedService: string;
  incidentId: string | null;
  rootCause: string | null;
  resumeApprovalId?: string | null;
}) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [live, setLive] = useState(false);
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<ExecutionVerdict | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Resume a run left in flight before a trip to the Approvals console — pick
  // the polling back up on the same approval id so the outcome shows on return.
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current || !resumeApprovalId) return;
    resumed.current = true;
    setApprovalId(resumeApprovalId);
    setPhase('running');
  }, [resumeApprovalId]);

  const start = useCallback(async () => {
    setErr(null);
    setVerdict(null);
    setApproval(null);
    setPhase('running');
    try {
      // dry_run=false is LIVE: after the gate clears the agent really calls the
      // option's tool (e.g. feature_flags.set_variant) and reports executed /
      // execution_failed. dry_run=true (default) only rehearses.
      const res = await api.executeOption(option, affectedService, { incidentId, dryRun: !live });
      setApprovalId(res.approval_id);
      // Persist so leaving to approve (and returning) resumes this same run.
      healSession.set('current', {
        option, affectedService, incidentId, rootCause, approvalId: res.approval_id,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setPhase('idle');
    }
  }, [option, affectedService, incidentId, rootCause, live]);

  const reset = useCallback(() => {
    setPhase('idle');
    setApprovalId(null);
    setVerdict(null);
    setApproval(null);
    setErr(null);
    healSession.delete('current');
  }, []);

  // Poll the outcome store (+ the approval record) while the gated run is live.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    if (phase !== 'running' || !approvalId) return () => { aliveRef.current = false; };
    const tick = async () => {
      try {
        const oc = await api.autoHealOutcome(approvalId);
        if (!aliveRef.current) return;
        if (oc.status && oc.status !== 'pending') {
          setVerdict(oc);
          setPhase('done');
          return;
        }
      } catch { /* transient */ }
      try {
        const ap = await api.getApproval(approvalId);
        if (aliveRef.current) setApproval(ap);
      } catch { /* not created yet */ }
    };
    tick();
    const timer = setInterval(tick, 1500);
    return () => { aliveRef.current = false; clearInterval(timer); };
  }, [phase, approvalId]);

  const awaiting = phase === 'running' && approval?.status !== 'approved';

  return (
    <div className="space-y-4">
      <ChosenOption option={option} service={affectedService} rootCause={rootCause} />

      {/* Mode toggle: dry-run (rehearse) vs live (really apply the fix). */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex rounded-lg border border-ink-200 p-0.5 dark:border-ink-700">
          <button
            type="button"
            onClick={() => setLive(false)}
            disabled={phase === 'running'}
            className={clsx('inline-flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition',
              !live ? 'bg-accent/15 text-accent' : 'text-ink-500 hover:text-ink-800 dark:text-ink-400')}
          >
            <FlaskConical className="h-3.5 w-3.5" /> Dry-run
          </button>
          <button
            type="button"
            onClick={() => setLive(true)}
            disabled={phase === 'running'}
            className={clsx('inline-flex items-center gap-1.5 rounded-md px-3 py-1 text-xs font-medium transition',
              live ? 'bg-bad/15 text-bad' : 'text-ink-500 hover:text-ink-800 dark:text-ink-400')}
          >
            <HeartPulse className="h-3.5 w-3.5" /> Live (apply)
          </button>
        </div>
        {live && (
          <span className="inline-flex items-center gap-1.5 text-[11px] text-bad">
            <AlertTriangle className="h-3.5 w-3.5" /> Live mode really runs {option.tool_capability ?? 'the action'} after approval.
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <button onClick={start} disabled={phase === 'running'} className={clsx('btn', live ? 'btn-danger' : 'btn-primary')}>
          {phase === 'running' ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
          {live ? 'Execute LIVE (HITL-gated)' : 'Execute (dry-run, HITL-gated)'}
        </button>
        {phase !== 'idle' && (
          <button onClick={reset} disabled={phase === 'running'} className="btn">
            <RotateCcw className="h-4 w-4" /> Clear
          </button>
        )}
        <Link to="/console/rca" className="btn">
          <ListChecks className="h-4 w-4" /> Back to RCA
        </Link>
      </div>

      {err && <ErrorState error={err} />}

      {phase !== 'idle' && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <StatCard
            label="Gate" value={verdict?.decision?.level ?? 'required'}
            icon={<ShieldCheck className="h-4 w-4" />}
            intent={verdict?.decision?.allowed ? 'ok' : awaiting ? 'warn' : 'default'}
            hint={awaiting ? 'awaiting human' : verdict?.decision?.allowed ? 'approved' : '—'}
          />
          <StatCard
            label="Outcome"
            value={verdict ? verdict.status ?? 'done' : 'running'}
            icon={<HeartPulse className="h-4 w-4" />}
            intent={statusIntent(verdict?.status)}
            hint={verdict?.would_execute ? 'would fire tool' : 'no tool fired'}
          />
          <StatCard
            label="Executor" value={option.tool_capability ? '1' : '0'}
            icon={<Wrench className="h-4 w-4" />}
            hint={option.tool_capability ?? 'manual — no executor'}
          />
        </div>
      )}

      {awaiting && approvalId && <HitlPanel approvalId={approvalId} approval={approval} />}

      {verdict && phase === 'done' && <VerdictView verdict={verdict} />}
    </div>
  );
}

function statusIntent(status?: string): 'default' | 'ok' | 'warn' | 'bad' {
  switch (status) {
    case 'executed': case 'dry_run_ok': case 'approved': return 'ok';
    case 'pending_approval': return 'warn';
    case 'blocked': case 'refused': case 'execution_failed': return 'bad';
    default: return 'default';
  }
}

function ChosenOption({ option, service, rootCause }: { option: RemediationOption; service: string; rootCause: string | null }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">Chosen remediation</h2>
        <span className="chip font-mono">{service}</span>
      </div>
      <div className="card-body space-y-2">
        <h3 className="text-sm font-semibold text-ink-900 dark:text-ink-50">{option.title}</h3>
        <p className="text-xs leading-snug text-ink-600 dark:text-ink-300">{option.description}</p>
        {rootCause && (
          <p className="text-[11px] text-ink-500 dark:text-ink-400">
            <span className="font-semibold">For root cause:</span> {rootCause}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="chip"><ShieldAlert className="mr-1 inline h-3 w-3" /> blast: {option.blast_radius} ({option.blast_radius_score}/5)</span>
          <span className="chip">confidence {(option.confidence * 100).toFixed(0)}%</span>
          <span className="chip"><Clock className="mr-1 inline h-3 w-3" /> ~{option.estimated_mttr_minutes}m</span>
          <span className={clsx('chip', option.rollback_tested ? '!border-ok/40 !text-ok' : '!border-warn/40 !text-warn')}>
            <Undo2 className="mr-1 inline h-3 w-3" /> rollback {option.rollback_tested ? 'tested' : 'untested'}
          </span>
          {option.tool_capability && (
            <span className="chip !border-accent/40 !text-accent"><Wrench className="mr-1 inline h-3 w-3" /> {option.action_type} · {option.tool_capability}</span>
          )}
        </div>
        {option.tool_capability && (
          <div className="rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
            {option.tool_capability}({JSON.stringify(option.tool_args)})
          </div>
        )}
      </div>
    </div>
  );
}

function VerdictView({ verdict }: { verdict: ExecutionVerdict }) {
  const trace = verdict.audit_metadata?.decision_trace ?? [];
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">Execution verdict</h2>
        <span className={clsx('chip font-mono',
          verdict.status === 'dry_run_ok' || verdict.status === 'executed' ? '!border-ok/40 !text-ok'
            : verdict.status === 'blocked' || verdict.status === 'refused' || verdict.status === 'execution_failed' ? '!border-bad/40 !text-bad'
            : '!border-warn/40 !text-warn')}>
          {verdict.status}
        </span>
      </div>
      <div className="card-body space-y-3 text-sm">
        {verdict.rationale && <p className="text-ink-700 dark:text-ink-200">{verdict.rationale}</p>}
        {verdict.would_execute && (
          <p className="flex items-center gap-1.5 text-[11px] text-ink-500 dark:text-ink-400">
            <FlaskConical className="h-3.5 w-3.5" /> Dry-run: the gate cleared and the tool would have fired here in a live run.
          </p>
        )}
        {verdict.error && <p className="flex items-center gap-1.5 text-xs text-bad"><XCircle className="h-3.5 w-3.5" /> {verdict.error}</p>}
        {verdict.decision && (
          <div className="rounded-md border border-ink-200 bg-ink-50/50 p-2.5 text-[11px] dark:border-ink-700 dark:bg-ink-800/30">
            <p className="card-title !text-[10px]">Gate decision</p>
            <p className="mt-1 text-ink-700 dark:text-ink-200">
              level <span className="font-mono">{verdict.decision.level}</span> · allowed{' '}
              <span className="font-mono">{String(verdict.decision.allowed)}</span>
              {verdict.decision.approver && <> · approver <span className="font-mono">{verdict.decision.approver}</span></>}
            </p>
            <p className="mt-0.5 text-ink-500 dark:text-ink-400">{verdict.decision.reason}</p>
          </div>
        )}
        {trace.length > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-ink-500 hover:text-accent dark:text-ink-400">
              Decision trace ({trace.length} steps)
            </summary>
            <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 font-mono text-[11px] text-ink-600 dark:border-ink-700 dark:text-ink-300">
              {trace.map((line, i) => <li key={i} className="leading-relaxed">{i + 1}. {line}</li>)}
            </ol>
          </details>
        )}
      </div>
    </div>
  );
}

// ─── HITL panel (links out to the standalone approver console) ───────────────
function HitlPanel({ approvalId, approval }: { approvalId: string; approval: ApprovalRecord | null }) {
  return (
    <div className="card border-warn/40">
      <div className="card-header">
        <h2 className="card-title !text-warn">Human approval required</h2>
        <span className="chip !border-warn/40 !text-warn font-mono">{approval?.status ?? 'pending'}</span>
      </div>
      <div className="card-body space-y-4">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-warn" />
          <div className="min-w-0 text-sm text-ink-700 dark:text-ink-200">
            <p>This remediation is held at the platform HITL gate. Approve it on the Approvals console —
              then use its <span className="font-medium">Auto-Heal</span> button to come back; this run
              resumes right here with the outcome.</p>
            <p className="mt-1.5 font-mono text-[11px] text-ink-500 dark:text-ink-400">
              approval id <span className="text-ink-700 dark:text-ink-300">{approvalId}</span>
              {approval?.action && <> · action <span className="text-ink-700 dark:text-ink-300">{approval.action}</span></>}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/console/approvals" className="btn btn-primary">
            <Gavel className="h-4 w-4" /> Approve on Approvals console
          </Link>
          <span className="inline-flex items-center gap-1.5 text-xs text-ink-500 dark:text-ink-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> waiting for a decision…
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── fallback: reached directly, no option handed off ────────────────────────
function NoOptionCard() {
  return (
    <div className="card border-accent/30">
      <div className="card-body flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-start gap-3">
          <ListChecks className="mt-0.5 h-5 w-5 flex-shrink-0 text-accent" />
          <div className="text-sm text-ink-700 dark:text-ink-200">
            <p className="font-semibold">No remediation chosen yet.</p>
            <p className="mt-0.5 text-xs text-ink-500 dark:text-ink-400">
              Diagnose the incident and approve a ranked fix step in the RCA console — that's where
              remediation now lives. Or try the standalone restart demo below.
            </p>
          </div>
        </div>
        <Link to="/console/rca" className="btn btn-primary flex-shrink-0">
          Go to RCA console <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </div>
  );
}

// ─── standalone restart demo (legacy HITL-1 narrow path) ─────────────────────
function RestartDemo() {
  const [deployment, setDeployment] = useState('product-catalog');
  const [phase, setPhase] = useState<Phase>('idle');
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ExecutionVerdict | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const start = useCallback(async () => {
    setErr(null);
    setOutcome(null);
    setApproval(null);
    setPhase('running');
    try {
      const res = await api.autoHealRestart({ deployment });
      setApprovalId(res.approval_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setPhase('idle');
    }
  }, [deployment]);

  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    if (phase !== 'running' || !approvalId) return () => { aliveRef.current = false; };
    const tick = async () => {
      try {
        const oc = await api.autoHealOutcome(approvalId);
        if (!aliveRef.current) return;
        if (oc.status && oc.status !== 'pending') { setOutcome(oc); setPhase('done'); return; }
      } catch { /* transient */ }
      try {
        const ap = await api.getApproval(approvalId);
        if (aliveRef.current) setApproval(ap);
      } catch { /* not created yet */ }
    };
    tick();
    const timer = setInterval(tick, 1500);
    return () => { aliveRef.current = false; clearInterval(timer); };
  }, [phase, approvalId]);

  const awaiting = phase === 'running' && approval?.status !== 'approved';

  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">Standalone demo · restart a deployment</h2>
        <span className="chip font-mono">automation.runbook.execute</span>
      </div>
      <div className="card-body space-y-3">
        <p className="text-xs text-ink-500 dark:text-ink-400">
          Exercises the same REQUIRED HITL gate end-to-end against the mock automation provider — no
          remediation option needed.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex items-center gap-2 rounded-lg border border-ink-200 px-2.5 py-1.5 dark:border-ink-700">
            <Server className="h-4 w-4 text-ink-400" />
            <input
              value={deployment}
              onChange={(e) => setDeployment(e.target.value)}
              disabled={phase === 'running'}
              className="w-40 bg-transparent text-sm text-ink-900 outline-none dark:text-ink-50"
              placeholder="deployment name"
            />
          </div>
          <button onClick={start} disabled={phase === 'running' || !deployment.trim()} className="btn btn-primary">
            {phase === 'running' ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
            Recommend restart
          </button>
        </div>

        {err && <ErrorState error={err} />}
        {awaiting && approvalId && <HitlPanel approvalId={approvalId} approval={approval} />}
        {outcome && phase === 'done' && (
          <div className="flex items-center gap-2 text-sm">
            {statusIntent(outcome.status) === 'ok'
              ? <CheckCircle2 className="h-4 w-4 text-ok" />
              : <XCircle className="h-4 w-4 text-bad" />}
            <span className="font-mono">{outcome.status}</span>
            {outcome.error && <span className="text-bad">— {outcome.error}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function AboutCard({ howItWorks }: { howItWorks: string[] }) {
  if (howItWorks.length === 0) return null;
  return (
    <div className="card">
      <div className="card-header"><h2 className="card-title">How it works</h2></div>
      <div className="card-body">
        <ol className="space-y-2">
          {howItWorks.map((step, i) => (
            <li key={i} className="flex items-start gap-3 text-sm text-ink-700 dark:text-ink-200">
              <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-accent/15 font-mono text-[11px] font-semibold text-accent">{i + 1}</span>
              {step}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
