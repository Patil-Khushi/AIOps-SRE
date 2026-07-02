import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Sparkles, RefreshCw, Inbox, Brain, Loader2 } from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, LoadingState, ErrorState } from '@/components/states';
import { SeverityBadge, StatusChip } from '@/components/SeverityBadge';
import { RcaView } from '@/components/RcaView';
import type { TriageVerdict, RCAVerdict, TriageResult, PrometheusAlert } from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';
import { makeCache } from '@/lib/persistentCache';

// localStorage-backed: RCA result survives page reloads and new tabs.
const rcaCache = makeCache<RCAVerdict>('rca');

// idx is a position tiebreaker for the null-created_at case (persistence is
// best-effort — a DB blip returns verdict_id=None which cascades to no created_at).
// Two verdicts for the same service in the same list position is impossible,
// so idx prevents collisions when created_at is missing.
function rcaKey(v: TriageVerdict, idx: number): string {
  return `${v.affected_service}:${v.severity}:${v.audit_metadata.created_at || idx}`;
}

// ─── RCA Agent console (PRS-008 ★) ──────────────────────────────────────────
//
// This is the RCA Agent's OWN surface — independent of Alert Triage. It takes
// the triage verdicts Alert Triage produced, lets the operator pick one, and
// runs root-cause analysis → ranked fix steps (each with a tested rollback) →
// a REQUIRED-HITL "approve & apply" gate. Alert Triage only generates the
// verdict; everything below the verdict belongs here.

export default function RcaConsole() {
  // Triaged verdicts (RCA's input). Polled so incidents self-populate as the
  // background auto-triage loop produces verdicts — the operator never has to
  // wait on a manual triage pass.
  const verdicts = useFetch(api.triageLive, { intervalMs: 5000, cacheKey: 'triage-live' });
  // Currently-firing alerts, fetched fast + in parallel so they show on the RCA
  // page IMMEDIATELY (as "triaging…" placeholders) instead of a blocking
  // spinner while the first triage pass runs.
  const live = useFetch(api.liveAlerts, { intervalMs: 5000, cacheKey: 'live-alerts' });
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [rca, setRca] = useState<RCAVerdict | null>(null);
  // ServiceNow incident number for the verdict the current RCA was run on.
  // Pinned at RCA time and forwarded to apply-fix so the backend fires the
  // resolution verifier → the 2nd (ticket-close) HITL approval.
  const [rcaIncidentId, setRcaIncidentId] = useState<string | null>(null);
  const [rcaBusy, setRcaBusy] = useState(false);
  const [rcaError, setRcaError] = useState<string | null>(null);

  // Service handed off from Alert Triage's "Generate RCA" button (router state).
  const location = useLocation();
  const wantedService = (location.state as { service?: string } | null)?.service;
  const handedOff = useRef(false);
  const handoffAttempts = useRef(0);

  const results = useMemo<TriageResult[]>(() => verdicts.data?.results ?? [], [verdicts.data]);
  const list: TriageVerdict[] = results.map((r) => r.verdict);

  // Firing alerts not yet represented by a triaged verdict — shown as
  // "triaging…" placeholders so the operator sees the incident immediately.
  const firing: PrometheusAlert[] = useMemo(() => {
    const triagedServices = new Set(list.map((v) => v.affected_service));
    return (live.data?.alerts ?? []).filter((a) => !triagedServices.has(a.service));
  }, [live.data, list]);
  const selectedResult = results[selectedIdx] ?? null;
  const selected: TriageVerdict | null = selectedResult?.verdict ?? null;

  // Keep a stable ref so the effect below can read results without declaring
  // it as a dependency (avoids re-firing on every poll revalidation).
  const resultsRef = useRef(results);
  resultsRef.current = results;

  // When the selected verdict changes, restore a cached RCA if one exists —
  // so switching back to a previously-analysed incident is instant.
  useEffect(() => {
    setRcaError(null);
    const v = resultsRef.current[selectedIdx]?.verdict;
    if (!v) {
      setRca(null);
      setRcaIncidentId(null);
      return;
    }
    const cached = rcaCache.get(rcaKey(v, selectedIdx));
    if (cached) {
      setRca(cached);
      setRcaIncidentId(resultsRef.current[selectedIdx]?.ticket?.ticket_id ?? null);
    } else {
      setRca(null);
      setRcaIncidentId(null);
    }
  }, [selectedIdx]);

  const runRca = async (target?: TriageVerdict, incidentId?: string | null, listIdx?: number) => {
    const v = target ?? selected;
    if (!v) return;
    const keyIdx = listIdx ?? selectedIdx;
    // Pin the ServiceNow incident number for this verdict (from RA-003's ticket
    // on the triage result). apply-fix forwards it so the verifier runs and the
    // ticket-close approval appears; without it only the fix approval shows.
    const inc =
      incidentId !== undefined ? incidentId : (selectedResult?.ticket?.ticket_id ?? null);
    setRca(null);
    setRcaError(null);
    setRcaBusy(true);
    setRcaIncidentId(inc);
    try {
      const result = await api.rca(v);
      rcaCache.set(rcaKey(v, keyIdx), result);
      setRca(result);
    } catch (e) {
      setRcaError(e instanceof Error ? e.message : String(e));
    } finally {
      setRcaBusy(false);
    }
  };

  // When arriving from Alert Triage (or from an approved fix on the Approvals
  // console), preselect the matching incident. If we already have a cached RCA
  // for it, just select it — the selection effect restores the verdict, so the
  // deep-link lands instantly on THAT failure without re-running the analysis.
  // Only run RCA fresh when nothing is cached (the Alert-Triage first-look case).
  useEffect(() => {
    if (handedOff.current || !wantedService) return;
    const idx = list.findIndex((v) => v.affected_service === wantedService);
    if (idx >= 0) {
      handedOff.current = true;
      setSelectedIdx(idx);
      const v = results[idx].verdict;
      if (!rcaCache.get(rcaKey(v, idx))) {
        runRca(v, results[idx]?.ticket?.ticket_id ?? null, idx);
      }
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

      {verdicts.error && !verdicts.data ? (
        <div className="card"><ErrorState error={verdicts.error} /></div>
      ) : list.length === 0 && firing.length === 0 ? (
        verdicts.loading || live.loading ? (
          <div className="card"><LoadingState label="Loading incidents…" /></div>
        ) : (
          <div className="card">
            <EmptyState
              label="No incidents yet"
              hint="Inject a scenario on the Overview page — firing alerts appear here immediately and RCA unlocks once each is triaged."
              icon={<Inbox className="h-7 w-7" />}
            />
          </div>
        )
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
              {/* Firing-but-not-yet-triaged alerts — shown immediately so the
                  operator sees the incident without waiting for the triage pass.
                  Non-selectable until a verdict exists (auto-triage fills it in). */}
              {firing.map((a) => (
                <li key={`firing-${a.alert_id}`}>
                  <div className="card w-full cursor-default opacity-90">
                    <div className="card-body !py-3">
                      <div className="flex items-center gap-2">
                        <span className="chip !border-warn/40 !text-warn">
                          <Loader2 className="mr-1 inline h-3 w-3 animate-spin" /> triaging…
                        </span>
                      </div>
                      <h3 className="mt-1.5 truncate text-sm font-semibold text-ink-900 dark:text-ink-50">
                        {a.service}
                      </h3>
                      <p className="mt-0.5 truncate text-xs text-ink-500 dark:text-ink-400">
                        {a.annotations?.summary || a.annotations?.description || a.metric}
                      </p>
                      <p className="mt-0.5 text-[11px] text-ink-400 dark:text-ink-500">
                        RCA unlocks once triage writes a verdict.
                      </p>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>

          {/* RCA panel — sticky on large screens but capped to the viewport and
              internally scrollable, so a long verdict (many fix steps + trace)
              is fully reachable instead of clipped below the fold. */}
          <aside className="lg:col-span-3">
            <div className="card lg:sticky lg:top-20 lg:flex lg:max-h-[calc(100vh-6rem)] lg:flex-col lg:overflow-hidden">
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
              <div className="card-body lg:flex-1 lg:overflow-y-auto">
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
                {rca && !rcaBusy && <RcaView v={rca} incidentId={rcaIncidentId} />}
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

