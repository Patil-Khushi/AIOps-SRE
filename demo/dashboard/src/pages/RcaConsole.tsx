import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Sparkles, RefreshCw, Inbox, Brain } from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, LoadingState, ErrorState } from '@/components/states';
import { SeverityBadge, StatusChip } from '@/components/SeverityBadge';
import { RcaView } from '@/components/RcaView';
import type { TriageVerdict, RCAVerdict } from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';

// ─── RCA Agent console (PRS-008 ★) ──────────────────────────────────────────
//
// This is the RCA Agent's OWN surface — independent of Alert Triage. It takes
// the triage verdicts Alert Triage produced, lets the operator pick one, and
// runs root-cause analysis → ranked fix steps (each with a tested rollback) →
// a REQUIRED-HITL "approve & apply" gate. Alert Triage only generates the
// verdict; everything below the verdict belongs here. The result renderer
// (RcaView) is shared with the Incident Commander console.

export default function RcaConsole() {
  const verdicts = useFetch(api.triageLive, { intervalMs: 0 });
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [rca, setRca] = useState<RCAVerdict | null>(null);
  const [rcaBusy, setRcaBusy] = useState(false);
  const [rcaError, setRcaError] = useState<string | null>(null);

  // Service handed off from Alert Triage's "Generate RCA" button (router state).
  const location = useLocation();
  const wantedService = (location.state as { service?: string } | null)?.service;
  const handedOff = useRef(false);
  const handoffAttempts = useRef(0);

  const results = verdicts.data?.results ?? [];
  const list: TriageVerdict[] = results.map((r) => r.verdict);
  const selected: TriageVerdict | null = list[selectedIdx] ?? null;

  // Clear any RCA when the selected verdict changes so we never show a stale
  // analysis for a different incident.
  useEffect(() => {
    setRca(null);
    setRcaError(null);
  }, [selectedIdx]);

  const runRca = async (target?: TriageVerdict) => {
    const v = target ?? selected;
    if (!v) return;
    setRca(null);
    setRcaError(null);
    setRcaBusy(true);
    try {
      setRca(await api.rca(v));
    } catch (e) {
      setRcaError(e instanceof Error ? e.message : String(e));
    } finally {
      setRcaBusy(false);
    }
  };

  // When arriving from Alert Triage, preselect the matching incident and run
  // RCA automatically — one click on "Generate RCA" lands you on the result.
  useEffect(() => {
    if (handedOff.current || !wantedService) return;
    const idx = list.findIndex((v) => v.affected_service === wantedService);
    if (idx >= 0) {
      handedOff.current = true;
      setSelectedIdx(idx);
      runRca(list[idx]);
      return;
    }
    // The just-triaged verdict may not be in this snapshot yet (intervalMs: 0,
    // one-shot). Retry a few times so the hand-off from "Generate RCA" is
    // deterministic instead of a silent miss when findIndex returns -1.
    if (handoffAttempts.current < 5) {
      const t = setTimeout(() => {
        handoffAttempts.current += 1;
        verdicts.refetch();
      }, 1200);
      return () => clearTimeout(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verdicts.data, wantedService]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            <Brain className="h-6 w-6 text-accent" /> RCA Agent
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            PRS-008 ★ · root-cause analysis → executable fix steps with rollback, gated by human approval.
          </p>
        </div>
        <button onClick={verdicts.refetch} className="btn">
          <RefreshCw className={clsx('h-4 w-4', verdicts.loading && 'animate-spin')} /> Refresh verdicts
        </button>
      </div>

      {verdicts.loading && !verdicts.data ? (
        <div className="card"><LoadingState label="Loading triaged incidents…" /></div>
      ) : verdicts.error ? (
        <div className="card"><ErrorState error={verdicts.error} /></div>
      ) : list.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No triaged incidents yet"
            hint="Inject a scenario on the Overview page — once Alert Triage writes a verdict, it lands here for root-cause analysis."
            icon={<Inbox className="h-7 w-7" />}
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          {/* Incident picker */}
          <div className="lg:col-span-2">
            <ul className="space-y-2">
              {list.map((v, i) => (
                <li key={i}>
                  <button
                    onClick={() => setSelectedIdx(i)}
                    className={clsx(
                      'card w-full text-left transition-all hover:border-accent',
                      i === selectedIdx && '!border-accent ring-1 ring-accent/30',
                    )}
                  >
                    <div className="card-body !py-3">
                      <div className="flex items-center gap-2">
                        <SeverityBadge severity={v.severity} />
                        <StatusChip status={v.status} />
                      </div>
                      <h3 className="mt-1.5 truncate text-sm font-semibold text-ink-900 dark:text-ink-50">
                        {v.affected_service}
                      </h3>
                      <p className="mt-0.5 truncate text-xs text-ink-500 dark:text-ink-400">
                        {v.alert_summary}
                      </p>
                      <p className="mt-0.5 font-mono text-[11px] text-ink-400 dark:text-ink-500">
                        {timeAgo(v.audit_metadata.created_at)}
                      </p>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {/* RCA panel */}
          <aside className="lg:col-span-3">
            <div className="card sticky top-20">
              <div className="card-header">
                <h2 className="card-title">Root-cause analysis</h2>
                <button
                  type="button"
                  onClick={() => runRca()}
                  disabled={rcaBusy || !selected}
                  className="btn btn-primary !py-1 !text-xs"
                >
                  {rcaBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  {rca ? 'Re-generate RCA' : 'Generate RCA'}
                </button>
              </div>
              <div className="card-body">
                {!rca && !rcaBusy && !rcaError && (
                  <EmptyState
                    label={selected ? `Analyse ${selected.affected_service}` : 'Select an incident'}
                    hint="Generate RCA to produce the root cause, ranked fix steps, and an approvable remediation."
                  />
                )}
                {rcaBusy && (
                  <div className="space-y-2 animate-pulse">
                    <div className="h-3 w-3/4 rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-3 w-5/6 rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                    <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                  </div>
                )}
                {rcaError && <p className="text-sm text-bad">{rcaError}</p>}
                {rca && !rcaBusy && <RcaView v={rca} />}
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
