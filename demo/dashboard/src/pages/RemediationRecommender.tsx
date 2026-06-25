import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Sparkles, RefreshCw, Inbox, ListChecks, ShieldAlert, Clock, Gauge, Undo2,
  ArrowRight, Star, Wrench, Brain,
} from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { EmptyState, LoadingState, ErrorState } from '@/components/states';
import { SeverityBadge, StatusChip } from '@/components/SeverityBadge';
import { setConsoleAgent } from '@/lib/consoleScope';
import { getAgentById } from '@/data/agentCatalog';
import type {
  TriageVerdict, RCAVerdict, RemediationVerdict, RemediationOption, BlastRadius,
} from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';

// ─── Remediation Recommender console (PRS-001) ───────────────────────────────
//
// This is the Remediation Recommender's OWN surface. It consumes the RCA
// Agent's verdict (PRS-008) for a triaged incident and produces a *ranked
// decision set* of remediation options — each with a blast radius, confidence,
// MTTR estimate, tested rollback, and the tool capability that would execute
// it. The operator picks one and hands it to the Auto-Healer (PRS-002), which
// runs it through the platform HITL gate. Recommending is this agent's job;
// executing belongs to Auto-Healer — the two stay independent.

const BLAST_STYLE: Record<BlastRadius, string> = {
  low: '!border-ok/40 !text-ok',
  medium: '!border-warn/40 !text-warn',
  high: '!border-bad/40 !text-bad',
};

export default function RemediationRecommender() {
  const catalog = getAgentById('remediation-recommender');
  const incidents = useFetch(api.triageLive, { intervalMs: 0, cacheKey: 'triage-live' });
  const navigate = useNavigate();

  const [selectedIdx, setSelectedIdx] = useState(0);
  const [rca, setRca] = useState<RCAVerdict | null>(null);
  const [reco, setReco] = useState<RemediationVerdict | null>(null);
  const [incidentId, setIncidentId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const results = incidents.data?.results ?? [];
  const list: TriageVerdict[] = results.map((r) => r.verdict);
  const selectedResult = results[selectedIdx] ?? null;
  const selected: TriageVerdict | null = selectedResult?.verdict ?? null;

  // Clear any prior analysis when the picked incident changes.
  useEffect(() => {
    setRca(null);
    setReco(null);
    setError(null);
    setIncidentId(null);
  }, [selectedIdx]);

  // Diagnose (RCA) → Recommend (PRS-001). RCA is the agent's required input,
  // so we run it first, then rank options off its verdict.
  const recommend = async () => {
    if (!selected) return;
    const inc = selectedResult?.ticket?.ticket_id ?? null;
    setRca(null);
    setReco(null);
    setError(null);
    setBusy(true);
    setIncidentId(inc);
    try {
      const rcaVerdict = await api.rca(selected);
      setRca(rcaVerdict);
      const verdict = await api.remediation(rcaVerdict, selected);
      setReco(verdict);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Hand a chosen option to the Auto-Healer page (router state). Scope the
  // console to Auto-Healer so its sidebar chrome is right on arrival.
  const sendToHealer = (option: RemediationOption) => {
    if (!selected) return;
    setConsoleAgent('auto-healer');
    navigate('/agents/auto-healer', {
      state: {
        option,
        affectedService: selected.affected_service,
        incidentId,
        rootCause: rca?.root_cause ?? null,
      },
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
              <ListChecks className="h-6 w-6 text-accent" /> Remediation Recommender
            </h1>
            <span className="chip font-mono">PRS-001</span>
            <span className="chip !border-warn/40 !text-warn"><ShieldAlert className="h-3 w-3" /> HITL Required</span>
          </div>
          <p className="mt-1 max-w-2xl text-sm text-ink-500 dark:text-ink-400">
            {catalog?.summary ?? 'Recommend the best fix and why it should work.'} Consumes the RCA
            verdict and ranks safe, reversible options for a human to approve.
          </p>
        </div>
        <button onClick={incidents.refetch} className="btn">
          <RefreshCw className={clsx('h-4 w-4', incidents.loading && 'animate-spin')} /> Refresh incidents
        </button>
      </div>

      {incidents.loading && !incidents.data ? (
        <div className="card"><LoadingState label="Loading triaged incidents…" /></div>
      ) : incidents.error ? (
        <div className="card"><ErrorState error={incidents.error} /></div>
      ) : list.length === 0 ? (
        <div className="card">
          <EmptyState
            label="No triaged incidents yet"
            hint="Inject a scenario on the Overview page — once Alert Triage writes a verdict, it lands here to recommend remediations for."
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
                      <p className="mt-0.5 truncate text-xs text-ink-500 dark:text-ink-400">{v.alert_summary}</p>
                      <p className="mt-0.5 font-mono text-[11px] text-ink-400 dark:text-ink-500">
                        {timeAgo(v.audit_metadata.created_at)}
                      </p>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {/* Recommendation panel */}
          <aside className="lg:col-span-3">
            <div className="card sticky top-20">
              <div className="card-header">
                <h2 className="card-title">Ranked remediation options</h2>
                <button
                  type="button"
                  onClick={recommend}
                  disabled={busy || !selected}
                  className="btn btn-primary !py-1 !text-xs"
                >
                  {busy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  {reco ? 'Re-run' : 'Recommend fixes'}
                </button>
              </div>
              <div className="card-body space-y-3">
                {!reco && !busy && !error && (
                  <EmptyState
                    label={selected ? `Recommend fixes for ${selected.affected_service}` : 'Select an incident'}
                    hint="Diagnoses the incident (RCA) then ranks reversible remediation options for approval."
                  />
                )}
                {busy && (
                  <div className="space-y-2">
                    <p className="flex items-center gap-2 text-xs text-ink-500 dark:text-ink-400">
                      <Brain className="h-3.5 w-3.5 text-accent" />
                      {rca ? 'Ranking remediation options…' : 'Diagnosing root cause (RCA)…'}
                    </p>
                    <div className="animate-pulse space-y-2">
                      <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                      <div className="h-12 w-full rounded bg-ink-200 dark:bg-ink-700" />
                    </div>
                  </div>
                )}
                {error && <p className="text-sm text-bad">{error}</p>}

                {reco && !busy && (
                  <>
                    {rca && (
                      <div className="rounded-md border border-ink-200 bg-ink-50/50 p-2.5 dark:border-ink-700 dark:bg-ink-800/30">
                        <div className="flex items-baseline justify-between gap-2">
                          <p className="card-title !text-[10px]">Diagnosed cause (RCA)</p>
                          <span className="font-mono text-[11px] text-ink-500 dark:text-ink-400">
                            confidence {(rca.confidence_score * 100).toFixed(0)}%
                          </span>
                        </div>
                        <p className="mt-1 text-xs leading-relaxed text-ink-700 dark:text-ink-200">{rca.root_cause}</p>
                      </div>
                    )}
                    <p className="text-[11px] text-ink-500 dark:text-ink-400">
                      {reco.options.length} option{reco.options.length === 1 ? '' : 's'} · ranked safest-first ·
                      overall confidence {(reco.confidence_score * 100).toFixed(0)}%
                    </p>
                    {reco.options.map((o) => (
                      <OptionCard
                        key={o.option_id}
                        option={o}
                        recommended={o.option_id === reco.recommended_option_id}
                        onSend={() => sendToHealer(o)}
                      />
                    ))}
                  </>
                )}
              </div>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function OptionCard({
  option, recommended, onSend,
}: {
  option: RemediationOption;
  recommended: boolean;
  onSend: () => void;
}) {
  return (
    <div className={clsx(
      'rounded-md border p-3',
      recommended ? 'border-accent/50 bg-accent/5' : 'border-ink-200 bg-ink-50/50 dark:border-ink-700 dark:bg-ink-800/30',
    )}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            {recommended && (
              <span className="chip !border-accent/50 !text-accent"><Star className="mr-1 inline h-3 w-3" /> recommended</span>
            )}
            <h3 className="text-sm font-semibold text-ink-900 dark:text-ink-50">{option.title}</h3>
          </div>
          <p className="mt-1 text-xs leading-snug text-ink-600 dark:text-ink-300">{option.description}</p>
        </div>
        <button type="button" onClick={onSend} className="btn btn-primary flex-shrink-0 !py-1 !text-xs">
          Send to Auto-Healer <ArrowRight className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className={clsx('chip', BLAST_STYLE[option.blast_radius])}>
          <ShieldAlert className="mr-1 inline h-3 w-3" /> blast: {option.blast_radius} ({option.blast_radius_score}/5)
        </span>
        <span className="chip"><Gauge className="mr-1 inline h-3 w-3" /> confidence {(option.confidence * 100).toFixed(0)}%</span>
        <span className="chip"><Clock className="mr-1 inline h-3 w-3" /> ~{option.estimated_mttr_minutes}m MTTR</span>
        <span className={clsx('chip', option.rollback_tested ? '!border-ok/40 !text-ok' : '!border-warn/40 !text-warn')}>
          <Undo2 className="mr-1 inline h-3 w-3" /> rollback {option.rollback_tested ? 'tested' : 'untested'}
        </span>
        {option.tool_capability ? (
          <span className="chip !border-accent/40 !text-accent" title={JSON.stringify(option.tool_args)}>
            <Wrench className="mr-1 inline h-3 w-3" /> {option.action_type} · {option.tool_capability}
          </span>
        ) : (
          <span className="chip" title="No automated executor — operator carries it out">manual</span>
        )}
      </div>

      <div className="mt-1.5 rounded bg-ink-100 px-2 py-1 font-mono text-[11px] text-ink-700 dark:bg-ink-900 dark:text-ink-200">
        <span className="text-ink-500 dark:text-ink-400">rollback:</span> {option.rollback}
      </div>
      <p className="mt-1.5 text-[11px] italic text-ink-500 dark:text-ink-400">{option.rationale}</p>
    </div>
  );
}
