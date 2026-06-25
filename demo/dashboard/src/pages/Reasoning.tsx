import { useState } from 'react';
import { Brain, Play, ListChecks, GitMerge, Gauge } from 'lucide-react';
import { api } from '@/lib/api';
import { useFetch } from '@/hooks/useFetch';
import { LoadingState, ErrorState, EmptyState } from '@/components/states';
import { SeverityBadge, StatusChip } from '@/components/SeverityBadge';
import type { TriageVerdict } from '@/types/api';
import { clsx, timeAgo } from '@/lib/format';

// Tag the decision-trace lines into pipeline stages so we can visualize the
// 8-stage workflow. Tag heuristics mirror the strings emitted by
// agents/alert_triage/agent.py — keep in lockstep.
const STAGE_TAGS: { stage: string; match: RegExp }[] = [
  { stage: 'Validate',      match: /^received alert_id/ },
  { stage: 'Deduplicate',   match: /(new alert cluster|matched duplicate)/ },
  { stage: 'Correlate',     match: /(metric|trace|metrics_ctx|trace_ctx|fetched)/ },
  { stage: 'Severity',      match: /severity (from|inferred)/ },
  { stage: 'Ownership',     match: /(CMDB|on-call|Platform On-Call|assigned to)/ },
  { stage: 'Summary',       match: /(summary|generated incident)/i },
];

function tagStage(line: string): string {
  for (const { stage, match } of STAGE_TAGS) {
    if (match.test(line)) return stage;
  }
  return 'Other';
}

export default function Reasoning() {
  const verdicts = useFetch(api.triageLive, { intervalMs: 0, cacheKey: 'triage-live' });
  const [selectedIdx, setSelectedIdx] = useState(0);

  if (verdicts.loading) return <LoadingState label="Running RA-001 against every firing alert…" />;
  if (verdicts.error) return <ErrorState error={verdicts.error} />;

  const results = verdicts.data?.results ?? [];
  const list: TriageVerdict[] = results.map((r) => r.verdict);
  const selected: TriageVerdict | null = list[selectedIdx] ?? null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 dark:text-ink-50">
            AI reasoning
          </h1>
          <p className="mt-1 text-sm text-ink-500 dark:text-ink-400">
            Inspect every stage of RA-001's 8-step decision pipeline.
          </p>
        </div>
        <button onClick={verdicts.refetch} className="btn btn-primary">
          <Play className="h-4 w-4" /> Re-run on all firing alerts
        </button>
      </div>

      {list.length === 0 && (
        <div className="card"><EmptyState label="No firing alerts to reason over" hint="Inject a scenario from the Overview page to feed the agent." /></div>
      )}

      {list.length > 0 && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
          {/* Verdict list */}
          <div className="lg:col-span-1">
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
                      <p className="mt-0.5 truncate font-mono text-[11px] text-ink-500 dark:text-ink-400">
                        {timeAgo(v.audit_metadata.created_at)}
                      </p>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {/* Detail */}
          {selected && (
            <div className="space-y-4 lg:col-span-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Metric icon={<Gauge className="h-4 w-4" />} label="Confidence"
                        value={`${(selected.confidence_score * 100).toFixed(0)}%`} />
                <Metric icon={<GitMerge className="h-4 w-4" />} label="Dup count"
                        value={String(selected.duplicate_alert_count)} />
                <Metric icon={<ListChecks className="h-4 w-4" />} label="Trace lines"
                        value={String(selected.audit_metadata.decision_trace.length)} />
              </div>

              {/* Pipeline */}
              <div className="card">
                <div className="card-header">
                  <h2 className="card-title">
                    <Brain className="mr-1.5 inline h-3.5 w-3.5" />
                    8-stage pipeline
                  </h2>
                  <span className="chip">{selected.audit_metadata.created_by}</span>
                </div>
                <div className="card-body space-y-3">
                  <Stage idx={1} title="Validate / Normalize" lines={filterLines(selected, 'Validate')} />
                  <Stage idx={2} title="Deduplicate" lines={filterLines(selected, 'Deduplicate')} />
                  <Stage idx={3} title="Correlate (Prom + Jaeger)" lines={filterLines(selected, 'Correlate')} />
                  <Stage idx={4} title="Classify severity" lines={filterLines(selected, 'Severity')} />
                  <Stage idx={5} title="Resolve ownership" lines={filterLines(selected, 'Ownership')} />
                  <Stage idx={6} title="Generate summary" lines={filterLines(selected, 'Summary')} />
                </div>
              </div>

              {/* Final verdict */}
              <div className="card border-accent/30">
                <div className="card-header">
                  <h2 className="card-title">Final verdict</h2>
                </div>
                <div className="card-body">
                  <p className="text-sm leading-relaxed text-ink-800 dark:text-ink-100">
                    {selected.alert_summary}
                  </p>
                  <div className="mt-3 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                    <KV k="assigned_team" v={selected.assigned_team} />
                    {selected.assigned_engineer && <KV k="engineer" v={selected.assigned_engineer} />}
                    {selected.recommended_runbook && (
                      <KV
                        k="runbook"
                        v={selected.recommended_runbook}
                        href={`/api/runbooks/by-service/${encodeURIComponent(selected.affected_service)}`}
                      />
                    )}
                    <KV k="status" v={selected.status} />
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function filterLines(v: TriageVerdict, stage: string): string[] {
  return v.audit_metadata.decision_trace.filter((l) => tagStage(l) === stage);
}

function Stage({ idx, title, lines }: { idx: number; title: string; lines: string[] }) {
  const empty = lines.length === 0;
  return (
    <div className="rounded-lg border border-ink-200 bg-ink-50/40 p-3 dark:border-ink-700 dark:bg-ink-900/40">
      <div className="flex items-center gap-2">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15 font-mono text-xs font-bold text-accent">{idx}</span>
        <h3 className="text-sm font-semibold text-ink-900 dark:text-ink-50">{title}</h3>
        {empty && <span className="chip ml-auto">no trace</span>}
      </div>
      {!empty && (
        <ul className="mt-2 space-y-1 pl-8 font-mono text-[11px] text-ink-600 dark:text-ink-300">
          {lines.map((l, i) => <li key={i} className="leading-relaxed">{l}</li>)}
        </ul>
      )}
    </div>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="card">
      <div className="card-body flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/15 text-accent">{icon}</div>
        <div>
          <p className="text-[11px] uppercase tracking-wider text-ink-500 dark:text-ink-400">{label}</p>
          <p className="font-mono text-xl font-semibold text-ink-900 dark:text-ink-50">{value}</p>
        </div>
      </div>
    </div>
  );
}

function KV({ k, v, href }: { k: string; v: string; href?: string }) {
  return (
    <div className="flex gap-2">
      <span className="font-mono text-ink-500 dark:text-ink-400">{k}:</span>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="min-w-0 break-all font-mono text-accent hover:underline"
        >
          {v}
        </a>
      ) : (
        <span className="min-w-0 break-all font-mono text-ink-900 dark:text-ink-50">{v}</span>
      )}
    </div>
  );
}
