import { Fragment, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import {
  PlayCircle, RotateCcw, Search, FlaskConical, Cog, UserCheck, BadgeCheck, Undo2,
  CheckCircle2, XCircle, Loader2, ExternalLink, ShieldCheck, AlertTriangle, Clock,
  SkipForward, FileText, Inbox, Calendar, ChevronDown, ChevronUp, Ban, Activity,
  GitCompare,
} from 'lucide-react';
import StatCard from '@/components/StatCard';
import { SeverityBadge } from '@/components/SeverityBadge';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { useFetch } from '@/hooks/useFetch';
import { api } from '@/lib/api';
import { getAgentById } from '@/data/agentCatalog';
import { clsx, timeAgo } from '@/lib/format';
import type {
  ApprovalRecord, AuditEvent, AuditEventType, PlannedStep, RunbookOutcome,
  RunbookRunResponse, RunbookStepRecord, SimulationComparison, Severity, VerdictRecord,
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
  const incidents = useFetch(() => api.verdicts({ limit: 20 }), { intervalMs: 5000, cacheKey: 'verdicts-20' });

  const [activeIncident, setActiveIncident] = useState<VerdictRecord | null>(null);
  // Which incident's runbook picker is open (lets the operator review steps and
  // pick a runbook other than the auto-selected match before running).
  const [pickerFor, setPickerFor] = useState<number | null>(null);
  // Date filter for the triaged-incident list (same pattern as the Knowledge
  // base calendar). "" = all dates.
  const [dayFilter, setDayFilter] = useState('');
  // Whether the execution detail of the just-run incident is expanded. After a
  // run finishes the row swaps its "Choose runbook" button for a chevron that
  // toggles this — to review what executed and the process it went through.
  const [detailOpen, setDetailOpen] = useState(true);
  const [phase, setPhase] = useState<Phase>('idle');
  const [run, setRun] = useState<RunbookRunResponse | null>(null);
  const [outcome, setOutcome] = useState<RunbookOutcome | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);

  const approvalId = run?.approval_id ?? null;

  const start = useCallback(async (incident: VerdictRecord, runbookId?: string) => {
    setActiveIncident(incident);
    setPickerFor(null);
    setDetailOpen(true);
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
        runbook_id: runbookId,
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
        pickerFor={pickerFor}
        running={phase === 'running'}
        executedId={phase === 'done' ? activeIncident?.id ?? null : null}
        detailOpen={detailOpen}
        onToggleDetail={() => setDetailOpen((o) => !o)}
        dayFilter={dayFilter}
        onDayFilter={setDayFilter}
        onChoose={(v) => setPickerFor((cur) => (cur === v.id ? null : v.id))}
        renderAfter={(v) => {
          if (activeIncident?.id === v.id && (run || runErr)) {
            // While running always show; once done, only when expanded.
            if (phase === 'done' && !detailOpen) return null;
            return (
              <RunDetail
                run={run}
                outcome={outcome}
                approval={approval}
                phase={phase}
                runErr={runErr}
                onReset={reset}
              />
            );
          }
          if (pickerFor === v.id) {
            return (
              <RunbookPicker
                incident={v}
                onRun={(rbId) => start(v, rbId)}
                onCancel={() => setPickerFor(null)}
              />
            );
          }
          return null;
        }}
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
          <StepList run={run} outcome={outcome} approval={approval} phase={phase} />
          {outcome?.verification && <VerifyCard verification={outcome.verification} />}
          {outcome?.audit_events && outcome.audit_events.length > 0 && (
            <AuditTimeline events={outcome.audit_events} />
          )}
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
  incidents, loading, error, activeId, pickerFor, running, executedId, detailOpen, onToggleDetail,
  dayFilter, onDayFilter, onChoose, renderAfter,
}: {
  incidents: VerdictRecord[];
  loading: boolean;
  error: string | null;
  activeId: number | null;
  pickerFor: number | null;
  running: boolean;
  executedId: number | null;
  detailOpen: boolean;
  onToggleDetail: () => void;
  dayFilter: string;
  onDayFilter: (d: string) => void;
  onChoose: (v: VerdictRecord) => void;
  // Rendered immediately below each row — used to slot the execution detail in
  // under the incident it belongs to.
  renderAfter?: (v: VerdictRecord) => ReactNode;
}) {
  const filterActive = Boolean(dayFilter);
  // Filter by the verdict's created_at day (YYYY-MM-DD), same as the KB calendar.
  const shown = filterActive
    ? incidents.filter((v) => (v.audit_metadata?.created_at ?? '').slice(0, 10) === dayFilter)
    : incidents;
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2 className="card-title">Triaged incidents</h2>
          <p className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
            Failures injected in the Operations Console appear here once triage assigns a severity.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Calendar className="h-4 w-4 text-ink-400" />
          <input
            type="date"
            value={dayFilter}
            onChange={(e) => onDayFilter(e.target.value)}
            title="Filter by date"
            className="rounded-lg border border-ink-200 bg-white px-2 py-1 text-xs text-ink-700 dark:border-ink-700 dark:bg-ink-800 dark:text-ink-200"
          />
          {filterActive && (
            <button
              onClick={() => onDayFilter('')}
              className="rounded-lg border border-ink-200 px-2 py-1 text-xs text-ink-600 hover:bg-ink-100 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800"
            >
              All dates
            </button>
          )}
          <span className="chip font-mono">
            {shown.length}{filterActive ? ` of ${incidents.length}` : ''}
          </span>
        </div>
      </div>
      <div className="card-body space-y-2">
        {loading ? (
          <LoadingState label="Loading incidents…" />
        ) : error ? (
          <ErrorState error={error} />
        ) : shown.length === 0 ? (
          <EmptyState
            icon={<Inbox className="h-7 w-7" />}
            label={filterActive ? 'No incidents for the selected date' : 'No triaged incidents yet'}
            hint={
              filterActive
                ? 'Pick a different date or clear the filter with “All dates”.'
                : 'Inject a failure from the Operations Console (Overview → Failure injection). Once it fires and Alert Triage assigns a severity, it shows up here to run a runbook against.'
            }
          />
        ) : (
          shown.map((v) => (
            <Fragment key={v.id}>
              <IncidentRow
                v={v}
                active={activeId === v.id}
                pickerOpen={pickerFor === v.id}
                running={running}
                executed={executedId === v.id}
                detailOpen={detailOpen}
                onToggleDetail={onToggleDetail}
                onChoose={() => onChoose(v)}
              />
              {renderAfter?.(v)}
            </Fragment>
          ))
        )}
      </div>
    </div>
  );
}

function IncidentRow({ v, active, pickerOpen, running, executed, detailOpen, onToggleDetail, onChoose }: { v: VerdictRecord; active: boolean; pickerOpen: boolean; running: boolean; executed: boolean; detailOpen: boolean; onToggleDetail: () => void; onChoose: () => void }) {
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
      {executed ? (
        // Run finished for this incident — swap the action button for an
        // expand/collapse chevron to review what executed and how.
        <button
          onClick={onToggleDetail}
          title={detailOpen ? 'Collapse execution detail' : 'Show what was executed'}
          aria-label={detailOpen ? 'Collapse execution detail' : 'Show what was executed'}
          className="btn flex-shrink-0 !px-2 !py-1"
        >
          {detailOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      ) : (
        <button
          onClick={onChoose}
          disabled={running}
          className={clsx(
            'btn flex-shrink-0 !py-1 !text-xs',
            pickerOpen ? '!border-accent/50 !text-accent' : 'btn-primary',
          )}
        >
          {running && active ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
          {pickerOpen ? 'Hide runbooks' : 'Choose runbook'}
        </button>
      )}
    </div>
  );
}

// ─── runbook picker: review steps + choose a runbook other than the match ──────
function RunbookPicker({
  incident, onRun, onCancel,
}: {
  incident: VerdictRecord;
  onRun: (runbookId: string) => void;
  onCancel: () => void;
}) {
  // Keyed per incident; fetches once and restores from cache on re-selection.
  const lib = useFetch(
    () =>
      api.runbookExecutorRunbooks({
        service: incident.affected_service,
        severity: sevToken(incident.severity),
        summary: incident.alert_summary,
      }),
    { cacheKey: `runbooks-${incident.id}` },
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const runbooks = lib.data?.runbooks ?? [];
  // Default the selection to the recommended runbook once loaded.
  const recommended = lib.data?.recommended ?? null;
  const chosen = selected ?? recommended ?? (runbooks[0]?.id ?? null);

  return (
    <div className="mt-1 space-y-3 border-l-2 border-accent/40 pl-4">
      <div className="flex items-center justify-between">
        <span className="chip !border-accent/40 !text-accent font-mono">choose runbook</span>
        <button onClick={onCancel} className="btn !py-1 !text-xs">
          <XCircle className="h-3.5 w-3.5" /> Cancel
        </button>
      </div>

      {lib.loading && !lib.data ? (
        <LoadingState label="Loading runbooks…" />
      ) : lib.error ? (
        <ErrorState error={lib.error} />
      ) : runbooks.length === 0 ? (
        <EmptyState
          icon={<Inbox className="h-7 w-7" />}
          label="No runbooks in the library"
          hint="The executor library has no runbooks to choose from."
        />
      ) : (
        <>
          <div className="space-y-2">
            {runbooks.map((rb) => {
              const isChosen = chosen === rb.id;
              const isExpanded = expanded === rb.id;
              const destructive = rb.steps.filter((s) => s.destructive).length;
              return (
                <div
                  key={rb.id}
                  className={clsx(
                    'rounded-lg border p-3 transition-colors',
                    isChosen ? 'border-accent bg-accent/5 ring-1 ring-accent/30' : 'border-ink-200 dark:border-ink-700',
                  )}
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="radio"
                      name={`rb-${incident.id}`}
                      checked={isChosen}
                      onChange={() => setSelected(rb.id)}
                      className="mt-1 accent-accent"
                    />
                    <button
                      type="button"
                      onClick={() => setSelected(rb.id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{rb.title}</span>
                        {rb.recommended && <span className="chip !border-ok/40 !text-ok">recommended</span>}
                        {rb.matches_service && !rb.recommended && <span className="chip !border-accent/40 !text-accent">service match</span>}
                      </div>
                      <p className="mt-0.5 font-mono text-[10px] text-ink-500 dark:text-ink-400">
                        {rb.id} · {rb.steps.length} step{rb.steps.length === 1 ? '' : 's'}
                        {destructive > 0 && <> · {destructive} destructive</>}
                        {' · '}service {rb.service}
                      </p>
                    </button>
                    <button
                      type="button"
                      onClick={() => setExpanded(isExpanded ? null : rb.id)}
                      className="btn !py-1 !text-xs flex-shrink-0"
                    >
                      {isExpanded ? 'Hide steps' : 'Review steps'}
                    </button>
                  </div>
                  {isExpanded && (
                    <ol className="mt-2 space-y-1 border-l border-ink-200 pl-3 dark:border-ink-700">
                      {rb.steps.map((s, i) => (
                        <li key={s.name} className="flex flex-wrap items-center gap-2 text-xs">
                          <span className="font-mono text-ink-400">{i + 1}.</span>
                          <span className="font-semibold text-ink-800 dark:text-ink-100">{s.name}</span>
                          <span className="font-mono text-[10px] text-ink-500 dark:text-ink-400">{s.action}</span>
                          {s.destructive && <span className="chip !border-bad/40 !text-bad !text-[10px]">destructive</span>}
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              );
            })}
          </div>
          <button
            onClick={() => chosen && onRun(chosen)}
            disabled={!chosen}
            className="btn btn-primary !py-1 !text-xs"
          >
            <PlayCircle className="h-3.5 w-3.5" /> Run runbook
          </button>
        </>
      )}
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
          const preview = sim.error ?? sim.summary ?? sim.preview ?? '(no preview returned)';
          const changes = Array.isArray(sim.changes) ? sim.changes : [];
          const predictedActions = sim.predicted_actions ?? [];
          const predictedSE = sim.predicted_side_effects ?? [];
          const warnings = sim.warnings ?? [];
          const estMs = typeof sim.estimated_duration_ms === 'number' ? sim.estimated_duration_ms : null;
          return (
            <div key={s.name} className="rounded-lg border border-ink-200 bg-ink-50/40 p-3 dark:border-ink-700 dark:bg-ink-900/40">
              <div className="flex flex-wrap items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-ink-100 font-mono text-[10px] font-semibold text-ink-500 dark:bg-ink-700 dark:text-ink-300">{i + 1}</span>
                <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{s.name}</span>
                <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{s.action}</span>
                {s.destructive && <span className="chip !border-bad/40 !text-bad">destructive</span>}
                {estMs != null && <span className="chip !text-[10px] font-mono">~{estMs}ms</span>}
                <span className="chip !border-ok/40 !text-ok">{sim.error ? 'error' : 'dry-run ok'}</span>
              </div>
              <p className="mt-1.5 break-words font-mono text-[11px] text-ink-600 dark:text-ink-300">{preview}</p>
              <div className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
                <PredictField label="predicted actions" items={predictedActions} />
                <PredictField label="predicted side effects" items={predictedSE} />
              </div>
              {warnings.length > 0 && (
                <p className="mt-1 flex items-start gap-1.5 font-mono text-[10px] text-warn">
                  <AlertTriangle className="mt-px h-3 w-3 flex-shrink-0" /> {warnings.join(' · ')}
                </p>
              )}
              <p className="mt-1 font-mono text-[10px] text-ink-500 dark:text-ink-500">
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
  // live (in-progress) statuses
  executing: '!border-accent/50 !text-accent', awaiting: '!border-warn/50 !text-warn', queued: '',
};

function StepList({
  run, outcome, approval, phase,
}: {
  run: RunbookRunResponse;
  outcome: RunbookOutcome | null;
  approval: ApprovalRecord | null;
  phase: Phase;
}) {
  const byName = new Map<string, RunbookStepRecord>();
  for (const r of outcome?.steps ?? []) byName.set(r.name, r);

  const steps = run.planned_steps;
  const finalKnown = !!(outcome && outcome.status !== 'pending');
  const approved = approval?.status === 'approved';
  const firstDestructiveIdx = steps.findIndex((s) => s.destructive);

  // Live execution cursor: walk the steps while running so each one shows as it
  // runs (the mock backend is instant and only emits the final outcome, so we
  // pace the reveal here). The cursor halts at the destructive step until the
  // HITL approval lands, then resumes; once the real outcome arrives it snaps
  // to the actual per-step results.
  const [cursor, setCursor] = useState(0);
  useEffect(() => {
    if (run.status === 'no_runbook') return;
    if (finalKnown) { setCursor(steps.length); return; }
    if (phase !== 'running') return;
    // Upper bound the cursor can reach right now: stop AT the destructive step
    // until it's approved; otherwise advance through all steps.
    const limit = !approved && firstDestructiveIdx >= 0 ? firstDestructiveIdx : steps.length;
    if (cursor >= limit) return; // caught up / paused at the gate
    const t = setTimeout(() => setCursor((c) => Math.min(c + 1, limit)), 800);
    return () => clearTimeout(t);
  }, [phase, approved, finalKnown, cursor, firstDestructiveIdx, steps.length, run.status]);

  if (run.status === 'no_runbook') return null;

  // Resolve each step's display status: real result once final, otherwise the
  // animated state derived from the cursor.
  const view = (i: number): { status: string; spinner: boolean } => {
    if (finalKnown) {
      const rec = byName.get(steps[i].name);
      return { status: rec?.status ?? 'skipped', spinner: false };
    }
    if (i < cursor) return { status: 'executed', spinner: false };
    if (i === cursor) {
      if (steps[i].destructive && !approved) return { status: 'awaiting', spinner: false };
      return { status: 'executing', spinner: true };
    }
    return { status: 'queued', spinner: false };
  };

  const doneCount = finalKnown ? steps.length : Math.min(cursor, steps.length);

  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">③ Execution · step results</h2>
        <span className="chip font-mono">
          {finalKnown ? (outcome?.status ?? 'done') : `${doneCount}/${steps.length}`}
        </span>
      </div>
      <div className="card-body space-y-2">
        {steps.map((p, i) => {
          const rec = byName.get(p.name);
          const { status, spinner } = view(i);
          const detail = rec ? (rec.error ?? (rec.executed?.stdout as string | undefined)) : undefined;
          const rolledBack = rec?.rolled_back ?? false;
          const active = spinner || status === 'awaiting';
          return (
            <div
              key={p.name}
              className={clsx(
                'flex items-start gap-3 rounded-lg border p-3 transition-colors',
                active
                  ? 'border-accent/50 bg-accent/5'
                  : 'border-ink-200 bg-ink-50/40 dark:border-ink-700 dark:bg-ink-900/40',
              )}
            >
              <span className="mt-0.5 flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-ink-100 font-mono text-[11px] font-semibold text-ink-500 dark:bg-ink-700 dark:text-ink-300">
                {spinner ? <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
                  : status === 'executed' ? <CheckCircle2 className="h-3.5 w-3.5 text-ok" />
                  : status === 'awaiting' ? <Clock className="h-3.5 w-3.5 animate-pulse-slow text-warn" />
                  : i + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-ink-900 dark:text-ink-50">{p.name}</span>
                  <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{p.action}</span>
                  {p.destructive && <span className="chip !border-bad/40 !text-bad">destructive</span>}
                  {rolledBack && <span className="chip !border-warn/40 !text-warn">rolled back</span>}
                </div>
                {detail && <p className="mt-1 break-words font-mono text-[11px] text-ink-500 dark:text-ink-400">{detail}</p>}
                {rec?.comparison && <ComparisonStrip c={rec.comparison} />}
              </div>
              <span className={clsx('chip flex-shrink-0 font-mono', STEP_BADGE[status] ?? '')}>
                {status === 'awaiting' ? 'awaiting approval' : status}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── stage 5 detail: real post-run verification (flag state re-checked) ─────────
function VerifyCard({ verification }: { verification: NonNullable<RunbookOutcome['verification']> }) {
  const { status, checks, reason } = verification;
  const tone =
    status === 'verified' ? '!border-ok/40 !text-ok'
      : status === 'unverified' ? '!border-bad/40 !text-bad'
      : '!border-ink-300/40 !text-ink-500';
  return (
    <div className="card">
      <div className="card-header">
        <h2 className="card-title">⑤ Verify · resolution check</h2>
        <span className={clsx('chip font-mono', tone)}>{status}</span>
      </div>
      <div className="card-body space-y-2">
        {checks.length === 0 ? (
          <p className="text-xs text-ink-500 dark:text-ink-400">
            {reason ?? 'Nothing to verify for this runbook.'}
          </p>
        ) : (
          <>
            <p className="text-[11px] text-ink-500 dark:text-ink-400">
              Re-read the flags the runbook reset to confirm the injected scenario cleared.
            </p>
            {checks.map((c) => (
              <div
                key={c.flag}
                className="flex items-center justify-between gap-3 rounded-lg border border-ink-200 bg-ink-50/40 p-2.5 text-sm dark:border-ink-700 dark:bg-ink-900/40"
              >
                <span className="flex items-center gap-2">
                  {!c.available ? <Clock className="h-4 w-4 text-ink-400" />
                    : c.ok ? <CheckCircle2 className="h-4 w-4 text-ok" />
                    : <XCircle className="h-4 w-4 text-bad" />}
                  <span className="font-mono text-xs text-ink-900 dark:text-ink-50">{c.flag}</span>
                </span>
                <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">
                  {!c.available ? 'seam unreachable' : `variant = ${c.variant ?? '—'}`}
                </span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

// ─── audit event log (#213 / #217): ordered, immutable event stream ─────────────
const EVENT_TONE: Record<string, { text: string; ring: string; bg: string }> = {
  ink: { text: 'text-ink-500 dark:text-ink-400', ring: 'border-ink-300 dark:border-ink-600', bg: 'bg-ink-100 dark:bg-ink-800' },
  accent: { text: 'text-accent', ring: 'border-accent/40', bg: 'bg-accent/10' },
  ok: { text: 'text-ok', ring: 'border-ok/40', bg: 'bg-ok/10' },
  warn: { text: 'text-warn', ring: 'border-warn/40', bg: 'bg-warn/10' },
  bad: { text: 'text-bad', ring: 'border-bad/40', bg: 'bg-bad/10' },
};

const EVENT_VISUAL: Record<AuditEventType, { icon: typeof Search; tone: string; label: string }> = {
  STEP_STARTED: { icon: PlayCircle, tone: 'ink', label: 'Step started' },
  STEP_SIMULATED: { icon: FlaskConical, tone: 'accent', label: 'Simulated (dry-run)' },
  GATE_CHECKED: { icon: ShieldCheck, tone: 'accent', label: 'Gate checked' },
  HITL_REQUESTED: { icon: UserCheck, tone: 'warn', label: 'Approval requested' },
  HITL_APPROVED: { icon: BadgeCheck, tone: 'ok', label: 'Approved' },
  STEP_EXECUTED: { icon: CheckCircle2, tone: 'ok', label: 'Executed' },
  STEP_FAILED: { icon: XCircle, tone: 'bad', label: 'Failed' },
  STEP_BLOCKED: { icon: Ban, tone: 'warn', label: 'Blocked at HITL gate' },
  STEP_ROLLED_BACK: { icon: Undo2, tone: 'warn', label: 'Rolled back' },
};

function fmtEventTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, { hour12: false });
  } catch {
    return ts;
  }
}

function metaStr(meta: AuditEvent['metadata'], key: string): string {
  const v = meta?.[key];
  return typeof v === 'string' ? v : '';
}

function AuditTimeline({ events }: { events: AuditEvent[] }) {
  return (
    <div className="card">
      <div className="card-header">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-accent" />
          <h2 className="card-title">Audit event log</h2>
        </div>
        <span className="chip font-mono">{events.length} events</span>
      </div>
      <div className="card-body">
        <p className="mb-3 text-[11px] text-ink-500 dark:text-ink-400">
          Append-only, ordered record of the run — every step transition and gate decision, in sequence.
        </p>
        <ol className="space-y-0">
          {events.map((e, i) => {
            const v = EVENT_VISUAL[e.status] ?? { icon: Activity, tone: 'ink', label: e.status };
            const tone = EVENT_TONE[v.tone] ?? EVENT_TONE.ink;
            const Icon = v.icon;
            const last = i === events.length - 1;
            const gate = metaStr(e.metadata, 'gate_type');
            const appr = metaStr(e.metadata, 'approval_id');
            const reason = metaStr(e.metadata, 'reason');
            return (
              <li key={e.seq} className="relative flex gap-3 pb-4 last:pb-0">
                {!last && (
                  <span
                    className="absolute left-[13px] top-7 h-[calc(100%-1rem)] w-px bg-ink-200 dark:bg-ink-700"
                    aria-hidden
                  />
                )}
                <span className={clsx('relative z-10 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full border', tone.ring, tone.bg)}>
                  <Icon className={clsx('h-3.5 w-3.5', tone.text)} />
                </span>
                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-[10px] text-ink-400">#{e.seq}</span>
                    <span className={clsx('text-sm font-semibold', tone.text)}>{v.label}</span>
                    <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">{e.step_id}</span>
                    {gate && <span className="chip !text-[10px]">gate · {gate}</span>}
                    {appr && <span className="chip !text-[10px] font-mono">appr · {appr.slice(0, 8)}</span>}
                    <span className="ml-auto font-mono text-[10px] text-ink-400">{fmtEventTime(e.timestamp)}</span>
                  </div>
                  {reason && <p className="mt-0.5 break-words text-[11px] text-ink-500 dark:text-ink-400">{reason}</p>}
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

// ─── sim-vs-execution comparison strip (#213 / #217) ────────────────────────────
function ComparisonStrip({ c }: { c: SimulationComparison }) {
  const dur = c.duration_delta_ms;
  return (
    <div className="mt-2 rounded-md border border-ink-200 bg-white/60 p-2 dark:border-ink-700 dark:bg-ink-900/50">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
          <GitCompare className="h-3 w-3" /> sim vs actual
        </span>
        <span className={clsx('chip !text-[10px]', c.matched ? '!border-ok/40 !text-ok' : '!border-warn/40 !text-warn')}>
          {c.matched ? 'matched' : 'diverged'}
        </span>
        {dur != null && (
          <span className="font-mono text-[10px] text-ink-500 dark:text-ink-400">
            {c.estimated_duration_ms ?? '?'}ms → {c.actual_duration_ms ?? '?'}ms
            <span className={clsx('ml-1', dur > 0 ? 'text-warn' : 'text-ink-400')}>({dur >= 0 ? '+' : ''}{dur}ms)</span>
          </span>
        )}
      </div>
      <div className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
        <SideEffectList label="predicted" items={c.predicted_side_effects} />
        <SideEffectList label="actual" items={c.actual_side_effects} />
      </div>
      {c.unexpected_side_effects.length > 0 && (
        <p className="mt-1 break-words font-mono text-[10px] text-bad">unexpected: {c.unexpected_side_effects.join(', ')}</p>
      )}
      {c.missing_side_effects.length > 0 && (
        <p className="mt-1 break-words font-mono text-[10px] text-warn">missing: {c.missing_side_effects.join(', ')}</p>
      )}
    </div>
  );
}

function SideEffectList({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="min-w-0">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-ink-400">{label}</span>
      {items.length === 0 ? (
        <span className="ml-1 font-mono text-[10px] text-ink-400">none</span>
      ) : (
        <ul className="mt-0.5 space-y-0.5">
          {items.map((s, i) => (
            <li key={`${i}-${s}`} className="break-words font-mono text-[10px] text-ink-600 dark:text-ink-300">{s}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Predicted actions / side-effects list used by the enriched dry-run card.
function PredictField({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="min-w-0">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-ink-400">{label}</span>
      <ul className="mt-0.5 space-y-0.5">
        {items.map((s, i) => (
          <li key={`${i}-${s}`} className="break-words font-mono text-[10px] text-ink-600 dark:text-ink-300">{s}</li>
        ))}
      </ul>
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
