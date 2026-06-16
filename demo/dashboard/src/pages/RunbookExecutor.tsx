import { Fragment, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import {
  PlayCircle, RotateCcw, Search, FlaskConical, Cog, UserCheck, BadgeCheck, Undo2,
  CheckCircle2, XCircle, Loader2, ExternalLink, ShieldCheck, AlertTriangle, Clock,
  SkipForward, FileText, Inbox,
} from 'lucide-react';
import StatCard from '@/components/StatCard';
import { SeverityBadge } from '@/components/SeverityBadge';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { getAgentById } from '@/data/agentCatalog';
import { clsx, timeAgo } from '@/lib/format';
import type {
  ApprovalRecord, PlannedStep, RunbookOutcome, RunbookRunResponse, RunbookStepRecord,
  Severity, VerdictRecord,
} from '@/types/api';

// Triage emits 'Sev-2'; the runbook selector matches 'sev2'.
function sevToken(sev: Severity): string {
  return sev.replace('Sev-', 'sev');
}

// ─── the six workflow stages the agent moves through, in order ───────────────
type StageState = 'pending' | 'active' | 'done' | 'failed' | 'skipped';

interface StageDef { key: string; label: string; desc: string; icon: typeof Search }

const STAGES: StageDef[] = [
  { key: 'select',   label: 'Select runbook', desc: 'Match service · tags · severity', icon: Search },
  { key: 'simulate', label: 'Dry-run',        desc: 'Preview every step, no changes',  icon: FlaskConical },
  { key: 'execute',  label: 'Execute',        desc: 'Run steps in order',              icon: Cog },
  { key: 'hitl',     label: 'Human approval', desc: 'Gate destructive steps',          icon: UserCheck },
  { key: 'verify',   label: 'Verify',         desc: 'Confirm resolution',              icon: BadgeCheck },
  { key: 'rollback', label: 'Rollback-ready', desc: 'Tested reverse on standby',       icon: Undo2 },
];

type Phase = 'idle' | 'running' | 'done';

function computeStages(
  run: RunbookRunResponse | null,
  outcome: RunbookOutcome | null,
  approval: ApprovalRecord | null,
  phase: Phase,
): StageState[] {
  const running = phase === 'running';
  const hasDestructive = run?.planned_steps.some((s) => s.destructive) ?? false;
  const noRunbook = run?.status === 'no_runbook' || outcome?.status === 'no_runbook';
  const final = outcome && outcome.status !== 'pending' ? outcome.status : null;
  const appr = approval?.status ?? null;

  const select: StageState = run ? (noRunbook ? 'failed' : 'done') : running ? 'active' : 'pending';
  const simulate: StageState = noRunbook ? 'skipped' : run ? 'done' : 'pending';
  let execute: StageState;
  if (noRunbook) execute = 'skipped';
  else if (!run) execute = 'pending';
  else if (final === 'failed') execute = 'failed';
  else if (final === 'denied') execute = 'skipped';
  else if (final) execute = 'done';
  else execute = 'active';
  let hitl: StageState;
  if (noRunbook || !hasDestructive) hitl = 'skipped';
  else if (final === 'denied' || appr === 'denied' || appr === 'expired') hitl = 'failed';
  else if (final === 'resolved' || final === 'rolled_back' || appr === 'approved') hitl = 'done';
  else if (running) hitl = 'active';
  else hitl = 'pending';
  let verify: StageState;
  if (noRunbook) verify = 'skipped';
  else if (final === 'resolved') verify = 'done';
  else if (final === 'denied') verify = 'skipped';
  else if (final === 'failed' || final === 'rolled_back') verify = 'failed';
  else if (run && hitl === 'done') verify = 'active';
  else verify = 'pending';
  let rollback: StageState;
  if (noRunbook) rollback = 'skipped';
  else if (final === 'rolled_back' || final === 'resolved') rollback = 'done';
  else if (final === 'failed') rollback = 'failed';
  else rollback = 'pending';

  return [select, simulate, execute, hitl, verify, rollback];
}

export default function RunbookExecutor() {
  const catalog = getAgentById('runbook-executor');
  const incidents = useFetch(() => api.verdicts({ limit: 20 }), { intervalMs: 5000 });

  const [activeIncident, setActiveIncident] = useState<VerdictRecord | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [run, setRun] = useState<RunbookRunResponse | null>(null);
  const [outcome, setOutcome] = useState<RunbookOutcome | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);

  const approvalId = run?.approval_id ?? null;

  const start = useCallback(async (incident: VerdictRecord) => {
    setActiveIncident(incident);
    setRunErr(null);
    setOutcome(null);
    setApproval(null);
    setRun(null);
    setPhase('running');
    try {
      const res = await api.runbookExecutorRun({
        service: incident.affected_service,
        severity: sevToken(incident.severity),
        incident_id: incident.incident_id || `verdict-${incident.id}`,
        summary: incident.alert_summary,
      });
      setRun(res);
      if (res.status === 'no_runbook') setPhase('done');
    } catch (e) {
      setRunErr(e instanceof Error ? e.message : String(e));
      setPhase('idle');
    }
  }, []);

  const reset = useCallback(() => {
    setPhase('idle');
    setRun(null);
    setOutcome(null);
    setApproval(null);
    setRunErr(null);
    setActiveIncident(null);
  }, []);

  // Poll the outcome store (+ the approval record) while a gated run is live.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    if (phase !== 'running' || !approvalId) return () => { aliveRef.current = false; };
    const tick = async () => {
      try {
        const oc = await api.runbookOutcome(approvalId);
        if (!aliveRef.current) return;
        setOutcome(oc);
        if (oc.status !== 'pending') { setPhase('done'); return; }
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

  return (
    <div className="space-y-6">
      <PageHeader
        summary={catalog?.summary ?? 'Execute a safe runbook when policy allows it.'}
        phase={phase}
        finalStatus={outcome && outcome.status !== 'pending' ? outcome.status : null}
      />

      <IncidentList
        incidents={incidents.data?.verdicts ?? []}
        loading={incidents.loading && !incidents.data}
        error={incidents.error}
        activeId={activeIncident?.id ?? null}
        running={phase === 'running'}
        onRun={start}
        renderAfter={(v) =>
          activeIncident?.id === v.id && (run || runErr) ? (
            <RunDetail
              run={run}
              outcome={outcome}
              approval={approval}
              phase={phase}
              runErr={runErr}
              onReset={reset}
            />
          ) : null
        }
      />

      {!run && !activeIncident && catalog && <AboutCard howItWorks={catalog.howItWorks ?? []} />}
    </div>
  );
}

// The full execution view — rendered inline, indented directly beneath the
// incident row it belongs to (so it's obvious which incident it's for).
function RunDetail({
  run, outcome, approval, phase, runErr, onReset,
}: {
  run: RunbookRunResponse | null;
  outcome: RunbookOutcome | null;
  approval: ApprovalRecord | null;
  phase: Phase;
  runErr: string | null;
  onReset: () => void;
}) {
  const stages = computeStages(run, outcome, approval, phase);
  const noRunbook = run?.status === 'no_runbook' || outcome?.status === 'no_runbook';
  const hasDestructive = run?.planned_steps.some((s) => s.destructive) ?? false;
  const awaitingApproval = phase === 'running' && hasDestructive && approval?.status !== 'approved';
  const completed = stages.filter((s) => s === 'done' || s === 'skipped').length;
  const hasActive = stages.includes('active');
  const percent = Math.round(((completed + (hasActive ? 0.5 : 0)) / STAGES.length) * 100);

  return (
    <div className="mt-1 space-y-4 border-l-2 border-accent/40 pl-4">
      <div className="flex items-center justify-between">
        <span className="chip !border-accent/40 !text-accent font-mono">execution</span>
        <button onClick={onReset} disabled={phase === 'running'} className="btn !py-1 !text-xs">
          <RotateCcw className="h-3.5 w-3.5" /> Clear
        </button>
      </div>

      {runErr && <ErrorState error={runErr} />}

      {run && (
        <>
          <StageProgress stages={stages} percent={percent} awaiting={awaitingApproval} />
          <SelectionCard run={run} stageState={stages[0]} />
          {!noRunbook && <DryRunCard steps={run.planned_steps} />}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="Selected runbook" value={run.selected_runbook ? '1' : '0'}
              icon={<FileText className="h-4 w-4" />} intent={noRunbook ? 'bad' : 'ok'}
              hint={run.runbook_title ?? (noRunbook ? 'no runbook matched' : '—')}
            />
            <StatCard
              label="Planned steps" value={outcome?.steps_total ?? run.planned_steps.length}
              icon={<Cog className="h-4 w-4" />}
              hint={`${run.planned_steps.filter((s) => s.destructive).length} destructive`}
            />
            <StatCard
              label="Executed" value={outcome?.steps_executed ?? '—'}
              icon={<CheckCircle2 className="h-4 w-4" />}
              intent={outcome?.status === 'resolved' ? 'ok' : 'default'}
              hint={phase === 'running' ? 'in progress…' : 'steps applied'}
            />
            <StatCard
              label="Resolution"
              value={outcome && outcome.status !== 'pending' ? outcome.status : awaitingApproval ? 'awaiting' : 'running'}
              icon={<ShieldCheck className="h-4 w-4" />} intent={statusIntent(outcome?.status)}
              hint={outcome?.reason || `incident ${run.incident_id}`}
            />
          </div>
          {awaitingApproval && <HitlPanel approvalId={run.approval_id} approval={approval} />}
          <StepList run={run} outcome={outcome} />
        </>
      )}
    </div>
  );
}

function statusIntent(status?: string): 'default' | 'ok' | 'warn' | 'bad' {
  switch (status) {
    case 'resolved': return 'ok';
    case 'rolled_back': case 'denied': return 'warn';
    case 'failed': case 'no_runbook': return 'bad';
    default: return 'default';
  }
}

// ─── header ──────────────────────────────────────────────────────────────────
function PageHeader({ summary, phase, finalStatus }: { summary: string; phase: Phase; finalStatus: string | null }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">Runbook Executor</h1>
          <span className="chip font-mono">RA-004</span>
          <span className="chip !border-warn/40 !text-warn"><ShieldCheck className="h-3 w-3" /> HITL Required</span>
        </div>
        <p className="mt-1 max-w-2xl text-sm text-ink-500 dark:text-ink-400">{summary}</p>
      </div>
      <div className="flex items-center gap-2">
        <span className={clsx('chip',
          phase === 'running' && '!border-accent/40 !text-accent',
          finalStatus === 'resolved' && '!border-ok/40 !text-ok',
          (finalStatus === 'failed' || finalStatus === 'no_runbook') && '!border-bad/40 !text-bad',
          (finalStatus === 'denied' || finalStatus === 'rolled_back') && '!border-warn/40 !text-warn',
        )}>
          <span className={clsx('h-1.5 w-1.5 rounded-full',
            phase === 'running' ? 'bg-accent animate-pulse-slow'
              : phase === 'done' ? (finalStatus === 'resolved' ? 'bg-ok' : 'bg-warn') : 'bg-ink-400')} />
          {phase === 'idle' ? 'idle' : phase === 'running' ? 'running' : (finalStatus ?? 'done')}
        </span>
      </div>
    </div>
  );
}

// ─── live triaged incidents (the injected failures) ─────────────────────────────
function IncidentList({
  incidents, loading, error, activeId, running, onRun, renderAfter,
}: {
  incidents: VerdictRecord[];
  loading: boolean;
  error: string | null;
  activeId: number | null;
  running: boolean;
  onRun: (v: VerdictRecord) => void;
  // Rendered immediately below each row — used to slot the execution detail in
  // under the incident it belongs to.
  renderAfter?: (v: VerdictRecord) => ReactNode;
}) {
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2 className="card-title">Triaged incidents</h2>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Failures injected in the Operations Console appear here once triage assigns a severity.
          </p>
        </div>
        <span className="chip font-mono">{incidents.length}</span>
      </div>
      <div className="card-body space-y-2">
        {loading ? (
          <LoadingState label="Loading incidents…" />
        ) : error ? (
          <ErrorState error={error} />
        ) : incidents.length === 0 ? (
          <EmptyState
            icon={<Inbox className="h-7 w-7" />}
            label="No triaged incidents yet"
            hint="Inject a failure from the Operations Console (Overview → Failure injection). Once it fires and Alert Triage assigns a severity, it shows up here to run a runbook against."
          />
        ) : (
          incidents.map((v) => (
            <Fragment key={v.id}>
              <IncidentRow v={v} active={activeId === v.id} running={running} onRun={() => onRun(v)} />
              {renderAfter?.(v)}
            </Fragment>
          ))
        )}
      </div>
    </div>
  );
}

function IncidentRow({ v, active, running, onRun }: { v: VerdictRecord; active: boolean; running: boolean; onRun: () => void }) {
  const when = v.audit_metadata?.created_at ? timeAgo(v.audit_metadata.created_at) : null;
  return (
    <div className={clsx(
      'flex items-start gap-3 rounded-lg border p-3 transition-all',
      active ? 'border-accent bg-accent/5 ring-1 ring-accent/30' : 'border-ink-200 bg-ink-50/40 dark:border-ink-700 dark:bg-ink-900/40',
    )}>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={v.severity} />
          <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{v.affected_service}</span>
          {v.status === 'Active'
            ? <span className="chip !border-ok/40 !text-ok">active</span>
            : <span className="chip !border-warn/40 !text-warn">suppressed</span>}
          {when && <span className="text-[11px] text-ink-500 dark:text-ink-400">· {when}</span>}
        </div>
        <p className="mt-1 break-words text-xs text-ink-600 dark:text-ink-400">{v.alert_summary}</p>
        <p className="mt-1 font-mono text-[10px] text-ink-500 dark:text-ink-400">
          {v.incident_id && <>incident <span className="text-ink-700 dark:text-ink-300">{v.incident_id}</span> · </>}
          team <span className="text-ink-700 dark:text-ink-300">{v.assigned_team}</span>
          {v.recommended_runbook && <> · triage hint <span className="text-ink-700 dark:text-ink-300">{v.recommended_runbook}</span></>}
        </p>
      </div>
      <button onClick={onRun} disabled={running} className="btn btn-primary flex-shrink-0 !py-1 !text-xs">
        {running && active ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlayCircle className="h-3.5 w-3.5" />}
        Run runbook
      </button>
    </div>
  );
}

// ─── six-stage progress ────────────────────────────────────────────────────────
const STAGE_TONE: Record<StageState, { ring: string; icon: string; bg: string }> = {
  pending: { ring: 'border-ink-200 dark:border-ink-700', icon: 'text-ink-400 dark:text-ink-500', bg: 'bg-white dark:bg-ink-800' },
  active:  { ring: 'border-accent', icon: 'text-accent', bg: 'bg-accent/10' },
  done:    { ring: 'border-ok', icon: 'text-ok', bg: 'bg-ok/10' },
  failed:  { ring: 'border-bad', icon: 'text-bad', bg: 'bg-bad/10' },
  skipped: { ring: 'border-ink-200 dark:border-ink-700', icon: 'text-ink-400 dark:text-ink-600', bg: 'bg-ink-50 dark:bg-ink-900/40' },
};

function StageProgress({ stages, percent, awaiting }: { stages: StageState[]; percent: number; awaiting: boolean }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">Workflow · 6 stages</h2>
        <span className="chip font-mono">{percent}%</span>
      </div>
      <div className="card-body space-y-5">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-900">
          <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${percent}%` }} />
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {STAGES.map((stage, i) => {
            const state = stages[i];
            const tone = STAGE_TONE[state];
            const Icon = stage.icon;
            return (
              <div key={stage.key} className="flex flex-col items-center text-center">
                <div className={clsx('relative flex h-12 w-12 items-center justify-center rounded-full border-2 transition-colors',
                  tone.ring, tone.bg, state === 'active' && 'animate-pulse-slow')}>
                  {state === 'done' ? <CheckCircle2 className={clsx('h-5 w-5', tone.icon)} />
                    : state === 'failed' ? <XCircle className={clsx('h-5 w-5', tone.icon)} />
                    : state === 'skipped' ? <SkipForward className={clsx('h-4 w-4', tone.icon)} />
                    : state === 'active' ? <Loader2 className={clsx('h-5 w-5 animate-spin', tone.icon)} />
                    : <Icon className={clsx('h-5 w-5', tone.icon)} />}
                  <span className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-ink-100 font-mono text-[9px] font-bold text-ink-500 dark:bg-ink-700 dark:text-ink-300">{i + 1}</span>
                </div>
                <p className="mt-2 text-xs font-semibold text-ink-800 dark:text-ink-100">{stage.label}</p>
                <p className="mt-0.5 text-[10px] leading-tight text-ink-500 dark:text-ink-400">{stage.desc}</p>
              </div>
            );
          })}
        </div>
        {awaiting && (
          <div className="flex items-center gap-2 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn">
            <Clock className="h-3.5 w-3.5 animate-pulse-slow" />
            Paused at the human-approval gate — destructive step held until a human decides.
          </div>
        )}
      </div>
    </div>
  );
}

// ─── stage 1 detail: which runbook was selected + the match criteria ────────────
function SelectionCard({ run, stageState }: { run: RunbookRunResponse; stageState: StageState }) {
  const noRunbook = run.status === 'no_runbook';
  // Older backend builds don't return matched_on — fall back to the service.
  const matched = run.matched_on ?? { service: run.service, severity: null, tags: [] };
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">① Select runbook</h2>
        <span className={clsx('chip font-mono', stageState === 'done' ? '!border-ok/40 !text-ok' : '!border-bad/40 !text-bad')}>
          {noRunbook ? 'no match' : 'matched'}
        </span>
      </div>
      <div className="card-body space-y-3">
        {noRunbook ? (
          <div className="flex items-center gap-3 text-sm text-ink-600 dark:text-ink-300">
            <AlertTriangle className="h-5 w-5 flex-shrink-0 text-bad" />
            No runbook in the library matches <span className="font-mono">{matched.service}</span> — nothing to execute.
          </div>
        ) : (
          <div className="flex items-start gap-3">
            <FileText className="mt-0.5 h-5 w-5 flex-shrink-0 text-accent" />
            <div className="min-w-0">
              <p className="text-sm font-semibold text-ink-900 dark:text-ink-50">{run.runbook_title}</p>
              <p className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{run.selected_runbook}</p>
            </div>
          </div>
        )}
        <div>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">Matched on</p>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="chip">service · {matched.service}</span>
            <span className="chip">severity · {matched.severity ?? '—'}</span>
            {matched.tags.length === 0
              ? <span className="chip">tags · none</span>
              : matched.tags.map((t) => <span key={t} className="chip">{t}</span>)}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── stage 2 detail: dry-run preview of every step (read-only, no changes) ──────
function DryRunCard({ steps }: { steps: PlannedStep[] }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">② Dry-run · preview (no changes made)</h2>
        <span className="chip font-mono">{steps.length} steps</span>
      </div>
      <div className="card-body space-y-2">
        {steps.map((s, i) => {
          const sim = s.simulate ?? {};
          const preview = (sim.error as string | undefined) ?? sim.preview ?? '(no preview returned)';
          const changes = Array.isArray(sim.changes) ? sim.changes : [];
          return (
            <div key={s.name} className="rounded-lg border border-ink-200 bg-ink-50/40 p-3 dark:border-ink-700 dark:bg-ink-900/40">
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-ink-100 font-mono text-[10px] font-semibold text-ink-500 dark:bg-ink-700 dark:text-ink-300">{i + 1}</span>
                <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{s.name}</span>
                <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{s.action}</span>
                {s.destructive && <span className="chip !border-bad/40 !text-bad">destructive</span>}
                <span className="chip !border-ok/40 !text-ok">{sim.error ? 'error' : 'dry-run ok'}</span>
              </div>
              <p className="mt-1.5 break-words font-mono text-[11px] text-ink-600 dark:text-ink-300">{preview}</p>
              <p className="mt-0.5 font-mono text-[10px] text-ink-500 dark:text-ink-500">
                changes: {changes.length === 0 ? 'none (read-only preview)' : JSON.stringify(changes)}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── HITL panel (links out to the standalone approver console) ──────────────────
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
            <p>A destructive step is held at the platform HITL gate. Open the approver console to
              approve or deny it — this page updates automatically when you return.</p>
            <p className="mt-1.5 font-mono text-[11px] text-ink-500 dark:text-ink-400">
              approval id <span className="text-ink-700 dark:text-ink-300">{approvalId}</span>
              {approval?.action && <> · action <span className="text-ink-700 dark:text-ink-300">{approval.action}</span></>}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <a href="/hitl" target="_blank" rel="noreferrer" className="btn btn-primary">
            <ExternalLink className="h-4 w-4" /> Open HITL approver console
          </a>
          <span className="inline-flex items-center gap-1.5 text-xs text-ink-500 dark:text-ink-400">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> waiting for a decision…
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── step list (execution outcome) ──────────────────────────────────────────────
const STEP_BADGE: Record<string, string> = {
  executed: '!border-ok/40 !text-ok', denied: '!border-warn/40 !text-warn',
  failed: '!border-bad/40 !text-bad', rolled_back: '!border-warn/40 !text-warn',
  skipped: '', planned: '',
};

function StepList({ run, outcome }: { run: RunbookRunResponse; outcome: RunbookOutcome | null }) {
  const byName = new Map<string, RunbookStepRecord>();
  for (const r of outcome?.steps ?? []) byName.set(r.name, r);

  const rows = run.planned_steps.map((p) => {
    const rec = byName.get(p.name);
    return {
      name: p.name, action: p.action, destructive: p.destructive,
      status: rec?.status ?? (outcome && outcome.status !== 'pending' ? 'skipped' : 'planned'),
      detail: rec ? (rec.error ?? (rec.executed?.stdout as string | undefined)) : undefined,
      rolledBack: rec?.rolled_back ?? false,
    };
  });

  if (run.status === 'no_runbook') return null;

  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">③ Execution · step results</h2>
        {run.runbook_title && <span className="chip">{run.runbook_title}</span>}
      </div>
      <div className="card-body space-y-2">
        {rows.map((r, i) => (
          <div key={r.name} className="flex items-start gap-3 rounded-lg border border-ink-200 bg-ink-50/40 p-3 dark:border-ink-700 dark:bg-ink-900/40">
            <span className="mt-0.5 flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-ink-100 font-mono text-[11px] font-semibold text-ink-500 dark:bg-ink-700 dark:text-ink-300">{i + 1}</span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{r.name}</span>
                <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{r.action}</span>
                {r.destructive && <span className="chip !border-bad/40 !text-bad">destructive</span>}
                {r.rolledBack && <span className="chip !border-warn/40 !text-warn">rolled back</span>}
              </div>
              {r.detail && <p className="mt-1 break-words font-mono text-[11px] text-ink-500 dark:text-ink-400">{r.detail}</p>}
            </div>
            <span className={clsx('chip flex-shrink-0 font-mono', STEP_BADGE[r.status] ?? '')}>{r.status}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── about (idle state) ─────────────────────────────────────────────────────────
function AboutCard({ howItWorks }: { howItWorks: string[] }) {
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
        <p className="mt-4 text-xs text-ink-500 dark:text-ink-400">
          Inject a failure from the Operations Console; once Alert Triage assigns it a severity it
          appears above. Run a runbook against it to watch the six stages — with the destructive
          step held at the human-approval gate.
        </p>
      </div>
    </div>
  );
}
